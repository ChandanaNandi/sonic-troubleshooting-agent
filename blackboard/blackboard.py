"""Blackboard: shared state for a single troubleshooting investigation.

A plain Python container — no LLM, no agents, no scheduling. Holds four
sections:
    user_complaint  the original problem description (str)
    evidence        dict keyed by collector name; values are collector dicts
    hypotheses      list of {claim, confidence, supporting_evidence} entries
    diagnosis       final synthesized diagnosis (str or dict), set once

Mutation is explicit through add_*/set_* methods. Both writes and reads
go through defensive deep copies: add_evidence/set_diagnosis snapshot
their inputs so the caller can keep mutating their own objects, and
get_evidence/get_hypotheses/to_dict return fully owned copies so callers
can't touch the audit trail through the returned values either. The
only way to modify a Blackboard's contents is by calling its methods.

The diagnosis agent (Phase 1 piece 4) will write here through these
methods, and the end-to-end runner (piece 5) will serialize the finished
blackboard via to_json().

Run standalone smoke test:
    python3 blackboard/blackboard.py
"""

import copy
import json


class Blackboard:
    """Shared state for a single troubleshooting investigation."""

    def __init__(self, user_complaint: str) -> None:
        if not isinstance(user_complaint, str) or not user_complaint.strip():
            raise ValueError("user_complaint must be a non-empty string")
        self._user_complaint = user_complaint
        self._evidence: dict[str, dict] = {}
        self._hypotheses: list[dict] = []
        self._diagnosis: str | dict | None = None

    def add_evidence(self, collector_name: str, data: dict) -> None:
        """Store a collector's output under the given name.

        Last-write-wins: re-adding the same collector_name overwrites the
        previous entry. This is deliberate — collectors may be re-run
        during an investigation and the latest reading is what matters.
        """
        if not isinstance(collector_name, str) or not collector_name.strip():
            raise ValueError("collector_name must be a non-empty string")
        if not isinstance(data, dict):
            raise ValueError(
                f"data must be a dict, got {type(data).__name__}"
            )
        self._evidence[collector_name] = copy.deepcopy(data)

    def add_hypothesis(
        self,
        claim: str,
        confidence: float,
        supporting_evidence: list[str],
    ) -> None:
        """Append a candidate diagnosis with confidence and supporting evidence.

        confidence must be in [0.0, 1.0]. supporting_evidence is a list of
        collector names whose data backs the claim; the names should match
        keys previously passed to add_evidence (not enforced here so a
        hypothesis can be recorded before its backing collector runs).
        """
        if not isinstance(claim, str) or not claim.strip():
            raise ValueError("claim must be a non-empty string")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ValueError(
                f"confidence must be a number, got {type(confidence).__name__}"
            )
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {confidence}"
            )
        if not isinstance(supporting_evidence, list) or not all(
            isinstance(item, str) for item in supporting_evidence
        ):
            raise ValueError(
                "supporting_evidence must be a list of strings"
            )
        self._hypotheses.append({
            "claim": claim,
            "confidence": float(confidence),
            "supporting_evidence": list(supporting_evidence),
        })

    def set_diagnosis(self, diagnosis) -> None:
        """Set the final diagnosis. Raises ValueError if called twice.

        Set-once is enforced so the audit trail can't be silently
        rewritten after a downstream consumer has already read the
        diagnosis.
        """
        if self._diagnosis is not None:
            raise ValueError(
                "diagnosis already set; Blackboard.set_diagnosis is set-once"
            )
        if not isinstance(diagnosis, (str, dict)):
            raise ValueError(
                f"diagnosis must be str or dict, got {type(diagnosis).__name__}"
            )
        if isinstance(diagnosis, str) and not diagnosis.strip():
            raise ValueError("diagnosis string must be non-empty")
        # deepcopy is a no-op on immutable str; the meaningful case is dict.
        self._diagnosis = copy.deepcopy(diagnosis)

    def get_evidence(self) -> dict:
        """Return a defensive deep copy of the evidence dict.

        The returned dict and every nested value are fully owned by the
        caller; mutating them has no effect on the blackboard.
        """
        return copy.deepcopy(self._evidence)

    def get_hypotheses(self) -> list:
        """Return a defensive deep copy of the hypotheses list.

        Each hypothesis entry and its supporting_evidence list are
        independent copies; mutating them has no effect on the blackboard.
        """
        return copy.deepcopy(self._hypotheses)

    def to_dict(self) -> dict:
        """Return the full blackboard state as a plain dict for serialization.

        Evidence, hypotheses, and diagnosis are deep-copied so the caller
        can freely mutate or serialize the returned dict without
        affecting the blackboard.
        """
        return {
            "user_complaint": self._user_complaint,
            "evidence": copy.deepcopy(self._evidence),
            "hypotheses": copy.deepcopy(self._hypotheses),
            "diagnosis": copy.deepcopy(self._diagnosis),
        }

    def to_json(self, indent: int = 2) -> str:
        """Return to_dict() rendered as a JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


if __name__ == "__main__":
    bb = Blackboard("Cannot reach 10.0.0.5 from Ethernet4")

    bb.add_evidence("interface_state", {
        "interface": "Ethernet4",
        "admin_status": "down",
        "oper_status": "down",
        "source": "CONFIG_DB and APP_DB",
    })
    bb.add_evidence("bgp_summary", {
        "bgp_instance_present": False,
        "neighbors": [],
        "source": "vtysh show bgp summary json",
    })

    bb.add_hypothesis(
        claim="Ethernet4 is administratively shut down",
        confidence=0.95,
        supporting_evidence=["interface_state"],
    )
    bb.add_hypothesis(
        claim="No BGP session is available to provide reachability",
        confidence=0.40,
        supporting_evidence=["bgp_summary"],
    )

    bb.set_diagnosis(
        "Ethernet4 admin_status=down. Restore with "
        "`config interface startup Ethernet4`."
    )

    print(bb.to_json())
    print()

    # Verify deep-copy on write: mutating the caller's evidence dict
    # after add_evidence does not affect what the blackboard stores.
    bb_iso = Blackboard("deep-copy isolation test")
    external: dict[str, object] = {"interface": "Ethernet8", "admin_status": "up"}
    bb_iso.add_evidence("interface_state", external)
    external["admin_status"] = "MUTATED_BY_CALLER"
    external["injected"] = True
    stored = bb_iso.get_evidence()["interface_state"]
    assert stored == {"interface": "Ethernet8", "admin_status": "up"}, (
        f"deep-copy on add_evidence failed: stored={stored}"
    )
    print("deep-copy on add_evidence verified")

    # Verify deep-copy on read: mutating the dict returned by get_evidence
    # does not affect what the blackboard stores.
    snapshot = bb_iso.get_evidence()
    snapshot["interface_state"]["admin_status"] = "MUTATED_VIA_READ"
    snapshot["injected_collector"] = {"injected": True}
    fresh = bb_iso.get_evidence()
    assert fresh == {
        "interface_state": {"interface": "Ethernet8", "admin_status": "up"}
    }, f"deep-copy on get_evidence failed: fresh={fresh}"
    print("deep-copy on get_evidence verified")

    # Verify set-once enforcement on set_diagnosis
    try:
        bb.set_diagnosis("attempt to overwrite")
    except ValueError as exc:
        print(f"set_diagnosis set-once verified: {exc}")
    else:
        raise AssertionError("set_diagnosis should have raised on second call")
