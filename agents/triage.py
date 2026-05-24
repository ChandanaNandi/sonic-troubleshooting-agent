"""Triage specialist: initial hypotheses from the user complaint alone.

The triage specialist is the only one of the four Phase 3 specialists
that does NOT read collector evidence. Its single input is the
natural-language user_complaint recorded on the blackboard at
investigation start; its single output is 1-3 hypotheses about what
kind of network issue this might be, posted via
Blackboard.add_hypothesis so the downstream diagnosis agent sees them
alongside collector evidence and the other specialists' hypotheses.

Each hypothesis claim is prefixed "[triage]" so the diagnosis agent
(and a human reader of the blackboard) can attribute hypotheses to
their source specialist without us extending the blackboard schema.

Ollama call boilerplate (constants, _call_ollama, _parse_hypotheses)
is intentionally duplicated across the four Phase 3 specialists rather
than factored into a shared helper module, to keep this session's
deliverables to the four agent files explicitly listed in the Phase 3
plan and to keep each specialist self-contained.
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
    "You are a network troubleshooting TRIAGE specialist for SONiC. "
    "Read the user's complaint and post 1-3 initial hypotheses about "
    "what kind of network issue this might be. Your hypotheses guide "
    "what specialists pay attention to. You do NOT have access to any "
    "technical evidence; only the user's words. Do not invent specific "
    "facts (no specific IPs, ASNs, interface names) unless they appear "
    "in the user's words.\n"
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
    """Extract (claim, confidence) pairs from HYPOTHESIS:/CONFIDENCE: lines.

    Lines outside the pair format are ignored. A HYPOTHESIS without a
    following CONFIDENCE is dropped; a CONFIDENCE without a preceding
    HYPOTHESIS is ignored. Confidence words map per _CONFIDENCE_MAP;
    anything unrecognized falls back to _DEFAULT_CONFIDENCE (0.5).
    """
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


def produce_triage_hypotheses(blackboard) -> None:
    """Post triage hypotheses to the blackboard from the user complaint alone.

    supporting_evidence is an empty list because triage reads no
    collector output. The claim text is prefixed "[triage]" for
    attribution by the downstream diagnosis agent.
    """
    bb_dict = blackboard.to_dict()
    user_prompt = f"User complaint: {bb_dict['user_complaint']}"
    text = _call_ollama(user_prompt)
    for claim, confidence in _parse_hypotheses(text):
        blackboard.add_hypothesis(
            claim=f"[triage] {claim}",
            confidence=confidence,
            supporting_evidence=[],
        )
