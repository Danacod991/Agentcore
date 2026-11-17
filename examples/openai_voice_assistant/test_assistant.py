"""End-to-end tests against a fake OpenAI Realtime server (no API key or network needed).

Run from this directory:  pytest test_assistant.py
"""

import asyncio
import json
import socket

import auth
import httpx
import pytest
import realtime
import tools
import uvicorn
import websockets
from assistant import app, translate_upstream_event


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Every client event the fake server receives, so tests can inspect what was sent to OpenAI.
RECEIVED = []


async def fake_openai(ws):
    """Minimal stand-in for the Realtime API: one tool call, then a spoken/text answer."""
    tool_round_done = False
    async for raw in ws:
        event = json.loads(raw)
        RECEIVED.append(event)
        kind = event["type"]
        if kind == "session.update":
            ws.session = event["session"]
            await ws.send(json.dumps({"type": "session.updated", "session": event["session"]}))
        elif kind == "input_audio_buffer.append" and not getattr(ws, "heard", False):
            ws.heard = True
            await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
            await ws.send(
                json.dumps(
                    {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Save a note"}
                )
            )
            await ws.send(json.dumps(_tool_call_response()))
        elif kind == "response.create" and not tool_round_done and ws.session["output_modalities"] == ["text"]:
            tool_round_done = True
            await ws.send(json.dumps(_tool_call_response()))
        elif kind == "conversation.item.create" and event["item"]["type"] == "function_call_output":
            ws.tool_output = json.loads(event["item"]["output"])
        elif kind == "response.create" and hasattr(ws, "tool_output"):
            if ws.session["output_modalities"] == ["audio"]:
                await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": "AAAA"}))
                await ws.send(json.dumps({"type": "response.output_audio_transcript.done", "transcript": "Saved!"}))
            else:
                await ws.send(json.dumps({"type": "response.output_text.delta", "delta": "Saved!"}))
            await ws.send(json.dumps({"type": "response.done", "response": {"output": []}}))


def _tool_call_response():
    call = {"type": "function_call", "name": "save_note", "call_id": "call_1", "arguments": '{"text": "likes tea"}'}
    return {"type": "response.done", "response": {"output": [call]}}


@pytest.fixture
async def servers(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(tools, "NOTES_PATH", tmp_path / "notes.json")
    upstream = await websockets.serve(fake_openai, "127.0.0.1", 0)
    monkeypatch.setattr(realtime, "REALTIME_URL", f"ws://127.0.0.1:{upstream.sockets[0].getsockname()[1]}")

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield port
    server.should_exit = True
    await task
    upstream.close()
    await upstream.wait_closed()


async def test_voice_session_runs_tool_and_streams_audio(servers):
    async with websockets.connect(f"ws://127.0.0.1:{servers}/ws") as ws:
        assert json.loads(await ws.recv()) == {"type": "ready"}
        await ws.send(json.dumps({"type": "audio", "audio": "AAAA"}))

        received = []
        while not received or received[-1] != {"type": "response_done"} or len(received) < 6:
            received.append(json.loads(await asyncio.wait_for(ws.recv(), 5)))

    assert received[0] == {"type": "interrupt"}
    assert {"type": "transcript", "role": "user", "text": "Save a note", "final": True} in received
    assert {"type": "tool", "name": "save_note"} in received
    assert {"type": "audio", "audio": "AAAA"} in received
    assert {"type": "transcript", "role": "assistant", "text": "Saved!", "final": True} in received
    assert tools.list_notes()["notes"][0]["text"] == "likes tea"


async def test_text_invocation(servers):
    async with httpx.AsyncClient() as client:
        response = await client.post(f"http://127.0.0.1:{servers}/invocations", json={"prompt": "Remember I like tea"})
    assert response.json() == {"response": "Saved!", "tools_used": ["save_note"]}


async def test_index_page_served(servers):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{servers}/")
    assert response.status_code == 200
    assert "Voice Assistant" in response.text


def test_session_config_shapes():
    voice = realtime.session_config(audio=True)["session"]
    assert voice["output_modalities"] == ["audio"]
    assert voice["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert {t["name"] for t in voice["tools"]} == set(tools.TOOL_HANDLERS)
    assert "audio" not in realtime.session_config(audio=False)["session"]


def test_translate_ignores_unknown_events():
    assert translate_upstream_event({"type": "rate_limits.updated"}) is None


def test_notes_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "NOTES_PATH", tmp_path / "notes.json")
    first = tools.save_note("buy milk")["saved"]
    tools.save_note("call mom")
    assert tools.delete_note(first["id"]) == {"deleted": first["id"]}
    assert [n["text"] for n in tools.list_notes()["notes"]] == ["call mom"]
    assert "error" in tools.delete_note(99)


async def test_run_tool_reports_errors():
    assert "Unknown tool" in json.loads(await tools.run_tool("nope", "{}"))["error"]
    assert json.loads(await tools.run_tool("get_current_time", '{"timezone": "Mars/Base"}'))["error"]
    assert "iso" in json.loads(await tools.run_tool("get_current_time", "{}"))


async def test_pwa_files_served(servers):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{servers}") as client:
        manifest = await client.get("/manifest.webmanifest")
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        for icon in manifest.json()["icons"]:
            assert (await client.get(icon["src"])).status_code == 200
        assert (await client.get("/sw.js")).headers["content-type"].startswith("text/javascript")
        assert (await client.get("/offline.html")).status_code == 200


async def test_password_protects_app(servers, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "open sesame")
    base = f"http://127.0.0.1:{servers}"
    async with httpx.AsyncClient(base_url=base) as client:
        assert (await client.get("/")).headers["location"] == "/login"
        assert (await client.post("/invocations", json={"prompt": "hi"})).status_code == 401
        for public in ("/ping", "/login", "/manifest.webmanifest", "/sw.js", "/icons/icon-192.png"):
            assert (await client.get(public)).status_code == 200, public
        with pytest.raises(websockets.InvalidStatus):
            await websockets.connect(f"ws://127.0.0.1:{servers}/ws")

        wrong = await client.post("/login", data={"password": "nope"})
        assert wrong.headers["location"] == "/login?error=1"
        right = await client.post("/login", data={"password": "open sesame"})
        assert right.headers["location"] == "/"
        assert "httponly" in right.headers["set-cookie"].lower()
        assert (await client.get("/")).status_code == 200

        cookie = f"{auth.COOKIE_NAME}={client.cookies[auth.COOKIE_NAME]}"
        async with websockets.connect(f"ws://127.0.0.1:{servers}/ws", additional_headers={"Cookie": cookie}) as ws:
            assert json.loads(await ws.recv()) == {"type": "ready"}

        bearer = {"Authorization": "Bearer open sesame"}
        response = await httpx.AsyncClient().post(f"{base}/invocations", json={"prompt": "hi"}, headers=bearer)
        assert response.json()["response"] == "Saved!"

    monkeypatch.setenv("APP_PASSWORD", "changed")
    async with httpx.AsyncClient(base_url=base, cookies={auth.COOKIE_NAME: client.cookies[auth.COOKIE_NAME]}) as c2:
        assert (await c2.get("/")).status_code == 303


async def test_settings_and_history_reach_openai(servers):
    RECEIVED.clear()
    history = [
        {"role": "user", "text": "My name is Sam"},
        {"role": "tool", "text": "save_note"},
        {"role": "assistant", "text": "Nice to meet you, Sam!"},
    ]
    async with websockets.connect(f"ws://127.0.0.1:{servers}/ws?voice=cedar&name=Jarvis") as ws:
        assert json.loads(await ws.recv()) == {"type": "ready"}
        await ws.send(json.dumps({"type": "history", "messages": history}))
        await ws.send(json.dumps({"type": "text", "text": "What's my name?"}))
        await asyncio.sleep(0.2)

    session = next(e["session"] for e in RECEIVED if e["type"] == "session.update")
    assert session["audio"]["output"]["voice"] == "cedar"
    assert session["instructions"].startswith("You are Jarvis,")
    items = [e["item"] for e in RECEIVED if e["type"] == "conversation.item.create"]
    assert [(i["role"], i["content"][0]["type"], i["content"][0]["text"]) for i in items] == [
        ("user", "input_text", "My name is Sam"),
        ("assistant", "output_text", "Nice to meet you, Sam!"),
        ("user", "input_text", "What's my name?"),
    ]


async def test_unknown_voice_falls_back_to_default(servers):
    RECEIVED.clear()
    async with websockets.connect(f"ws://127.0.0.1:{servers}/ws?voice=not-a-voice") as ws:
        await ws.recv()
    session = next(e["session"] for e in RECEIVED if e["type"] == "session.update")
    assert session["audio"]["output"]["voice"] == realtime.VOICE


async def test_notes_and_config_api(servers):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{servers}") as client:
        config = (await client.get("/api/config")).json()
        assert config["voice"] in config["voices"] and config["name"]

        assert (await client.get("/api/notes")).json() == {"notes": []}
        assert (await client.post("/api/notes", json={"text": "  "})).status_code == 400
        created = await client.post("/api/notes", json={"text": "Coffee, black"})
        assert created.status_code == 201
        note_id = created.json()["saved"]["id"]
        assert [n["text"] for n in (await client.get("/api/notes")).json()["notes"]] == ["Coffee, black"]
        assert (await client.delete(f"/api/notes/{note_id}")).status_code == 200
        assert (await client.delete(f"/api/notes/{note_id}")).status_code == 404


async def test_notes_api_requires_sign_in(servers, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{servers}") as client:
        assert (await client.get("/api/notes")).status_code == 401
        assert (await client.get("/api/notes", headers={"Authorization": "Bearer pw"})).status_code == 200
