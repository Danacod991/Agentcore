# Ficus voice agent on AgentCore Runtime

This hosts the Ficus (Echo Show 11) voice agent on **Amazon Bedrock AgentCore Runtime**
instead of the starter's API Gateway + Lambda stack. `main.py` is a port of the
starter's `lambda_function.py`, and the device contract stays the same:

```
POST {"utterance": "Echo, what was our annual revenue?", "dsn": "G0911234567890"}
  -> {"response": "Our annual revenue was ..."}
```

Every reply has a speakable `response`. Failures also include an `error` code, for
example `{"response": "Sorry, something went wrong on my end. Please try again.",
"error": "Internal error: RuntimeError"}`.

## Build the zip

```bash
./build_zip.sh          # Python 3.14 by default; ./build_zip.sh 3.12 for another runtime
# -> dist/ficus-agent-agentcore.zip
```

The zip has `main.py` and all dependencies at its root. The dependencies are Linux
ARM64 (`manylinux2014_aarch64`) wheels, which is the format AgentCore direct code
deployment needs. You need `uv` to build it.

## Upload to AgentCore

1. Upload the zip to an S3 bucket in your Region, for example `us-east-2`.
2. Open the AgentCore console, go to **Runtime**, choose **Host agent**, and select
   **Code upload / S3**. Then set:
   - **Runtime:** Python 3.14 (it must match the build).
   - **Entrypoint:** `main.py`
   - **Environment variables:** pick one mode.
     - **Bedrock Agent mode** (guide Part A): set `AGENT_ID`, `AGENT_ALIAS_ID` and
       `BEDROCK_REGION`.
     - **Strands mode** (default when `AGENT_ID` is unset): you can set
       `KNOWLEDGE_BASE_ID`, `BEDROCK_REGION`, `TIMEZONE` (default
       `America/New_York`) and `TAVILY_API_KEY` (web search otherwise uses
       DuckDuckGo).
     - **Voice vs text:** voice mode is the default. It uses `VOICE_MODEL_ID`
       (default Claude Haiku 4.5) and short, speech-ready answers.
       `VOICE_MODE=false` switches to `TEXT_MODEL_ID` (default Claude Sonnet
       4.5) with longer, formatted answers. `MODEL_ID` overrides both.
3. **Execution role:** start from the AgentCore Runtime default role. Then add:
   - `bedrock:InvokeAgent` on the agent and its alias (Bedrock Agent mode), or
   - `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on both
     the Haiku and Sonnet models and inference profiles, plus `bedrock:Retrieve`
     on the Knowledge Base (Strands mode).
4. **Inbound auth:** the device sends `Authorization: Bearer <token>`. Choose a
   **JWT authorizer** with your IdP's discovery URL and allowed audience or
   clients (Cognito, Okta or Auth0). This replaces the starter's
   `authorizer.py` stub and gives you real signature and expiry checks.

## Point the device at it

In **Ficus Device Management**, use **Set Agent Endpoint** with this URL and your
OAuth Bearer token:

```
https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded-runtime-arn>/invocations?qualifier=DEFAULT
```

Test it with curl. The session header is optional. It must be at least 33 characters
and keeps a multi-turn conversation together:

```bash
curl -X POST "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $(uuidgen)-ficus" \
  -d '{"utterance":"Echo, what was our annual revenue?","dsn":"G0911234567890"}'
```

## Run locally

```bash
pip install -r requirements.txt && python main.py
curl -s localhost:8080/invocations -H 'Content-Type: application/json' \
  -d '{"utterance":"Echo, what time is it?","dsn":"LOCAL"}'
```
