# Personal voice assistant (OpenAI Realtime + Bedrock AgentCore)

A speech-to-speech personal assistant that runs on Bedrock AgentCore Runtime and uses the
[OpenAI Realtime API](https://platform.openai.com/docs/guides/realtime) for low-latency voice.
You talk to it from the browser, interrupt it mid-sentence, and it can call tools for you.

```
Browser (mic + speaker) ──/ws──▶ AgentCore app ──wss──▶ OpenAI Realtime (gpt-realtime)
                         ◀──────  runs tools   ◀──────
```

The browser never sees your OpenAI key. The server opens the Realtime session, sets the voice,
instructions and tools, relays audio both ways, and runs tool calls itself.

## Features

- Natural turn-taking with semantic voice activity detection, and barge-in: start talking and it stops speaking.
- Live transcripts of both sides, plus a text box for typing instead of talking.
- Tools:
  - `get_current_time`: the time in any timezone.
  - `get_weather`: current weather and today's forecast, from Open-Meteo (no key needed).
  - `save_note`, `list_notes` and `delete_note`: personal memory kept in a JSON file.
- Text mode on `POST /invocations`, so `agentcore invoke` and scripts work too.
- Panels for:
  - **Settings:** choose the assistant's name and voice. These are saved per device.
  - **Notes:** see, add and delete what the assistant remembers about you.
  - **History:** past conversations, saved in the browser. Open one to reread it or to continue it;
    continuing replays the earlier turns so the assistant has the context. Reopening the mic within
    30 minutes continues the current conversation automatically.
- Installable on your phone's home screen (a PWA) with its own icon and a full-screen layout.
- Optional password, so you can put it on a public URL without others spending your OpenAI credit.

## Run locally

```bash
cd examples/openai_voice_assistant
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
python assistant.py
```

Open http://localhost:8080, click the mic, and say "Hi! What's the weather in Seattle?"
Browsers only allow the microphone on `localhost` or HTTPS.

Text mode:

```bash
curl -s localhost:8080/invocations -H 'content-type: application/json' \
  -d '{"prompt": "Remember that I take my coffee black"}'
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | (required) | Your OpenAI API key |
| `APP_PASSWORD` | (off) | Password for the web app. Always set it on a public URL |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime` | Realtime model |
| `OPENAI_VOICE` | `marin` | Voice (for example `cedar`, `alloy`, `verse`) |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | Model for the user-side transcripts |
| `ASSISTANT_NAME` | `Nova` | What the assistant calls itself |
| `ASSISTANT_NOTES_PATH` | `~/.voice_assistant_notes.json` | Where notes are stored |
| `PORT` | `8080` | Local port |

To change the personality, edit `instructions()` in `realtime.py`.

## Add a tool

Add a JSON schema to `TOOL_SCHEMAS` and a function to `TOOL_HANDLERS` in `tools.py`.
Handlers are plain synchronous functions. They run in a worker thread and return a dict, which is
sent back to the model as JSON. Errors are reported to the model so it can explain them to the user.

## Deploy as a web app (no AWS needed)

The app is a single Docker container, so any host that runs containers and supports WebSockets works.

### Render (one click)

`render.yaml` at the repository root is a Render Blueprint.

1. Push this repository to GitHub.
2. In the Render dashboard choose **New → Blueprint** and pick the repository.
3. When asked, enter `OPENAI_API_KEY` and an `APP_PASSWORD` of your choice.
4. Open the `https://voice-assistant-….onrender.com` URL Render gives you and sign in.

Render's free plan sleeps after 15 minutes without traffic, so the first visit after a break takes
about a minute to load. Free services have no persistent disk, so notes reset on each deploy. For
lasting notes, use a paid plan and add a disk mounted at `/data`.

### Fly.io

```bash
cd examples/openai_voice_assistant
fly launch --no-deploy
fly secrets set OPENAI_API_KEY=sk-... APP_PASSWORD=choose-one
fly volumes create data --size 1
fly deploy
```

To keep notes, add a `[mounts]` section with `source = "data"` and `destination = "/data"` to the
generated `fly.toml` before deploying.

### Railway or any other container host

Point the host at `examples/openai_voice_assistant/Dockerfile`. Set `OPENAI_API_KEY` and
`APP_PASSWORD`. The app listens on `$PORT` (default 8080) and its health check is `/ping`.

## Install it on your phone

Open your deployed URL (it must be HTTPS) and sign in, then:

- **iPhone (Safari):** tap Share, then **Add to Home Screen**.
- **Android (Chrome):** tap ⋮, then **Install app** or **Add to Home screen**.

It opens full screen like a native app and stays signed in for 90 days. Allow microphone access
the first time you tap the mic. The mic turns off when you leave the app.

## Deploy to AgentCore Runtime

The `Dockerfile` serves `/ping`, `/invocations` and `/ws` on port 8080, as AgentCore Runtime expects.
AgentCore needs an ARM64 image. Leave `APP_PASSWORD` unset there, because AgentCore authenticates
callers itself. Using the [starter toolkit](https://github.com/aws/bedrock-agentcore-starter-toolkit):

```bash
agentcore configure --entrypoint assistant.py
agentcore launch --env OPENAI_API_KEY=sk-...
```

For production on AWS, keep the key in AWS Secrets Manager or AgentCore Identity rather than a plain
environment variable. Runtime containers are ephemeral, so point `ASSISTANT_NOTES_PATH` at durable
storage, or swap the notes tools for AgentCore Memory (`bedrock_agentcore.memory`).

## How sign-in works

When `APP_PASSWORD` is set, browsers sign in once at `/login` and get an HttpOnly cookie derived
from the password. Changing the password signs everyone out. Scripts can send
`Authorization: Bearer <APP_PASSWORD>` instead. The health check, login page, icons and manifest
stay public so phones can show the install prompt. Everything else, including the `/ws` voice
connection, needs the cookie.

## Tests

The tests run the whole bridge against a fake Realtime server, so they need no key or network:

```bash
pytest test_assistant.py
```

## HTTP API used by the panels

| Endpoint | Purpose |
| --- | --- |
| `GET /api/config` | Available voices plus the default voice and name |
| `GET /api/notes` | List saved notes |
| `POST /api/notes` with `{"text": "..."}` | Add a note |
| `DELETE /api/notes/{id}` | Delete a note |

## Browser protocol (`/ws`)

| Direction | Message |
| --- | --- |
| connect | `/ws?voice=<voice>&name=<assistant name>`. Both are optional and fall back to the server defaults |
| client to server | `{"type": "audio", "audio": "<base64 PCM16, 24 kHz mono>"}`, `{"type": "text", "text": "..."}` and `{"type": "history", "messages": [{"role": "user", "text": "..."}]}` |
| server to client | `ready`, `audio`, `transcript` (`role`, `delta` or `text`, `final`), `interrupt`, `tool`, `response_done`, `error` |
