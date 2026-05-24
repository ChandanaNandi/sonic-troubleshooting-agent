"""Fault injection: BGP ASN mismatch on neighbor 10.10.10.2.

Second Phase 2D BGP fault scenario, after bgp_neighbor_removal. Per
phase2/2C_CONTROL_PLANE_DECISION.md, Phase 2C/2D BGP faults mutate
state via vtysh on the SUT, matching the path scripts/configure_bgp.sh
uses for setup. CONFIG_DB + bgpcfgd is not used here.

Inject and restore methods were proven in two prior spikes:
    phase2/2D_ASN_MISMATCH_SPIKE_FINDINGS.md   — evidence of the
        OPEN/NOTIFICATION exchange and the Established->Idle transition
        when the configured remote-as no longer matches the peer's
        actual AS.
    phase2/2D_ASN_MISMATCH_RESTORE_FINDINGS.md — reconvergence method
        comparison; candidate C (revert remote-as + `clear bgp <peer>`)
        was the only method observed to keep ~2s convergence under
        both short-dwell and deep-dwell (60s mismatch + 5 NOTIFICATIONs)
        conditions.

Preconditions:
    The two-container BGP lab must be up (sonic-bgp-peer container
    running, sonic-vs-troubleshoot connected to sonic-bgp-lab, BGP
    session Established with remoteAs 65001). This script does NOT
    call scripts/configure_bgp.sh itself; precondition failures exit
    non-zero with a pointer to the right setup command, matching the
    fail-loud pattern in faults/bgp_neighbor_removal.py.

Evidence-shape notes:
    The current collectors/sonic_state.py:collect_bgp_summary surfaces
    the Established/Idle transition via `state` and the configured AS
    via `remoteAs` (both in `show bgp summary json`). It does NOT
    consume the per-neighbor JSON that exposes Bad Peer AS specifically
    (lastErrorCodeSubcode "0202", lastNotificationReason
    "OPEN Message Error/Bad Peer AS"). This script does not read that
    enrichment either; it observes the FSM transition only. Diagnosis
    quality enrichment is a separate Phase 2D+ concern.

Usage:
    python3 faults/bgp_asn_mismatch.py inject
    python3 faults/bgp_asn_mismatch.py restore
    python3 faults/bgp_asn_mismatch.py status
"""

import argparse
import json
import subprocess
import sys
import time

CONTAINER = "sonic-vs-troubleshoot"
SUT_ASN = "65000"
PEER_IP = "10.10.10.2"
CORRECT_PEER_ASN = "65001"
WRONG_PEER_ASN = "65002"
INJECT_TIMEOUT_SECONDS = 30.0
RESTORE_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.5
COMMAND_TIMEOUT_SECONDS = 10


class FaultInjectionError(Exception):
    """Raised when a fault injection step fails."""


def _docker_exec(args: list[str]) -> str:
    """Run a command inside CONTAINER and return stdout (newline-stripped).

    Same pattern as faults/bgp_neighbor_removal.py and
    faults/interface_admin_down.py.
    """
    cmd = ["docker", "exec", CONTAINER, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise FaultInjectionError(
            f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: {cmd}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise FaultInjectionError(
            f"command failed with exit code {exc.returncode}: {cmd}; "
            f"stderr: {exc.stderr.strip()}"
        ) from exc
    except FileNotFoundError as exc:
        raise FaultInjectionError(
            "docker executable not found on PATH; is Docker Desktop running?"
        ) from exc
    return result.stdout.rstrip("\n")


def _check_container_running() -> None:
    """Fail fast if sonic-vs-troubleshoot is not running."""
    ps = subprocess.run(
        ["docker", "ps", "--filter", f"name={CONTAINER}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=5,
    )
    if CONTAINER not in ps.stdout.split():
        raise FaultInjectionError(
            f"container {CONTAINER!r} is not running. "
            f"Run ./scripts/bringup.sh first."
        )


def _peer_reachable() -> bool:
    """True if the SUT can reach PEER_IP via a single ICMP echo.

    Used by restore() to guard against re-creating SUT-side BGP config
    when the lab fixture is not actually up. Same pattern as
    faults/bgp_neighbor_removal.py.
    """
    try:
        _docker_exec(["ping", "-c", "1", "-W", "1", PEER_IP])
        return True
    except FaultInjectionError:
        return False


def _read_peer_raw() -> tuple:
    """Return (state, remoteAs) for the peer from show bgp summary json.

    Returns (None, None) if the peer is absent (summary is {} or the
    peer key is missing from ipv4Unicast.peers). state is a string;
    remoteAs is the int FRR reports.
    """
    raw = _docker_exec(["vtysh", "-c", "show bgp summary json"]).strip()
    if not raw or raw == "{}":
        return None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    peers = (
        data.get("ipv4Unicast", {}).get("peers", {})
        if isinstance(data, dict) else {}
    )
    if PEER_IP not in peers:
        return None, None
    peer = peers[PEER_IP]
    return peer.get("state"), peer.get("remoteAs")


def read_peer_state() -> str:
    """Return the categorized peer state for the ASN-mismatch scenario.

    Returns one of:
        "established"  — peer present, state=="Established",
                         remoteAs==CORRECT_PEER_ASN (the healthy
                         baseline state).
        "mismatched"   — peer present, state=="Idle",
                         remoteAs==WRONG_PEER_ASN (the injected
                         fault state).
        "removed"      — peer absent from show bgp summary json
                         (no neighbor entry or empty BGP instance).
        any other str  — formatted as "other:<state>:asn=<remoteAs>"
                         for any combination outside the three above
                         (e.g. transition states like Connect,
                         OpenSent, or a partially-configured
                         neighbor).
    """
    state, asn = _read_peer_raw()
    if state is None:
        return "removed"
    if state == "Established" and asn == int(CORRECT_PEER_ASN):
        return "established"
    if state == "Idle" and asn == int(WRONG_PEER_ASN):
        return "mismatched"
    return f"other:{state}:asn={asn}"


def _format_peer_line(state, asn) -> str:
    """Render (state, asn) as the human-readable suffix for before/after lines."""
    if state is None:
        return f"removed (no peer entry for {PEER_IP})"
    return f"state={state} asn={asn}"


def wait_for_state(predicate, timeout: float, interval: float = POLL_INTERVAL_SECONDS) -> str:
    """Poll read_peer_state until predicate(state) is True or timeout elapses.

    Returns the last observed category string. The caller decides
    whether the final state satisfies expectations and raises if not.
    """
    deadline = time.monotonic() + timeout
    last = read_peer_state()
    while not predicate(last) and time.monotonic() < deadline:
        time.sleep(interval)
        last = read_peer_state()
    return last


def _apply_inject() -> None:
    """Change SUT's configured remote-as for PEER_IP from CORRECT to WRONG."""
    _docker_exec([
        "vtysh",
        "-c", "configure terminal",
        "-c", f"router bgp {SUT_ASN}",
        "-c", f"neighbor {PEER_IP} remote-as {WRONG_PEER_ASN}",
    ])


def _apply_restore() -> None:
    """Revert remote-as to CORRECT, then force immediate reconvergence.

    The `clear bgp <peer>` is critical: per
    phase2/2D_ASN_MISMATCH_RESTORE_FINDINGS.md, a bare revert without
    `clear` re-established in ~15s under deep-backoff conditions
    (5 NOTIFICATIONs accumulated, FRR mid-connect-retry). Adding
    `clear bgp <peer>` brings convergence down to ~2s in the same
    conditions.
    """
    _docker_exec([
        "vtysh",
        "-c", "configure terminal",
        "-c", f"router bgp {SUT_ASN}",
        "-c", f"neighbor {PEER_IP} remote-as {CORRECT_PEER_ASN}",
    ])
    _docker_exec(["vtysh", "-c", f"clear bgp {PEER_IP}"])


def inject() -> None:
    """Inject the ASN mismatch. Requires the lab to be Established baseline."""
    _check_container_running()
    state, asn = _read_peer_raw()
    category = read_peer_state()
    print(f"before: peer {PEER_IP} {_format_peer_line(state, asn)}")
    if category != "established":
        raise FaultInjectionError(
            f"BGP lab is not ready for ASN mismatch "
            f"(peer state={category!r}, expected established). "
            f"Run scripts/configure_bgp.sh up"
        )
    print(f"injecting: changing remote-as to {WRONG_PEER_ASN} via vtysh")
    _apply_inject()
    after_category = wait_for_state(
        lambda s: s == "mismatched", timeout=INJECT_TIMEOUT_SECONDS,
    )
    after_state, after_asn = _read_peer_raw()
    print(f"after:  peer {PEER_IP} {_format_peer_line(after_state, after_asn)}")
    if after_category != "mismatched":
        raise FaultInjectionError(
            f"expected peer mismatched after inject, got state={after_category!r} "
            f"(timeout after {INJECT_TIMEOUT_SECONDS}s)"
        )
    print(f"inject ok: neighbor {PEER_IP} remote-as set to {WRONG_PEER_ASN}")


def restore() -> None:
    """Revert remote-as + clear, then wait for Established.

    No-op if already established with the correct ASN. Refuses to
    re-create SUT-side BGP config if the peer fixture is unreachable
    (matches faults/bgp_neighbor_removal.py's _peer_reachable guard).
    """
    _check_container_running()
    state, asn = _read_peer_raw()
    category = read_peer_state()
    print(f"before: peer {PEER_IP} {_format_peer_line(state, asn)}")
    if category == "established":
        print(f"peer {PEER_IP} is already established with remoteAs "
              f"{CORRECT_PEER_ASN}; nothing to restore.")
        return
    if not _peer_reachable():
        raise FaultInjectionError(
            f"BGP lab is not ready; peer {PEER_IP} is unreachable. "
            f"Run scripts/configure_bgp.sh up"
        )
    print(
        f"restoring: revert remote-as to {CORRECT_PEER_ASN} + "
        f"clear bgp {PEER_IP} via vtysh"
    )
    _apply_restore()
    after_category = wait_for_state(
        lambda s: s == "established", timeout=RESTORE_TIMEOUT_SECONDS,
    )
    after_state, after_asn = _read_peer_raw()
    print(f"after:  peer {PEER_IP} {_format_peer_line(after_state, after_asn)}")
    if after_category != "established":
        raise FaultInjectionError(
            f"expected peer established after restore, got state={after_category!r} "
            f"(timeout after {RESTORE_TIMEOUT_SECONDS}s)"
        )
    print(f"restore ok: neighbor {PEER_IP} is established")


def status() -> None:
    """Print current categorized peer state for the ASN-mismatch scenario."""
    _check_container_running()
    print(read_peer_state())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inject, restore, or report the BGP ASN-mismatch fault on "
            f"{CONTAINER} (Phase 2D BGP scenario)."
        )
    )
    parser.add_argument(
        "action",
        choices=["inject", "restore", "status"],
        help=(
            "inject: change remote-as to a wrong value. "
            "restore: revert remote-as and force reconvergence via 'clear bgp'. "
            "status: print categorized peer state (established | mismatched | removed | other:<state>:asn=<remoteAs>)."
        ),
    )
    args = parser.parse_args()
    try:
        if args.action == "inject":
            inject()
        elif args.action == "restore":
            restore()
        else:
            status()
    except FaultInjectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
