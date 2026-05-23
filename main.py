"""End-to-end runner for Phase 1: inject → collect → diagnose → restore.

Wires together the four building blocks for the single Phase 1 scenario
(Ethernet4 admin down):

    faults.interface_admin_down  - inject / restore the fault
    collectors.sonic_state       - read CONFIG_DB, APP_DB, COUNTERS_DB, syslog
    blackboard.blackboard        - shared evidence container
    agents.diagnosis             - qwen2.5:7b-instruct narrator over evidence

Imports use direct module references (not subprocess shell-out). Repo
root is added to sys.path at the top so the namespace-package imports
resolve regardless of the caller's CWD; no __init__.py files exist by
design (matches agents/diagnosis.py).

stdout / stderr split (so the diagnosis JSON can be piped cleanly):
    stdout = the diagnosis dict as pretty JSON (and nothing else on the
             happy path)
    stderr = section headers, before/after summaries, inject/restore
             messages, errors

CLI:
    python3 main.py              run the full scenario
    python3 main.py --dry-run    verify container + print planned steps;
                                 no mutation, no Ollama call
    python3 main.py --keep-fault inject and diagnose, then leave the
                                 fault in place for manual inspection

A note on `restore`: it is test cleanup for the lab fault we injected,
not autonomous remediation. The agent diagnoses; it does not fix the
network.
"""

import argparse
import contextlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from agents.diagnosis import DiagnosisError, produce_diagnosis
from blackboard.blackboard import Blackboard
from collectors.sonic_state import (
    collect_bgp_summary,
    collect_interface_counters,
    collect_interface_state,
    collect_recent_logs,
)
from faults.interface_admin_down import inject as fault_inject
from faults.interface_admin_down import restore as fault_restore

CONTAINER = "sonic-vs-troubleshoot"
INTERFACE = "Ethernet4"
USER_COMPLAINT = (
    "Ethernet4 stopped passing traffic. Something is wrong, figure it out."
)
APP_DB_PROPAGATION_SLEEP_SECONDS = 1.0


def _eprint(*parts: object) -> None:
    """Print to stderr so stdout stays clean for the diagnosis JSON."""
    print(*parts, file=sys.stderr)


def _call_with_stdout_to_stderr(fn) -> None:
    """Call fn(), capturing its stdout writes and re-emitting to stderr.

    The fault script's inject()/restore() print progress messages to
    stdout for direct CLI use. main.py reserves stdout for the diagnosis
    JSON only, so we redirect those messages here.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    for line in buf.getvalue().splitlines():
        _eprint(f"  {line}")


def is_container_running(name: str) -> bool:
    """Return True if a running container with the given name exists."""
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=5,
    )
    return name in result.stdout.split()


def take_snapshot(interface: str) -> dict[str, dict]:
    """Run all four collectors against the given interface."""
    return {
        "interface_state": collect_interface_state(interface),
        "interface_counters": collect_interface_counters(interface),
        "bgp_summary": collect_bgp_summary(),
        "recent_logs": collect_recent_logs(20),
    }


def _one_line_summary(name: str, data: dict) -> str:
    """Domain-specific one-line summary of a single collector's output."""
    if isinstance(data, dict) and "error" in data:
        return f"error: {data['error']}"
    if name == "interface_state":
        return (
            f"admin_status={data.get('admin_status')} "
            f"oper_status={data.get('oper_status')}"
        )
    if name == "interface_counters":
        return (
            f"rx_packets={data.get('rx_packets')} "
            f"tx_packets={data.get('tx_packets')} "
            f"rx_errors={data.get('rx_errors')} "
            f"tx_errors={data.get('tx_errors')}"
        )
    if name == "bgp_summary":
        return (
            f"bgp_instance_present={data.get('bgp_instance_present')} "
            f"neighbors={len(data.get('neighbors', []))}"
        )
    if name == "recent_logs":
        return f"log_lines={len(data.get('log_lines', []))}"
    return str(data)[:80]


def print_snapshot(snapshot: dict[str, dict], label: str) -> None:
    """Print a one-line per-collector summary to stderr under a section header."""
    _eprint(f"=== {label} ===")
    for name, data in snapshot.items():
        _eprint(f"  {name}: {_one_line_summary(name, data)}")


def _filter_logs_for_interface(logs_data: dict, interface: str) -> dict:
    """Return a recent_logs evidence dict scoped to the scenario.

    Runner-level evidence hygiene for the Phase 1 admin-down scenario.
    Two filters, applied in order:
        1. Keep only lines containing the target interface name. SONiC
           VS syslog is dominated by baseline noise unrelated to the
           scenario (FDB aging, bridge VLAN warnings, PFC counter
           checks on other ports).
        2. Drop lines containing "oper error event:". SONiC VS emits
           a cascade of synthetic hardware-fault events
           (mac_local_fault, mac_remote_fault, fec_sync_loss,
           fec_alignment_loss, high_ser_error, etc.) during admin-down
           transitions on the virtual switch. Those lines are
           literally present in syslog but they are virtual-switch
           artifacts, not real physical faults; passing them makes the
           narrator describe an admin shutdown as a hardware failure.

    If the collector returned an error or an unexpected shape, the
    input is returned unchanged so the agent still sees the failure.
    """
    if not isinstance(logs_data, dict) or "log_lines" not in logs_data:
        return logs_data
    if "error" in logs_data:
        return logs_data
    source = logs_data.get("source", "/var/log/syslog")
    interface_lines = [
        line for line in logs_data.get("log_lines", []) if interface in line
    ]
    filtered = [
        line for line in interface_lines if "oper error event:" not in line
    ]
    return {
        "log_lines": filtered,
        "source": (
            f"{source} filtered for {interface}; "
            f"suppressed SONiC VS synthetic oper-error cascade"
        ),
    }


def run_dry_run() -> None:
    """Print the planned steps without mutating state or calling Ollama."""
    _eprint("=== DRY RUN (no mutation, no Ollama call) ===")
    _eprint("planned steps:")
    _eprint(f"  1. take BEFORE snapshot of {INTERFACE} via 4 collectors")
    _eprint(f"  2. inject admin-down fault on {INTERFACE}")
    _eprint(
        f"  3. sleep {APP_DB_PROPAGATION_SLEEP_SECONDS}s for APP_DB to catch up"
    )
    _eprint(f"  4. take AFTER snapshot of {INTERFACE} via 4 collectors")
    _eprint(f"  5. populate Blackboard with user_complaint + after-evidence")
    _eprint(f"  6. call produce_diagnosis (qwen2.5:7b-instruct via Ollama)")
    _eprint(f"  7. print diagnosis dict as JSON to stdout")
    _eprint(
        f"  8. restore {INTERFACE} admin status (test cleanup, "
        f"not autonomous remediation)"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 1 end-to-end: inject Ethernet4 admin-down on "
            f"{CONTAINER}, run collectors, ask qwen2.5:7b-instruct to "
            "narrate a diagnosis, restore."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify container + print planned steps; no mutation, no Ollama call",
    )
    parser.add_argument(
        "--keep-fault",
        action="store_true",
        help="inject and diagnose, but skip restore so the fault can be "
             "inspected manually afterward",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not is_container_running(CONTAINER):
        _eprint(
            f"error: container {CONTAINER!r} is not running. "
            f"Run ./scripts/bringup.sh first."
        )
        return 2

    if args.dry_run:
        run_dry_run()
        return 0

    injected = False
    exit_code = 0

    try:
        before = take_snapshot(INTERFACE)
        print_snapshot(before, "BEFORE")

        _eprint(f"=== INJECT ===")
        _call_with_stdout_to_stderr(fault_inject)
        injected = True

        # inject() already polls CONFIG_DB to confirm admin_status=down,
        # but APP_DB oper_status is updated downstream by swss/orchagent
        # and can lag by a few hundred ms. A brief sleep gives APP_DB
        # time to reflect the change before the after-snapshot reads it.
        time.sleep(APP_DB_PROPAGATION_SLEEP_SECONDS)

        after = take_snapshot(INTERFACE)
        print_snapshot(after, "AFTER")

        # Filter recent_logs to scenario-relevant lines only before
        # populating the blackboard. See _filter_logs_for_interface for
        # rationale. BEFORE/AFTER snapshot printouts above still show
        # the raw collector counts; only the blackboard / agent sees
        # the filtered view.
        evidence_for_agent = dict(after)
        evidence_for_agent["recent_logs"] = _filter_logs_for_interface(
            after["recent_logs"], INTERFACE
        )

        bb = Blackboard(USER_COMPLAINT)
        for name, data in evidence_for_agent.items():
            bb.add_evidence(name, data)

        _eprint("=== DIAGNOSIS (calling qwen2.5:7b-instruct) ===")
        try:
            diagnosis = produce_diagnosis(bb)
        except DiagnosisError as exc:
            _eprint(f"error: diagnosis failed: {exc}")
            exit_code = 3
        else:
            print(json.dumps(diagnosis, indent=2))

    except Exception as exc:
        _eprint(f"error: unexpected failure: {exc}")
        if exit_code == 0:
            exit_code = 1

    finally:
        if injected and not args.keep_fault:
            _eprint("=== RESTORE (test cleanup, not autonomous remediation) ===")
            try:
                _call_with_stdout_to_stderr(fault_restore)
                final_state = collect_interface_state(INTERFACE)
                _eprint(
                    f"  {INTERFACE} admin_status="
                    f"{final_state.get('admin_status')}"
                )
            except Exception as exc:
                _eprint(f"warn: restore failed: {exc}")
                if exit_code == 0:
                    exit_code = 4
        elif injected and args.keep_fault:
            _eprint("=== KEEPING FAULT (--keep-fault) ===")
            _eprint(
                f"  {INTERFACE} is left admin down. Restore manually with:"
            )
            _eprint("    python3 faults/interface_admin_down.py restore")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
