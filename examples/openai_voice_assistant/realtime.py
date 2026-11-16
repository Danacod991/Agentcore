"""Thin helpers around the OpenAI Realtime API (WebSocket transport)."""

import json
import os
from datetime import datetime
from typing import Any, Dict, List

import websockets
from tools import TOOL_SCHEMAS, run_tool

REALTIME_URL = os.environ.get("OPENAI_REALTIME_URL", "wss://api.openai.com/v1/realtime")
MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
VOICE = os.environ.get("OPENAI_VOICE", "marin")
TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Nova")

# Voices the browser may pick from; anything else falls back to VOICE.
VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")

# The Realtime API streams 16-bit little-endian mono PCM at 24 kHz in both directions.
SAMPLE_RATE = 24000


def instructions(name: str = "") -> str:
    """System prompt for the assistant, rebuilt per session so the date is current."""
    return (
        f"You are {name or ASSISTANT_NAME}, a warm, upbeat personal voice assistant. "
        f"Today is {datetime.now().astimezone():%A, %B %d %Y}. "
        "Keep spoken replies short and conversational: one to three sentences unless the user asks for detail. "
        "Never read out URLs, markdown, or long lists; summarise instead. "
        "Use your tools for the time, the weather, and the user's personal notes. "
        "When the user shares a lasting preference or asks you to remember something, save it as a note. "
        "At the start of a conversation, check the user's notes if it would help you personalise your answer."
    )


def session_config(audio: bool = True, voice: str = "", name: str = "") -> Dict[str, Any]:
    """Build a ``session.update`` event for a voice (audio) or text-only session.

    ``voice`` and ``name`` are per-session overrides chosen in the browser settings.
    """
    session: Dict[str, Any] = {
        "type": "realtime",
        "model": MODEL,
        "instructions": instructions(name[:40].strip()),
        "output_modalities": ["audio"] if audio else ["text"],
        "tools": TOOL_SCHEMAS,
        "tool_choice": "auto",
    }
    if audio:
        pcm = {"type": "audio/pcm", "rate": SAMPLE_RATE}
        session["audio"] = {
            "input": {
                "format": pcm,
                "transcription": {"model": TRANSCRIBE_MODEL},
                "noise_reduction": {"type": "near_field"},
                "turn_detection": {"type": "semantic_vad", "create_response": True, "interrupt_response": True},
            },
            "output": {"format": pcm, "voice": voice if voice in VOICES else VOICE},
        }
    return {"type": "session.update", "session": session}


async def connect() -> Any:
    """Open a WebSocket to the Realtime API. The API key never leaves the server."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return await websockets.connect(
        f"{REALTIME_URL}?model={MODEL}",
        additional_headers={"Authorization": f"Bearer {api_key}"},
        max_size=None,
    )


async def send(upstream: Any, event: Dict[str, Any]) -> None:
    """Send one client event to the Realtime API."""
    await upstream.send(json.dumps(event))


async def send_user_text(upstream: Any, text: str) -> None:
    """Add a typed user message to the conversation and ask for a reply."""
    await send(
        upstream,
        {
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    await send(upstream, {"type": "response.create"})


async def handle_tool_calls(upstream: Any, response_done: Dict[str, Any]) -> List[str]:
    """Run every function call in a finished response, then request a follow-up response.

    Waiting for ``response.done`` (rather than reacting to each call as it streams) lets the
    model issue several calls in one turn without overlapping ``response.create`` requests.
    Returns the names of the tools that ran.
    """
    output_items = response_done.get("response", {}).get("output", [])
    calls = [item for item in output_items if item.get("type") == "function_call"]
    for call in calls:
        output = await run_tool(call["name"], call.get("arguments", "{}"))
        await send(
            upstream,
            {
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call["call_id"], "output": output},
            },
        )
    if calls:
        await send(upstream, {"type": "response.create"})
    return [call["name"] for call in calls]


async def ask_text(prompt: str, name: str = "") -> Dict[str, Any]:
    """Run a single text-in, text-out turn (used by the text endpoints)."""
    upstream = await connect()
    try:
        await send(upstream, session_config(audio=False, name=name))
        await send_user_text(upstream, prompt)
        text: List[str] = []
        tools_used: List[str] = []
        async for raw in upstream:
            event = json.loads(raw)
            kind = event.get("type")
            if kind == "response.output_text.delta":
                text.append(event.get("delta", ""))
            elif kind == "error":
                raise RuntimeError(event.get("error", {}).get("message", "Realtime API error"))
            elif kind == "response.done":
                ran = await handle_tool_calls(upstream, event)
                if not ran:
                    break
                tools_used.extend(ran)
        return {"response": "".join(text), "tools_used": tools_used}
    finally:
        await upstream.close()
