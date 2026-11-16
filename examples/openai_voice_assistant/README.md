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

## Deploy to AgentCore Runtime

The `Dockerfile` builds an ARM64 image that serves `/ping`, `/invocations` and `/ws` on port 8080,
as AgentCore Runtime expects. Using the [starter toolkit](https://github.com/aws/bedrock-agentcore-starter-toolkit):

```bash
agentcore configure --entrypoint assistant.py
agentcore launch --env OPENAI_API_KEY=sk-...
```

For production, keep the key in AWS Secrets Manager or AgentCore Identity rather than a plain
environment variable. Runtime containers are ephemeral, so point `ASSISTANT_NOTES_PATH` at durable
storage, or swap the notes tools for AgentCore Memory (`bedrock_agentcore.memory`).

## Tests

The tests run the whole bridge against a fake Realtime server, so they need no key or network:

```bash
pytest test_assistant.py
```

## Browser protocol (`/ws`)

| Direction | Message |
| --- | --- |
| client to server | `{"type": "audio", "audio": "<base64 PCM16, 24 kHz mono>"}` and `{"type": "text", "text": "..."}` |
| server to client | `ready`, `audio`, `transcript` (`role`, `delta` or `text`, `final`), `interrupt`, `tool`, `response_done`, `error` |
