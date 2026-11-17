"""Personal voice assistant on Bedrock AgentCore Runtime, powered by the OpenAI Realtime API.

Endpoints:
    GET  /             Browser voice client (push the mic button and talk). Installable as a phone app.
    GET  /login        Sign-in page, used only when APP_PASSWORD is set (see auth.py).
    WS   /ws           Voice bridge: browser <-> this server <-> OpenAI Realtime.
                       Optional query params ?voice=<voice>&name=<assistant name> override the defaults.
    GET  /api/config   Voices to choose from and the server defaults, for the settings panel.
    GET  /api/notes    Saved notes; POST {"text": ...} adds one, DELETE /api/notes/{id} removes one.
    POST /invocations  Text mode: {"prompt": "..."} -> {"response": "...", "tools_used": [...]}.
    GET  /ping         AgentCore health check.

Browser protocol on /ws (JSON messages):
    client -> server  {"type": "audio", "audio": <base64 pcm16 24kHz mono>}
                      {"type": "text", "text": "..."}
                      {"type": "history", "messages": [{"role": "user" | "assistant", "text": "..."}]}
                                    earlier turns to continue a past conversation
    server -> client  {"type": "ready"}
                      {"type": "audio", "audio": <base64 pcm16 24kHz mono>}
                      {"type": "transcript", "role": "user" | "assistant", "delta" | "text": "...", "final": bool}
                      {"type": "interrupt"}          user started talking; stop playback
                      {"type": "tool", "name": "..."}
                      {"type": "response_done"}
                      {"type": "error", "message": "..."}
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

import auth
import realtime
import tools
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from bedrock_agentcore import BedrockAgentCoreApp

logger = logging.getLogger("voice_assistant")

app = BedrockAgentCoreApp(middleware=[Middleware(auth.PasswordMiddleware)])
STATIC = Path(__file__).parent / "static"


def static_page(name: str, media_type: Optional[str] = None, headers: Optional[Dict[str, str]] = None) -> Any:
    """Route handler that serves one file from ``static/``."""

    async def handler(request: Request) -> FileResponse:
        return FileResponse(STATIC / name, media_type=media_type, headers=headers)

    return handler


async def login(request: Request) -> Response:
    """Check the submitted password and set the session cookie."""
    if not auth.password():
        return RedirectResponse("/", status_code=303)
    form = parse_qs((await request.body()).decode())
    if not auth.check_password(form.get("password", [""])[0]):
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.session_token(auth.password()),
        max_age=auth.COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        # Hosts like Render and Fly terminate TLS and forward plain HTTP, so trust their header.
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
    )
    return response


async def config(request: Request) -> JSONResponse:
    """Options for the settings panel."""
    return JSONResponse({"voices": realtime.VOICES, "voice": realtime.VOICE, "name": realtime.ASSISTANT_NAME})


async def notes(request: Request) -> JSONResponse:
    """List notes (GET) or add one (POST) for the notes panel."""
    if request.method == "POST":
        text = str((await request.json()).get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "Note text is required."}, status_code=400)
        return JSONResponse(await asyncio.to_thread(tools.save_note, text[:2000]), status_code=201)
    return JSONResponse(await asyncio.to_thread(tools.list_notes))


async def delete_note(request: Request) -> JSONResponse:
    """Remove one note by id."""
    result = await asyncio.to_thread(tools.delete_note, request.path_params["note_id"])
    return JSONResponse(result, status_code=404 if "error" in result else 200)


app.add_route("/", static_page("index.html"), methods=["GET"])
app.add_route("/api/config", config, methods=["GET"])
app.add_route("/api/notes", notes, methods=["GET", "POST"])
app.add_route("/api/notes/{note_id:int}", delete_note, methods=["DELETE"])
app.add_route("/login", static_page("login.html"), methods=["GET"])
app.add_route("/login", login, methods=["POST"])
app.add_route("/manifest.webmanifest", static_page("manifest.webmanifest", "application/manifest+json"))
# The service worker must be served from the root so it can control the whole app.
app.add_route("/sw.js", static_page("sw.js", "text/javascript", {"Cache-Control": "no-cache"}))
app.add_route("/offline.html", static_page("offline.html"))
app.add_route("/favicon.ico", static_page("favicon.ico"))
app.mount("/icons", StaticFiles(directory=STATIC / "icons"), name="icons")


@app.entrypoint
async def invoke(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Text mode, handy for `agentcore invoke` and quick tests."""
    prompt = (payload or {}).get("prompt")
    if not prompt:
        return {"error": 'Send a JSON body like {"prompt": "What\'s the weather in Paris?"}'}
    return await realtime.ask_text(prompt, name=str(payload.get("name", "")))


def translate_upstream_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map an OpenAI Realtime server event to the simpler browser protocol (None = don't forward)."""
    kind = event.get("type")
    if kind == "session.updated":
        return {"type": "ready"}
    if kind == "response.output_audio.delta":
        return {"type": "audio", "audio": event["delta"]}
    if kind == "response.output_audio_transcript.delta":
        return {"type": "transcript", "role": "assistant", "delta": event.get("delta", ""), "final": False}
    if kind == "response.output_audio_transcript.done":
        return {"type": "transcript", "role": "assistant", "text": event.get("transcript", ""), "final": True}
    if kind == "conversation.item.input_audio_transcription.completed":
        return {"type": "transcript", "role": "user", "text": event.get("transcript", ""), "final": True}
    if kind == "input_audio_buffer.speech_started":
        return {"type": "interrupt"}
    if kind == "error":
        return {"type": "error", "message": event.get("error", {}).get("message", "Realtime API error")}
    return None


async def pump_browser_to_openai(websocket: WebSocket, upstream: Any) -> None:
    """Forward mic audio and typed messages from the browser to OpenAI."""
    while True:
        message = await websocket.receive_json()
        kind = message.get("type")
        if kind == "audio" and message.get("audio"):
            await realtime.send(upstream, {"type": "input_audio_buffer.append", "audio": message["audio"]})
        elif kind == "text" and message.get("text", "").strip():
            await realtime.send_user_text(upstream, message["text"].strip())
        elif kind == "history":
            await realtime.restore_history(upstream, message.get("messages", []))


async def pump_openai_to_browser(websocket: WebSocket, upstream: Any) -> None:
    """Relay OpenAI events to the browser and run tool calls server-side."""
    async for raw in upstream:
        event = json.loads(raw)
        outgoing = translate_upstream_event(event)
        if outgoing:
            await websocket.send_json(outgoing)
        if event.get("type") == "response.done":
            for name in await realtime.handle_tool_calls(upstream, event):
                logger.info("Tool call: %s", name)
                await websocket.send_json({"type": "tool", "name": name})
            await websocket.send_json({"type": "response_done"})


@app.websocket
async def voice(websocket: WebSocket, context: Any) -> None:
    """Bridge one browser voice session to one OpenAI Realtime session."""
    await websocket.accept()
    try:
        upstream = await realtime.connect()
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"Could not reach OpenAI: {e}"})
        await websocket.close(code=1011)
        return

    tasks = []
    try:
        params = websocket.query_params
        session = realtime.session_config(audio=True, voice=params.get("voice", ""), name=params.get("name", ""))
        await realtime.send(upstream, session)
        tasks = [
            asyncio.create_task(pump_browser_to_openai(websocket, upstream)),
            asyncio.create_task(pump_openai_to_browser(websocket, upstream)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() and not isinstance(task.exception(), WebSocketDisconnect):
                raise task.exception()
    finally:
        for task in tasks:
            task.cancel()
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed by the browser


if __name__ == "__main__":
    if not auth.password():
        logger.warning("APP_PASSWORD is not set: anyone who can reach this server can use your OpenAI key.")
    app.run(port=int(os.environ.get("PORT", "8080")))
