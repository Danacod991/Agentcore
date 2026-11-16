"""End-to-end tests against a fake OpenAI Realtime server (no API key or network needed).

Run from this directory:  pytest test_assistant.py
"""

import asyncio
import json
import socket

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


async def fake_openai(ws):
    """Minimal stand-in for the Realtime API: one tool call, then a spoken/text answer."""
    tool_round_done = False
    async for raw in ws:
        event = json.loads(raw)
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
