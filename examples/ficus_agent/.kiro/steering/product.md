# Product

A voice agent for an office, hosted on Amazon Bedrock AgentCore Runtime. It is
used directly (console, CLI, boto3) today and is planned to sit behind an Echo
Show 11 in Ficus enterprise mode later.

- Request: `{"prompt": "..."}` or `{"utterance": "Echo, ...", "dsn": "<device serial>"}`
- Response: `{"response": "<one or two spoken sentences>"}`
- Replies are spoken aloud: keep them to a sentence or two, plain text, no
  markdown, lists or line breaks.
- Keep this request/response contract stable; the Echo device depends on it.
