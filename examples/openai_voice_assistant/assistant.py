"""Personal voice assistant on Bedrock AgentCore Runtime, powered by the OpenAI Realtime API.

Endpoints:
    GET  /             Browser voice client (push the mic button and talk).
    WS   /ws           Voice bridge: browser <-> this server <-> OpenAI Realtime.
    POST /invocations  Text mode: {"prompt": "..."} -> {"response": "...", "tools_used": [...]}.
    GET  /ping         AgentCore health check.

Browser protocol on /ws (JSON messages):
    client -> server  {"type": "audio", "audio": <base64 pcm16 24kHz mono>}
                      {"type": "text", "text": "..."}
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

import realtime
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from bedrock_agentcore import BedrockAgentCoreApp

logger = logging.getLogger("voice_assistant")

app = BedrockAgentCoreApp()
INDEX_HTML = Path(__file__).parent / "static" / "index.html"


async def index(request: Request) -> FileResponse:
    """Serve the browser voice client."""
    return FileResponse(INDEX_HTML)


app.add_route("/", index, methods=["GET"])


@app.entrypoint
async def invoke(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Text mode, handy for `agentcore invoke` and quick tests."""
    prompt = (payload or {}).get("prompt")
    if not prompt:
        return {"error": 'Send a JSON body like {"prompt": "What\'s the weather in Paris?"}'}
    return await realtime.ask_text(prompt)


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
        await realtime.send(upstream, realtime.session_config(audio=True))
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
    app.run(port=int(os.environ.get("PORT", "8080")))
