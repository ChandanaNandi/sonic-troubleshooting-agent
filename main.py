"""End-to-end runner: scenario dispatch.

Wires together the building blocks for any registered fault scenario:

    faults.<scenario>            - inject / restore the fault
    collectors.sonic_state       - read CONFIG_DB, APP_DB, COUNTERS_DB,
                                   vtysh, syslog
    blackboard.blackboard        - shared evidence container
    agents.diagnosis             - qwen2.5:7b-instruct narrator over evidence

The scenario is selected at the command line with --scenario. There is
no silent default; running `python3 main.py` with no flag prints
argparse usage and exits non-zero.

Registered scenarios live in the SCENARIOS dict below as Scenario
dataclass entries. Adding a new scenario means: implement
faults/<name>.py with inject/restore, then add a Scenario entry here.
Two scenarios are registered today:

    interface_admin_down   - Phase 1 baseline (single-container)
    bgp_neighbor_removal   - Phase 2C (two-container BGP lab)

BGP scenarios set requires_bgp_lab=True. The runner calls
scripts/configure_bgp.sh up before BEFORE-snapshot, and
scripts/configure_bgp.sh down after restore (unless --keep-fault).
This is lab fixture management, NOT autonomous remediation. The
diagnosis agent never sees fixture-management evidence; it only
sees what the collectors observe on sonic-vs-troubleshoot.

stdout / stderr split (so the diagnosis JSON can be piped cleanly):
    stdout = the diagnosis dict as pretty JSON (and nothing else on the
             happy path)
    stderr = section headers, before/after summaries, inject/restore
             messages, BGP lab setup/cleanup messages, errors

CLI:
    python3 main.py --scenario <name>              run the full scenario
    python3 main.py --scenario <name> --dry-run    plan only; no mutation, no Ollama
    python3 main.py --scenario <name> --keep-fault inject + diagnose; skip
                                                   restore and skip BGP lab down

A note on `restore` and `scripts/configure_bgp.sh down`: both are test
cleanup for lab state we put in place. They are not autonomous
remediation of a real network. The diagnosis agent diagnoses; it does
not fix the network.
"""

import argparse
import contextlib
import io
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from agents.bgp_specialist import produce_bgp_hypotheses
from agents.diagnosis import DiagnosisError, produce_diagnosis
from agents.interface_specialist import produce_interface_hypotheses
from agents.logs_specialist import produce_logs_hypotheses
from agents.triage import produce_triage_hypotheses
from blackboard.blackboard import Blackboard
from collectors.sonic_state import (
    collect_bgp_summary,
    collect_interface_counters,
    collect_interface_state,
    collect_recent_logs,
)
from faults import bgp_asn_mismatch, bgp_neighbor_removal, interface_admin_down

CONTAINER = "sonic-vs-troubleshoot"
DEFAULT_INTERFACE = "Ethernet4"
CONFIGURE_BGP_SCRIPT = REPO_ROOT / "scripts" / "configure_bgp.sh"
CONFIGURE_BGP_TIMEOUT_SECONDS = 180


def _eprint(*parts: object) -> None:
    """Print to stderr so stdout stays clean for the diagnosis JSON."""
    print(*parts, file=sys.stderr)


def _call_with_stdout_to_stderr(fn) -> None:
    """Call fn(), capturing its stdout writes and re-emitting to stderr.

    The fault scripts print progress messages to stdout for direct CLI
    use. main.py reserves stdout for the diagnosis JSON only, so we
    redirect those messages here.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    for line in buf.getvalue().splitlines():
        _eprint(f"  {line}")


def _run_configure_bgp(action: str) -> None:
    """Run scripts/configure_bgp.sh <action>, forwarding its output to stderr.

    Raises RuntimeError on non-zero exit so the caller can decide
    whether to proceed or bail.
    """
    result = subprocess.run(
        [str(CONFIGURE_BGP_SCRIPT), action],
        capture_output=True,
        text=True,
        timeout=CONFIGURE_BGP_TIMEOUT_SECONDS,
    )
    for line in result.stdout.splitlines():
        _eprint(f"  {line}")
    for line in result.stderr.splitlines():
        _eprint(f"  {line}")
    if result.returncode != 0:
        raise RuntimeError(
            f"scripts/configure_bgp.sh {action} exited "
            f"{result.returncode}"
        )


def is_container_running(name: str) -> bool:
    """Return True if a running container with the given name exists."""
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=5,
    )
    return name in result.stdout.split()


def take_snapshot(interface: str) -> dict[str, dict]:
    """Run all four collectors. Per-port collectors get the given interface."""
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
    """Return a recent_logs evidence dict scoped to the admin-down scenario.

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


def _admin_down_evidence_filter(
    snapshot: dict[str, dict], interface: str
) -> dict[str, dict]:
    """Evidence filter for interface_admin_down: scope recent_logs to the
    given interface and suppress the synthetic oper-error cascade.

    Per-scenario filters wrap the snapshot-level mutation so the runner's
    main loop stays generic. The interface is passed in from the caller
    (sourced from Scenario.interface) so the filter does not have to
    hardcode any specific port. BEFORE/AFTER stderr summaries still see
    the raw collector output; only the blackboard / agent sees the
    filtered view.
    """
    filtered = dict(snapshot)
    filtered["recent_logs"] = _filter_logs_for_interface(
        snapshot["recent_logs"], interface
    )
    return filtered


@dataclass(frozen=True)
class Scenario:
    """Per-scenario metadata used by the runner to drive dispatch.

    inject/restore are callables imported from faults/<scenario>.py.
    evidence_filter, if not None, is applied to the AFTER snapshot
    (with the scenario's interface) before it is added to the
    blackboard, so scenario-specific evidence hygiene lives in
    registry metadata rather than being scattered through main().
    manual_restore_command is the literal command string the runner
    prints under --keep-fault so users can clean up by hand later.
    """
    name: str
    inject: Callable[[], None]
    restore: Callable[[], None]
    user_complaint: str
    interface: str
    requires_bgp_lab: bool
    evidence_filter: Optional[Callable[[dict, str], dict]]
    post_inject_delay_seconds: float
    manual_restore_command: str


SCENARIOS: dict[str, Scenario] = {
    "interface_admin_down": Scenario(
        name="interface_admin_down",
        inject=interface_admin_down.inject,
        restore=interface_admin_down.restore,
        user_complaint=(
            "Ethernet4 stopped passing traffic. "
            "Something is wrong, figure it out."
        ),
        interface=DEFAULT_INTERFACE,
        requires_bgp_lab=False,
        evidence_filter=_admin_down_evidence_filter,
        post_inject_delay_seconds=1.0,
        manual_restore_command=(
            "python3 faults/interface_admin_down.py restore"
        ),
    ),
    "bgp_neighbor_removal": Scenario(
        name="bgp_neighbor_removal",
        inject=bgp_neighbor_removal.inject,
        restore=bgp_neighbor_removal.restore,
        user_complaint=(
            "Traffic to prefixes learned over BGP stopped working. "
            "Figure out what changed."
        ),
        interface=DEFAULT_INTERFACE,
        requires_bgp_lab=True,
        evidence_filter=None,
        post_inject_delay_seconds=1.0,
        manual_restore_command=(
            "python3 faults/bgp_neighbor_removal.py restore"
        ),
    ),
    "bgp_asn_mismatch": Scenario(
        name="bgp_asn_mismatch",
        inject=bgp_asn_mismatch.inject,
        restore=bgp_asn_mismatch.restore,
        user_complaint=(
            "BGP sessions are not establishing correctly. "
            "Figure out what changed."
        ),
        interface=DEFAULT_INTERFACE,
        requires_bgp_lab=True,
        evidence_filter=None,
        post_inject_delay_seconds=1.0,
        manual_restore_command=(
            "python3 faults/bgp_asn_mismatch.py restore"
        ),
    ),
}


def run_dry_run(scenario: Scenario) -> None:
    """Print the planned steps without mutating state or calling Ollama.

    Dry-run does NOT call inject, restore, configure_bgp.sh, collectors,
    or Ollama. It only describes what would happen.
    """
    _eprint("=== DRY RUN (no mutation, no Ollama call) ===")
    _eprint(f"scenario:                          {scenario.name}")
    _eprint(f"requires BGP lab:                  {scenario.requires_bgp_lab}")
    _eprint(f"interface for per-port collectors: {scenario.interface}")
    _eprint(
        f"evidence filter:                   "
        f"{'present' if scenario.evidence_filter else 'none'}"
    )
    _eprint(f"post-inject delay seconds:         {scenario.post_inject_delay_seconds}")
    _eprint(f"user_complaint:                    {scenario.user_complaint!r}")
    _eprint("planned steps:")
    step = 1
    if scenario.requires_bgp_lab:
        _eprint(f"  {step}. scripts/configure_bgp.sh up (test fixture)")
        step += 1
    _eprint(
        f"  {step}. take BEFORE snapshot via 4 collectors "
        f"(interface_state[{scenario.interface}], "
        f"interface_counters[{scenario.interface}], "
        f"bgp_summary, recent_logs)"
    )
    step += 1
    _eprint(f"  {step}. inject scenario via faults.{scenario.name}.inject")
    step += 1
    _eprint(f"  {step}. sleep {scenario.post_inject_delay_seconds}s")
    step += 1
    _eprint(f"  {step}. take AFTER snapshot via same 4 collectors")
    step += 1
    if scenario.evidence_filter is not None:
        _eprint(f"  {step}. apply scenario evidence filter to AFTER snapshot")
        step += 1
    _eprint(f"  {step}. populate Blackboard with user_complaint + evidence")
    step += 1
    _eprint(
        f"  {step}. fan-out specialists (triage, interface, bgp, logs) "
        f"concurrently via Ollama; each posts hypotheses to blackboard"
    )
    step += 1
    _eprint(
        f"  {step}. fan-in: call produce_diagnosis (qwen2.5:7b-instruct) "
        f"to synthesize evidence + specialist hypotheses"
    )
    step += 1
    _eprint(f"  {step}. print diagnosis dict as JSON to stdout")
    step += 1
    _eprint(
        f"  {step}. restore scenario via faults.{scenario.name}.restore "
        f"(test cleanup)"
    )
    step += 1
    if scenario.requires_bgp_lab:
        _eprint(
            f"  {step}. scripts/configure_bgp.sh down "
            f"(test fixture teardown)"
        )
    _eprint("--keep-fault would skip the restore and BGP-lab-down steps.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "End-to-end runner: inject a registered fault on "
            f"{CONTAINER}, run collectors, ask qwen2.5:7b-instruct "
            "to narrate a diagnosis, restore."
        )
    )
    parser.add_argument(
        "--scenario",
        required=True,
        choices=sorted(SCENARIOS.keys()),
        help=(
            "name of the fault scenario to run. Required. "
            "Choices come from the SCENARIOS registry in main.py."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print planned steps for the chosen scenario; no mutation, no "
            "Ollama call, no configure_bgp.sh call"
        ),
    )
    parser.add_argument(
        "--keep-fault",
        action="store_true",
        help=(
            "inject and diagnose, then skip restore (and skip "
            "configure_bgp.sh down for BGP scenarios) so the fault state "
            "can be inspected manually afterward"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scenario = SCENARIOS[args.scenario]

    if args.dry_run:
        run_dry_run(scenario)
        return 0

    if not is_container_running(CONTAINER):
        _eprint(
            f"error: container {CONTAINER!r} is not running. "
            f"Run ./scripts/bringup.sh first."
        )
        return 2

    bgp_lab_up = False
    injected = False
    exit_code = 0

    try:
        if scenario.requires_bgp_lab:
            _eprint("=== BGP LAB UP (test fixture, not remediation) ===")
            try:
                _run_configure_bgp("up")
            except Exception as exc:
                _eprint(f"error: configure_bgp.sh up failed: {exc}")
                return 7
            bgp_lab_up = True

        before = take_snapshot(scenario.interface)
        print_snapshot(before, "BEFORE")

        _eprint(f"=== INJECT ({scenario.name}) ===")
        _call_with_stdout_to_stderr(scenario.inject)
        injected = True

        time.sleep(scenario.post_inject_delay_seconds)

        after = take_snapshot(scenario.interface)
        print_snapshot(after, "AFTER")

        if scenario.evidence_filter is not None:
            evidence_for_agent = scenario.evidence_filter(
                after, scenario.interface
            )
        else:
            evidence_for_agent = after

        bb = Blackboard(scenario.user_complaint)
        for name, data in evidence_for_agent.items():
            bb.add_evidence(name, data)

        # Fan-out: four specialist agents read their evidence slice
        # from the blackboard concurrently and each posts hypotheses
        # back. Individual specialist failures are non-fatal — the
        # diagnosis agent then sees only the surviving hypotheses.
        # Output ordering below is by future completion, not by
        # submission order, so the per-specialist lines may appear in
        # any order; that is expected.
        _eprint("=== SPECIALISTS (fan-out) ===")
        specialists = [
            ("triage", produce_triage_hypotheses),
            ("interface", produce_interface_hypotheses),
            ("bgp", produce_bgp_hypotheses),
            ("logs", produce_logs_hypotheses),
        ]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(fn, bb): name for name, fn in specialists
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    _eprint(f"  {name}: posted hypotheses")
                except Exception as exc:
                    _eprint(f"  {name}: failed ({exc})")

        _eprint("=== FAN-IN: DIAGNOSIS ===")
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
        # Scenario restore first (re-add neighbor while lab is still up,
        # bring interface back up, etc.). Then lab teardown after.
        if injected and not args.keep_fault:
            _eprint(
                f"=== RESTORE ({scenario.name}, test cleanup, not remediation) ==="
            )
            try:
                _call_with_stdout_to_stderr(scenario.restore)
            except Exception as exc:
                _eprint(f"warn: restore failed: {exc}")
                if exit_code == 0:
                    exit_code = 4
        elif injected and args.keep_fault:
            _eprint("=== KEEPING FAULT (--keep-fault) ===")
            _eprint("  Manual cleanup commands:")
            _eprint(f"    {scenario.manual_restore_command}")
            if scenario.requires_bgp_lab:
                _eprint("    scripts/configure_bgp.sh down")

        if bgp_lab_up and not args.keep_fault:
            _eprint("=== BGP LAB DOWN (test cleanup, not remediation) ===")
            try:
                _run_configure_bgp("down")
            except Exception as exc:
                _eprint(f"warn: configure_bgp.sh down failed: {exc}")
                if exit_code == 0:
                    exit_code = 4

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
