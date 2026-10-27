# Structure

- `main.py`: the whole agent (entrypoint, utterance/response cleanup, both orchestration modes).
- `requirements.txt`: runtime dependencies bundled into the zip.
- `build_zip.sh`: builds `dist/ficus-agent-agentcore.zip` for AgentCore direct code deployment.
- `build/`, `dist/`: build outputs, git-ignored; never edit or commit them.
