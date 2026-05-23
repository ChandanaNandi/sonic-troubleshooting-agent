"""Diagnosis agent: qwen2.5:7b-instruct narrates a Blackboard's evidence.

The model is a NARRATOR over structured evidence that Python has already
collected. It is NOT the investigation brain. Python decides what to
put on the blackboard (collectors, fault scripts, future schedulers);
this function just renders the findings into natural language and
returns the model's text verbatim alongside audit metadata.

HTTP client choice: stdlib urllib.request, not the `ollama` package
that project 1 uses. Reasons:
- Zero new dependencies for project 2 at this phase (no requirements.txt
  or venv yet).
- We only need /api/chat with a single non-streaming message; urllib
  handles it in a few lines.
- Project 1 picked the `ollama` package because it wraps tool-calling,
  which Phase 1 of project 2 does not use. We can switch to the package
  later if a phase needs richer features.

Preconditions:
- Ollama is running locally on http://localhost:11434.
- The qwen2.5:7b-instruct model has been pulled (`ollama pull
  qwen2.5:7b-instruct`).

Run standalone smoke test from the repo root:
    python3 agents/diagnosis.py
"""

import json
import urllib.error
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b-instruct"
TEMPERATURE = 0.2
REQUEST_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = (
    "You are a network troubleshooting NARRATOR for SONiC switches. "
    "Your role is to read structured evidence that has ALREADY been "
    "collected by Python tooling and explain what it indicates about "
    "the user's complaint.\n"
    "\n"
    "You are NOT an investigator and NOT a remediation engine:\n"
    "- Do not recommend additional commands, checks, or remediation "
    "steps, even when the obvious fix would be a single command.\n"
    "- Do not invent facts that are not directly supported by the "
    "evidence dict you were given.\n"
    "- If the evidence is insufficient to explain the complaint, say so "
    "using the phrase \"insufficient evidence\" and identify at a high "
    "level which evidence is missing (for example: \"no neighbor "
    "reachability data was provided\"). Do NOT instruct the user what "
    "to run next.\n"
    "\n"
    "Ground every claim in specific fields from the evidence. When you "
    "reference a value, quote it (for example: admin_status=\"down\"). "
    "Keep the narrative concise: 2-5 sentences typically, longer only "
    "when the evidence genuinely supports it."
)


class DiagnosisError(Exception):
    """Raised on Ollama HTTP failure or unparseable response."""


def _build_evidence_summary(evidence: dict) -> str:
    """One-line Python-side audit: which collectors returned data vs error.

    Insertion order is preserved (Python 3.7+ dict semantics) so the
    summary mirrors the order the caller added evidence in.
    """
    if not evidence:
        return "no evidence"
    parts: list[str] = []
    for name, data in evidence.items():
        status = "error" if isinstance(data, dict) and "error" in data else "ok"
        parts.append(f"{name} {status}")
    return ", ".join(parts)


def _build_user_prompt(blackboard) -> str:
    """Render the blackboard's contents into the user-message text."""
    bb_dict = blackboard.to_dict()
    parts: list[str] = [
        f"User complaint: {bb_dict['user_complaint']}",
        "",
        "Evidence (structured collector output):",
        json.dumps(bb_dict["evidence"], indent=2),
    ]
    if bb_dict.get("hypotheses"):
        parts.extend([
            "",
            "Candidate hypotheses (with confidence and supporting evidence keys):",
            json.dumps(bb_dict["hypotheses"], indent=2),
        ])
    return "\n".join(parts)


def produce_diagnosis(blackboard) -> dict:
    """Ask qwen2.5:7b-instruct to narrate a diagnosis from blackboard evidence.

    The model is a NARRATOR: it reads structured evidence (already
    collected by Python) and explains what it means. It is NOT the
    investigation brain. Python decided what to collect; this function
    just renders the findings into natural language and returns the
    model's text verbatim — no claim parsing, no Python post-processing
    of the diagnosis content.

    Returns:
        {
          "diagnosis": "...natural language explanation, model text verbatim...",
          "model": "qwen2.5:7b-instruct",
          "evidence_summary": "interface_state ok, bgp_summary ok, ...",
          "raw_response": {...full Ollama JSON response...}
        }

    Raises:
        DiagnosisError on Ollama HTTP failure or unparseable response.
    """
    evidence = blackboard.get_evidence()
    evidence_summary = _build_evidence_summary(evidence)
    user_prompt = _build_user_prompt(blackboard)

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
        raise DiagnosisError(
            f"Ollama returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DiagnosisError(
            f"could not reach Ollama at {OLLAMA_URL}: {exc.reason}; "
            f"is `ollama serve` running?"
        ) from exc

    try:
        response_data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiagnosisError(
            f"Ollama response was not valid JSON: {exc}"
        ) from exc

    message = response_data.get("message") if isinstance(response_data, dict) else None
    diagnosis_text = (
        message.get("content", "") if isinstance(message, dict) else ""
    )
    if not diagnosis_text.strip():
        raise DiagnosisError(
            "Ollama response had no message.content; "
            f"raw (truncated): {raw[:200]!r}"
        )

    return {
        "diagnosis": diagnosis_text,
        "model": MODEL,
        "evidence_summary": evidence_summary,
        "raw_response": response_data,
    }


if __name__ == "__main__":
    # Minimal sys.path adjustment so this script can be run directly from
    # the repo root (`python3 agents/diagnosis.py`) without adding
    # __init__.py packaging just for the smoke test. Guarded under
    # __main__ so it doesn't affect normal imports of this module.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from blackboard.blackboard import Blackboard

    bb = Blackboard("Ethernet4 stopped passing traffic")
    bb.add_evidence("interface_state", {
        "interface": "Ethernet4",
        "admin_status": "down",
        "oper_status": "down",
        "source": "CONFIG_DB and APP_DB",
    })
    bb.add_evidence("interface_counters", {
        "interface": "Ethernet4",
        "rx_packets": 0,
        "tx_packets": 0,
        "rx_errors": 0,
        "tx_errors": 0,
        "rx_discards": 0,
        "tx_discards": 0,
        "source": (
            "COUNTERS_DB (oid present but COUNTERS hash empty; "
            "flex_counter has not populated stats on this SONiC VS)"
        ),
    })
    bb.add_evidence("bgp_summary", {
        "bgp_instance_present": False,
        "neighbors": [],
        "source": "vtysh show bgp summary json",
    })
    bb.add_evidence("recent_logs", {
        "log_lines": [],
        "source": "/var/log/syslog",
    })

    result = produce_diagnosis(bb)
    print(json.dumps(result, indent=2))
