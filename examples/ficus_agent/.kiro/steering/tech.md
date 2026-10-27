# Tech

- Python 3.14 (must match the AgentCore runtime's language version; code must stay 3.10+ compatible for local runs).
- `bedrock-agentcore` `BedrockAgentCoreApp`: HTTP protocol, `POST /invocations` and `GET /ping` on port 8080.
- Two orchestration modes chosen by environment variables:
  - Bedrock Agent: `AGENT_ID`, `AGENT_ALIAS_ID`.
  - Strands (default): `MODEL_ID`, optional `KNOWLEDGE_BASE_ID`.
  - `BEDROCK_REGION` (default `us-east-2`) applies to both.
- Add new agent tools as `@tool` functions inside `_build_strands_agent()` in `main.py`.
- New dependencies go in `requirements.txt` and must ship Linux ARM64 wheels.

## Commands

- Run locally: `python main.py`, then
  `curl -s localhost:8080/invocations -H 'Content-Type: application/json' -d '{"prompt":"What time is it?"}'`
- Build the upload zip: `./build_zip.sh` -> `dist/ficus-agent-agentcore.zip`
  (ARM64 wheels, `main.py` at the zip root). Upload it via "Edit runtime" in the
  AgentCore console with entry point `main.py`.
