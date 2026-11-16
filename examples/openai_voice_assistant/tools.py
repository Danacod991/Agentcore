"""Tools the voice assistant can call during a conversation.

Each tool has an OpenAI Realtime function schema in ``TOOL_SCHEMAS`` and an
implementation registered in ``TOOL_HANDLERS``. Handlers receive the parsed
arguments and return a JSON-serialisable dict that is sent back to the model.
"""

import asyncio
import json
import os
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NOTES_PATH = Path(os.environ.get("ASSISTANT_NOTES_PATH", Path.home() / ".voice_assistant_notes.json"))
HTTP_TIMEOUT_SECONDS = 10

_notes_lock = threading.Lock()

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_current_time",
        "description": "Get the current date and time, optionally in a specific IANA timezone such as 'Europe/Paris'.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "IANA timezone name. Defaults to the server timezone."}
            },
        },
    },
    {
        "type": "function",
        "name": "get_weather",
        "description": "Get the current weather and today's forecast for a city.",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City name, e.g. 'Seattle'."}},
            "required": ["location"],
        },
    },
    {
        "type": "function",
        "name": "save_note",
        "description": "Remember something for the user, such as a preference, a to-do item, or a fact about them.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The note to remember."}},
            "required": ["text"],
        },
    },
    {
        "type": "function",
        "name": "list_notes",
        "description": "Recall everything previously saved for the user.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "delete_note",
        "description": "Forget a previously saved note by its id (from list_notes).",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "The note id."}},
            "required": ["id"],
        },
    },
]


def get_current_time(timezone: str = "") -> Dict[str, Any]:
    """Return the current time, in the requested timezone when given."""
    if timezone:
        try:
            now = datetime.now(ZoneInfo(timezone))
        except ZoneInfoNotFoundError:
            return {"error": f"Unknown timezone '{timezone}'."}
    else:
        now = datetime.now().astimezone()
    return {"iso": now.isoformat(timespec="seconds"), "readable": now.strftime("%A, %B %d %Y, %I:%M %p %Z")}


def _http_get_json(url: str) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:  # nosec B310 - fixed https hosts
        return json.loads(response.read().decode("utf-8"))


def get_weather(location: str) -> Dict[str, Any]:
    """Look up weather with the free Open-Meteo API (no API key needed)."""
    query = urllib.parse.urlencode({"name": location, "count": 1})
    geo = _http_get_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}")
    if not geo.get("results"):
        return {"error": f"Could not find a place called '{location}'."}
    place = geo["results"][0]
    query = urllib.parse.urlencode(
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 1,
            "timezone": "auto",
        }
    )
    forecast = _http_get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    return {
        "place": ", ".join(p for p in (place.get("name"), place.get("admin1"), place.get("country")) if p),
        "current": forecast.get("current", {}),
        "today": forecast.get("daily", {}),
        "units": {**forecast.get("current_units", {}), **forecast.get("daily_units", {})},
    }


def _load_notes() -> List[Dict[str, Any]]:
    if not NOTES_PATH.exists():
        return []
    return json.loads(NOTES_PATH.read_text(encoding="utf-8"))


def _store_notes(notes: List[Dict[str, Any]]) -> None:
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTES_PATH.write_text(json.dumps(notes, indent=2), encoding="utf-8")


def save_note(text: str) -> Dict[str, Any]:
    """Append a note to the local notes file."""
    with _notes_lock:
        notes = _load_notes()
        note = {
            "id": max((n["id"] for n in notes), default=0) + 1,
            "text": text,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        notes.append(note)
        _store_notes(notes)
    return {"saved": note}


def list_notes() -> Dict[str, Any]:
    """Return all saved notes."""
    with _notes_lock:
        return {"notes": _load_notes()}


def delete_note(id: int) -> Dict[str, Any]:
    """Delete a note by id."""
    with _notes_lock:
        notes = _load_notes()
        remaining = [n for n in notes if n["id"] != id]
        if len(remaining) == len(notes):
            return {"error": f"No note with id {id}."}
        _store_notes(remaining)
    return {"deleted": id}


TOOL_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "get_current_time": get_current_time,
    "get_weather": get_weather,
    "save_note": save_note,
    "list_notes": list_notes,
    "delete_note": delete_note,
}


async def run_tool(name: str, arguments_json: str) -> str:
    """Run a tool off the event loop and return its result as a JSON string."""
    handler: Callable[..., Dict[str, Any]] | None = TOOL_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool '{name}'."})
    try:
        arguments = json.loads(arguments_json or "{}")
        call: Awaitable[Dict[str, Any]] = asyncio.to_thread(handler, **arguments)
        result = await call
    except Exception as e:  # report failures to the model so it can tell the user
        result = {"error": f"{type(e).__name__}: {e}"}
    return json.dumps(result)
