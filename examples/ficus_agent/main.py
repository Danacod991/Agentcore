"""Ficus voice agent for Amazon Bedrock AgentCore Runtime.

Port of the Ficus starter's ``lambda_function.py`` to an AgentCore Runtime
entrypoint. The Echo Show 11 (AEOS) request/response contract is unchanged:

    request : {"utterance": "Echo, what was our annual revenue?", "dsn": "G0911234567890"}
    response: {"response": "<one or two spoken sentences>"}

Two orchestration modes, selected by environment variables:

* Bedrock Agent (managed RAG + memory, as in the guide's Part A) - set
  ``AGENT_ID`` and ``AGENT_ALIAS_ID``.
* Strands (code-controlled, the guide's "Larch" alternative) - used when
  ``AGENT_ID`` is unset. Claude Sonnet with the voice-friendly instructions,
  a PST date/time tool, a web search tool (Tavily when ``TAVILY_API_KEY`` is
  set, otherwise DuckDuckGo) and, if ``KNOWLEDGE_BASE_ID`` is set, a Knowledge
  Base retrieval tool.

``VOICE_MODE=false`` lifts the one-or-two-sentence limit for text use.
"""

import json
import logging
import os
import re
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext

logger = logging.getLogger("ficus_agent")
logger.setLevel(logging.INFO)

AGENT_ID = os.environ.get("AGENT_ID", "")
AGENT_ALIAS_ID = os.environ.get("AGENT_ALIAS_ID", "")
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-2")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
DEFAULT_SESSION_ID = os.environ.get("DEFAULT_SESSION_ID", "550e7910-e397-41d4-a716-456858440000")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
VOICE_MODE = os.environ.get("VOICE_MODE", "true").lower() not in ("false", "0", "no")

VOICE_INSTRUCTIONS = (
    "You're having a verbal conversation, so make your language clear and easy "
    "to follow when spoken aloud. LIMIT your responses to a sentence or two "
    "ALWAYS. Format responses as continuous text without special characters or "
    "line breaks."
)
TEXT_INSTRUCTIONS = (
    "You are an office assistant. Answer clearly and concisely. When you use "
    "web search results, synthesize them and cite the source URLs you relied on."
)
TOOL_INSTRUCTIONS = (
    " Use the web_search tool for current events, recent trends or anything you "
    "are not sure is up to date, and the date/time tool for the current date."
)

app = BedrockAgentCoreApp()


def clean_utterance(utterance: str) -> str:
    """Strip the 'echo' wake word and surrounding punctuation/whitespace."""
    cleaned = re.sub(r"\becho\b", "", utterance, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[\s,]+|[\s,]+$", "", cleaned)
    return cleaned.strip()


def clean_agent_response(response: str) -> str:
    """Remove newlines/escape sequences and markdown so the response speaks naturally."""
    if not VOICE_MODE:
        return response.strip()
    cleaned = re.sub(r"\\[nrt]", " ", response)
    cleaned = re.sub(r"[*_#`>]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _session_id(context: RequestContext, dsn: str | None) -> str:
    """Prefer the AgentCore runtime session, then the device serial, then a fixed default."""
    candidate = (context.session_id if context else None) or dsn or DEFAULT_SESSION_ID
    # Bedrock Agent session ids allow [0-9a-zA-Z._:-], max 100 chars.
    return re.sub(r"[^0-9a-zA-Z._:-]", "-", candidate)[:100]


# ---------------------------------------------------------------------------
# Mode 1: Bedrock Agent
# ---------------------------------------------------------------------------
_bedrock_agent_runtime = None


def invoke_bedrock_agent(prompt: str, session_id: str) -> str:
    """Invoke the Bedrock Agent and aggregate the streamed response chunks."""
    global _bedrock_agent_runtime
    if _bedrock_agent_runtime is None:
        _bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=BEDROCK_REGION)

    response = _bedrock_agent_runtime.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=prompt,
    )

    completion = ""
    for event in response.get("completion", []):
        chunk = event.get("chunk")
        if chunk and "bytes" in chunk:
            completion += chunk["bytes"].decode("utf-8")
    return completion


# ---------------------------------------------------------------------------
# Mode 2: Strands
# ---------------------------------------------------------------------------
_strands_agents: dict = {}


def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Return [{"title", "url", "content"}] from Tavily if configured, else DuckDuckGo."""
    if TAVILY_API_KEY:
        request = urllib.request.Request(
            "https://api.tavily.com/search",
            data=json.dumps({"query": query, "max_results": max_results}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {TAVILY_API_KEY}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            results = json.loads(response.read()).get("results", [])
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")} for r in results]

    from ddgs import DDGS

    results = DDGS(timeout=10).text(query, max_results=max_results)
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "content": r.get("body", "")} for r in results]


def _build_strands_agent():
    from strands import Agent, tool
    from strands.models import BedrockModel

    @tool
    def current_datetime_pst() -> str:
        """Return the current date and time in Pacific time."""
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        return now.strftime("%A, %B %d, %Y at %I:%M %p %Z")

    @tool
    def web_search(query: str) -> str:
        """Search the web for current information, news and trends.

        Args:
            query: The search query.
        """
        try:
            results = search_web(query)
        except Exception as error:  # noqa: BLE001 - report to the model instead of failing the turn
            logger.warning("Web search failed: %s", error)
            return f"Web search failed: {error}"
        if not results:
            return "No web results found."
        return "\n\n".join(f"{r['title']}\n{r['url']}\n{r['content']}" for r in results)

    tools = [current_datetime_pst, web_search]

    if KNOWLEDGE_BASE_ID:
        kb_client = boto3.client("bedrock-agent-runtime", region_name=BEDROCK_REGION)

        @tool
        def search_office_knowledge(query: str) -> str:
            """Search the office knowledge base (policies, FAQs, product and sales data).

            Args:
                query: What to look up.
            """
            result = kb_client.retrieve(
                knowledgeBaseId=KNOWLEDGE_BASE_ID,
                retrievalQuery={"text": query},
                retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}},
            )
            passages = [r["content"]["text"] for r in result.get("retrievalResults", [])]
            return "\n\n".join(passages) or "No relevant documents found."

        tools.append(search_office_knowledge)

    return Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=BEDROCK_REGION),
        system_prompt=(VOICE_INSTRUCTIONS if VOICE_MODE else TEXT_INSTRUCTIONS) + TOOL_INSTRUCTIONS,
        tools=tools,
        callback_handler=None,
    )


def invoke_strands_agent(prompt: str, session_id: str) -> str:
    """Run the Strands agent, keeping one conversation per session in this microVM."""
    agent = _strands_agents.get(session_id)
    if agent is None:
        agent = _strands_agents[session_id] = _build_strands_agent()
    return str(agent(prompt))


# ---------------------------------------------------------------------------
# AgentCore entrypoint
# ---------------------------------------------------------------------------
@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    """Handle a Ficus device request: {"utterance", "dsn"} -> {"response"}."""
    # Accept the API Gateway proxy shape too, so the same test events work.
    body = payload.get("body", payload) if isinstance(payload, dict) else {}
    if isinstance(body, str):
        body = json.loads(body)

    dsn = body.get("dsn")
    utterance = body.get("utterance") or body.get("prompt")
    if not utterance:
        logger.warning("No utterance provided in request")
        return {"error": "Missing required field: utterance"}

    cleaned_utterance = clean_utterance(utterance)
    session_id = _session_id(context, dsn)
    logger.info("dsn=%s session=%s utterance=%s", dsn, session_id, cleaned_utterance)

    try:
        if AGENT_ID and AGENT_ALIAS_ID:
            agent_response = invoke_bedrock_agent(cleaned_utterance, session_id)
        else:
            agent_response = invoke_strands_agent(cleaned_utterance, session_id)
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code", "Unknown")
        logger.error("AWS service error: %s", error)
        return {"error": f"Bedrock error: {error_code}"}

    cleaned_response = clean_agent_response(agent_response)
    logger.info("Response length: %d characters", len(cleaned_response))
    return {"response": cleaned_response}


if __name__ == "__main__":
    app.run()
