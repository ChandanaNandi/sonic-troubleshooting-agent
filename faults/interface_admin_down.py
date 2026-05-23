"""Fault injection: set Ethernet4 admin_status to down on sonic-vs-troubleshoot.

Mirrors the apply pattern from sonic-intent-agent phase4/sonic_client.py
(apply_set_interface_admin_status): shells out to the SONiC
`config interface shutdown|startup` CLI inside the container, then
verifies the change by reading PORT|Ethernet4 from CONFIG_DB (redis db 4).

Reversible: `inject` sets admin down, `restore` brings it back up.

Preconditions:
    sonic-vs-troubleshoot is running with the SONiC service stack up.
    Run ./scripts/bringup.sh first if redis or swss are not responding.

Usage:
    python3 faults/interface_admin_down.py inject
    python3 faults/interface_admin_down.py restore
"""

import argparse
import subprocess
import sys
import time

CONTAINER = "sonic-vs-troubleshoot"
INTERFACE = "Ethernet4"
CONFIG_DB_NUMBER = 4
COMMAND_TIMEOUT_SECONDS = 10


class FaultInjectionError(Exception):
    """Raised when a fault injection step fails."""


def _docker_exec(args: list[str]) -> str:
    """Run a command inside CONTAINER and return its stdout (newline-stripped).

    Raises FaultInjectionError on timeout, non-zero exit, or missing docker.
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


def _check_preconditions() -> None:
    """Fail fast with a helpful message if the container is not operational."""
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
    exists = _docker_exec(
        ["redis-cli", "-n", str(CONFIG_DB_NUMBER),
         "EXISTS", f"PORT|{INTERFACE}"]
    )
    if exists != "1":
        raise FaultInjectionError(
            f"CONFIG_DB has no PORT|{INTERFACE} entry. "
            f"Run ./scripts/bringup.sh to bring the SONiC stack up."
        )


def read_admin_status() -> str:
    """Return the admin_status of INTERFACE as 'up' or 'down'.

    When CONFIG_DB has no admin_status field for the port, SONiC treats
    the port as administratively up, so this returns 'up' in that case.
    """
    value = _docker_exec(
        ["redis-cli", "-n", str(CONFIG_DB_NUMBER),
         "HGET", f"PORT|{INTERFACE}", "admin_status"]
    )
    if not value:
        return "up"
    return value.strip().lower()


def wait_for_admin_status(
    expected: str,
    timeout: float = 2.0,
    interval: float = 0.05,
) -> str:
    """Poll read_admin_status until it matches expected or timeout elapses.

    Accommodates SONiC's measurable CONFIG_DB read-after-write lag (60-80ms
    typical per sonic-intent-agent phase6 measurements). The `config
    interface shutdown|startup` CLI call returns before the CONFIG_DB key
    is necessarily readable, so a single immediate HGET would be racy.

    Returns the last observed admin_status, which equals expected on success
    and otherwise reflects the final read before the deadline. Callers
    compare against expected and raise FaultInjectionError on mismatch.
    """
    deadline = time.monotonic() + timeout
    last = read_admin_status()
    while last != expected and time.monotonic() < deadline:
        time.sleep(interval)
        last = read_admin_status()
    return last


def _apply_admin_status(target: str) -> None:
    """Run `config interface shutdown|startup INTERFACE` inside the container.

    Mirrors sonic-intent-agent phase4 apply_set_interface_admin_status.
    """
    if target == "down":
        subcommand = "shutdown"
    elif target == "up":
        subcommand = "startup"
    else:
        raise ValueError(f"target must be 'up' or 'down': {target!r}")
    _docker_exec(["config", "interface", subcommand, INTERFACE])


def inject() -> None:
    """Set INTERFACE admin_status to down. No-op if already down."""
    _check_preconditions()
    before = read_admin_status()
    print(f"before: {INTERFACE} admin_status={before}")
    if before == "down":
        print(f"{INTERFACE} is already admin down; nothing to inject.")
        return
    print(f"injecting: shutting down {INTERFACE}")
    _apply_admin_status("down")
    after = wait_for_admin_status("down")
    print(f"after:  {INTERFACE} admin_status={after}")
    if after != "down":
        raise FaultInjectionError(
            f"expected admin_status=down after inject, got {after!r}"
        )
    print(f"inject ok: {INTERFACE} is now admin down")


def restore() -> None:
    """Bring INTERFACE admin_status back to up. No-op if already up."""
    _check_preconditions()
    before = read_admin_status()
    print(f"before: {INTERFACE} admin_status={before}")
    if before == "up":
        print(f"{INTERFACE} is already admin up; nothing to restore.")
        return
    print(f"restoring: bringing {INTERFACE} back up")
    _apply_admin_status("up")
    after = wait_for_admin_status("up")
    print(f"after:  {INTERFACE} admin_status={after}")
    if after != "up":
        raise FaultInjectionError(
            f"expected admin_status=up after restore, got {after!r}"
        )
    print(f"restore ok: {INTERFACE} is now admin up")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject or restore an admin-down fault on Ethernet4."
    )
    parser.add_argument(
        "action",
        choices=["inject", "restore"],
        help="inject: set admin_status=down. restore: set admin_status=up.",
    )
    args = parser.parse_args()
    try:
        if args.action == "inject":
            inject()
        else:
            restore()
    except FaultInjectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
