"""Fault injection: remove BGP neighbor 10.10.10.2 from sonic-vs-troubleshoot.

First Phase 2C BGP fault scenario. Per phase2/2C_CONTROL_PLANE_DECISION.md,
Phase 2C BGP faults mutate state via vtysh on the SUT, matching the path
scripts/configure_bgp.sh uses for setup. CONFIG_DB + bgpcfgd is not used
here; that path is deferred until bgpcfgd's behavior on this image has been
validated separately.

Preconditions:
    The two-container BGP lab must be up (sonic-bgp-peer container running,
    sonic-vs-troubleshoot connected to sonic-bgp-lab, BGP session
    Established). This script does NOT call scripts/configure_bgp.sh
    itself; if the precondition is not met, `inject` exits non-zero with a
    pointer to the right setup command.

Note on post-inject observable state:
    `no neighbor 10.10.10.2` removes the only configured neighbor from the
    router-bgp block on the SUT. FRR then reports `show bgp summary json`
    as `{}` (the same shape an empty `router bgp` block produces, observed
    in phase2/2B_TOPOLOGY_FINDINGS.md). This script categorizes that case
    as "removed" rather than confusing it with "no BGP instance at all".

Usage:
    python3 faults/bgp_neighbor_removal.py inject
    python3 faults/bgp_neighbor_removal.py restore
    python3 faults/bgp_neighbor_removal.py status
"""

import argparse
import json
import subprocess
import sys
import time

CONTAINER = "sonic-vs-troubleshoot"
SUT_ASN = "65000"
PEER_IP = "10.10.10.2"
PEER_ASN = "65001"
INJECT_TIMEOUT_SECONDS = 30.0
RESTORE_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.5
COMMAND_TIMEOUT_SECONDS = 10


class FaultInjectionError(Exception):
    """Raised when a fault injection step fails."""


def _docker_exec(args: list[str]) -> str:
    """Run a command inside CONTAINER and return stdout (newline-stripped).

    Same pattern as faults/interface_admin_down.py: raises
    FaultInjectionError on timeout, non-zero exit, or missing docker.
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
    """Fail fast if sonic-vs-troubleshoot is not running.

    Does NOT verify BGP lab state — that is inject's responsibility.
    """
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


def read_peer_state() -> str:
    """Return the current BGP peer state for PEER_IP on the SUT.

    Returns one of:
        "established"  — peer present, FRR state == "Established"
        "removed"      — peer absent from `show bgp summary json`
                         (either the entire output is {} when no peers
                         remain, or the peer key is missing from
                         ipv4Unicast.peers)
        any other str  — the raw FRR FSM state ("Idle", "Active",
                         "Connect", "OpenSent", "OpenConfirm", etc.)
    """
    raw = _docker_exec(["vtysh", "-c", "show bgp summary json"]).strip()
    if not raw or raw == "{}":
        return "removed"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # vtysh occasionally emits non-JSON text when no BGP is configured;
        # treat as removed since the peer is by definition not visible.
        return "removed"
    peers = data.get("ipv4Unicast", {}).get("peers", {}) if isinstance(data, dict) else {}
    if PEER_IP not in peers:
        return "removed"
    state = peers[PEER_IP].get("state", "unknown")
    return "established" if state == "Established" else state


def wait_for_state(predicate, timeout: float, interval: float = POLL_INTERVAL_SECONDS) -> str:
    """Poll read_peer_state until predicate(state) is True or timeout elapses.

    Returns the last observed state. The caller decides whether the final
    state satisfies expectations and raises if not.
    """
    deadline = time.monotonic() + timeout
    last = read_peer_state()
    while not predicate(last) and time.monotonic() < deadline:
        time.sleep(interval)
        last = read_peer_state()
    return last


def _peer_reachable() -> bool:
    """True if the SUT can reach PEER_IP via a single ICMP echo.

    Used by restore() to guard against re-creating SUT-side BGP config
    when the lab fixture (peer container + sonic-bgp-lab network) is
    not actually up. Without this guard, restore from a clean state
    would leave a stale `router bgp` block on the SUT.
    """
    try:
        _docker_exec(["ping", "-c", "1", "-W", "1", PEER_IP])
        return True
    except FaultInjectionError:
        return False


def _apply_no_neighbor() -> None:
    """Run `vtysh ... no neighbor PEER_IP` on the SUT to remove the neighbor."""
    _docker_exec([
        "vtysh",
        "-c", "configure terminal",
        "-c", f"router bgp {SUT_ASN}",
        "-c", f"no neighbor {PEER_IP}",
    ])


def _apply_add_neighbor() -> None:
    """Run `vtysh ... neighbor PEER_IP remote-as PEER_ASN` on the SUT."""
    _docker_exec([
        "vtysh",
        "-c", "configure terminal",
        "-c", f"router bgp {SUT_ASN}",
        "-c", f"neighbor {PEER_IP} remote-as {PEER_ASN}",
    ])


def inject() -> None:
    """Remove the BGP neighbor. Requires the lab to be Established."""
    _check_container_running()
    before = read_peer_state()
    print(f"before: peer {PEER_IP} state={before}")
    if before != "established":
        raise FaultInjectionError(
            f"BGP lab is not ready (peer state={before!r}, expected established). "
            f"Run scripts/configure_bgp.sh up"
        )
    print(f"injecting: removing neighbor {PEER_IP} via vtysh")
    _apply_no_neighbor()
    after = wait_for_state(lambda s: s == "removed", timeout=INJECT_TIMEOUT_SECONDS)
    print(f"after:  peer {PEER_IP} state={after}")
    if after != "removed":
        raise FaultInjectionError(
            f"expected peer removed after inject, got state={after!r} "
            f"(timeout after {INJECT_TIMEOUT_SECONDS}s)"
        )
    print(f"inject ok: neighbor {PEER_IP} removed")


def restore() -> None:
    """Re-add the BGP neighbor and wait for Established. No-op if already up.

    Refuses to re-create SUT-side BGP config if the peer fixture is not
    reachable; otherwise restore from a clean state would leave a stale
    `router bgp` block on the SUT after the lab is down.
    """
    _check_container_running()
    before = read_peer_state()
    print(f"before: peer {PEER_IP} state={before}")
    if before == "established":
        print(f"peer {PEER_IP} is already established; nothing to restore.")
        return
    if not _peer_reachable():
        raise FaultInjectionError(
            f"BGP lab is not ready; peer {PEER_IP} is unreachable. "
            f"Run scripts/configure_bgp.sh up"
        )
    print(f"restoring: neighbor {PEER_IP} remote-as {PEER_ASN} via vtysh")
    _apply_add_neighbor()
    after = wait_for_state(lambda s: s == "established", timeout=RESTORE_TIMEOUT_SECONDS)
    print(f"after:  peer {PEER_IP} state={after}")
    if after != "established":
        raise FaultInjectionError(
            f"expected peer established after restore, got state={after!r} "
            f"(timeout after {RESTORE_TIMEOUT_SECONDS}s)"
        )
    print(f"restore ok: neighbor {PEER_IP} is established")


def status() -> None:
    """Print current peer state in the concise three-category form."""
    _check_container_running()
    state = read_peer_state()
    if state == "established":
        print("established")
    elif state == "removed":
        print("removed")
    else:
        print(f"other:{state}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inject, restore, or report the BGP-neighbor-removal fault on "
            f"{CONTAINER} (Phase 2C BGP scenario)."
        )
    )
    parser.add_argument(
        "action",
        choices=["inject", "restore", "status"],
        help=(
            "inject: remove neighbor via 'no neighbor'. "
            "restore: re-add neighbor with original remote-as. "
            "status: print current peer state (established | removed | other:<state>)."
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
