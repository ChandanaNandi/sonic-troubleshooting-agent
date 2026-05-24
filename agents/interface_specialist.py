"""Interface specialist: hypotheses scoped to interface-layer evidence.

Reads only the interface_state (CONFIG_DB admin / APP_DB oper) and
interface_counters (COUNTERS_DB SAI stats) entries from the blackboard.
Posts hypotheses about admin/oper state, errors, and drops. Other
evidence (BGP, logs) is the responsibility of agents/bgp_specialist.py
and agents/logs_specialist.py respectively.

Each hypothesis claim is prefixed "[interface]" so the diagnosis agent
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
    "You are an INTERFACE-LAYER specialist for SONiC switches. "
    "Read the interface_state and interface_counters evidence and post "
    "hypotheses ONLY about interface-layer issues "
    "(admin/oper state, errors, drops). "
    "Ignore BGP, routing, and logs. "
    "If the evidence does not support an interface-layer hypothesis, "
    "post one hypothesis with low confidence saying so.\n"
    "\n"
    "Ground every claim in specific fields from the evidence. When you "
    "reference a value, quote it (for example: admin_status=\"down\").\n"
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


def produce_interface_hypotheses(blackboard) -> None:
    """Post interface-layer hypotheses to the blackboard.

    Reads interface_state and interface_counters; ignores other
    evidence keys. supporting_evidence is the list of the two
    collector names whose data this specialist read. Claim text is
    prefixed "[interface]" for attribution.
    """
    bb_dict = blackboard.to_dict()
    evidence = bb_dict["evidence"]
    interface_state = evidence.get("interface_state", {})
    interface_counters = evidence.get("interface_counters", {})
    user_prompt = (
        f"User complaint: {bb_dict['user_complaint']}\n"
        "\n"
        "interface_state evidence:\n"
        f"{json.dumps(interface_state, indent=2)}\n"
        "\n"
        "interface_counters evidence:\n"
        f"{json.dumps(interface_counters, indent=2)}"
    )
    text = _call_ollama(user_prompt)
    for claim, confidence in _parse_hypotheses(text):
        blackboard.add_hypothesis(
            claim=f"[interface] {claim}",
            confidence=confidence,
            supporting_evidence=["interface_state", "interface_counters"],
        )
