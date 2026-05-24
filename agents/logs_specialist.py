"""Logs specialist: hypotheses scoped to recent_logs evidence.

Reads only the recent_logs entry from the blackboard. Posts hypotheses
about what the log lines suggest. Does NOT speculate beyond what the
log lines actually say. If the log_lines list is empty (or has been
filtered down to nothing by main.py's scenario evidence filter), the
specialist posts one low-confidence hypothesis to that effect.

Each hypothesis claim is prefixed "[logs]" so the diagnosis agent
(and a human reader of the blackboard) can attribute hypotheses to
their source specialist without us extending the blackboard schema.

Ollama call boilerplate is duplicated across the four Phase 3
specialists rather than factored into a shared module — see the
matching header in agents/triage.py for the rationale.
"""

import json
import urllib.error
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b-instruct"
TEMPERATURE = 0.2
REQUEST_TIMEOUT_SECONDS = 60

_CONFIDENCE_MAP = {"high": 0.8, "medium": 0.5, "low": 0.2}
_DEFAULT_CONFIDENCE = 0.5

SYSTEM_PROMPT = (
    "You are a LOGS specialist for SONiC switches. "
    "Read the recent_logs evidence and post hypotheses ONLY about "
    "what the log lines suggest. If the log_lines list is empty, "
    "post one hypothesis with low confidence saying no log evidence "
    "is available. Do NOT speculate about issues the logs do not "
    "mention.\n"
    "\n"
    "Ground every claim in specific log content. If you reference a "
    "log line, quote a recognizable substring from it.\n"
    "\n"
    "Format each hypothesis on its own line as:\n"
    "HYPOTHESIS: <one-sentence claim>\n"
    "CONFIDENCE: <high|medium|low>\n"
    "Do not write anything else. No preamble. No explanation."
)


class SpecialistError(Exception):
    """Raised on Ollama HTTP failure or unparseable response."""


def _call_ollama(user_prompt: str) -> str:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": TEMPERATURE},
    }
    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise SpecialistError(
            f"Ollama returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SpecialistError(
            f"could not reach Ollama at {OLLAMA_URL}: {exc.reason}; "
            f"is `ollama serve` running?"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecialistError(
            f"Ollama response was not valid JSON: {exc}"
        ) from exc
    message = data.get("message") if isinstance(data, dict) else None
    text = message.get("content", "") if isinstance(message, dict) else ""
    if not text.strip():
        raise SpecialistError(
            "Ollama response had no message.content; "
            f"raw (truncated): {raw[:200]!r}"
        )
    return text


def _parse_hypotheses(text: str) -> list[tuple[str, float]]:
    """Extract (claim, confidence) pairs from HYPOTHESIS:/CONFIDENCE: lines."""
    pairs: list[tuple[str, float]] = []
    current_claim: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        upper = line.upper()
        if upper.startswith("HYPOTHESIS:"):
            claim = line.split(":", 1)[1].strip()
            current_claim = claim if claim else None
        elif upper.startswith("CONFIDENCE:") and current_claim is not None:
            word = line.split(":", 1)[1].strip().lower()
            confidence = _CONFIDENCE_MAP.get(word, _DEFAULT_CONFIDENCE)
            pairs.append((current_claim, confidence))
            current_claim = None
    return pairs


def produce_logs_hypotheses(blackboard) -> None:
    """Post logs-derived hypotheses to the blackboard.

    Reads recent_logs only. supporting_evidence is ["recent_logs"].
    Claim text is prefixed "[logs]" for attribution.
    """
    bb_dict = blackboard.to_dict()
    evidence = bb_dict["evidence"]
    recent_logs = evidence.get("recent_logs", {})
    user_prompt = (
        f"User complaint: {bb_dict['user_complaint']}\n"
        "\n"
        "recent_logs evidence:\n"
        f"{json.dumps(recent_logs, indent=2)}"
    )
    text = _call_ollama(user_prompt)
    for claim, confidence in _parse_hypotheses(text):
        blackboard.add_hypothesis(
            claim=f"[logs] {claim}",
            confidence=confidence,
            supporting_evidence=["recent_logs"],
        )
