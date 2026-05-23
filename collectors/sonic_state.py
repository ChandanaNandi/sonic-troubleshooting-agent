"""Evidence collectors that read SONiC state from sonic-vs-troubleshoot.

Pure Python, no LLM. Each `collect_*` function returns a structured dict.
Failures do NOT raise — they return a dict with an "error" key so a
caller can include "this collector failed" as evidence later.

Data sources (Redis databases inside the container):
    CONFIG_DB    (db 4): PORT|<name> for admin_status
    APP_DB       (db 0): PORT_TABLE:<name> for oper_status (note `:` separator)
    COUNTERS_DB  (db 2): COUNTERS_PORT_NAME_MAP -> oid; COUNTERS:<oid> for SAI stats
    plus vtysh (`show bgp summary json`) and /var/log/syslog inside the container.

Preconditions:
    sonic-vs-troubleshoot is running with the SONiC service stack up.
    Run ./scripts/bringup.sh first if redis or swss are not responding.

Run standalone to smoke-test all four collectors:
    python3 collectors/sonic_state.py
"""

import json
import subprocess

CONTAINER = "sonic-vs-troubleshoot"
CONFIG_DB = 4
APP_DB = 0
COUNTERS_DB = 2
COMMAND_TIMEOUT_SECONDS = 10

# SAI counter fields read from COUNTERS:<oid>. Mapped to the result keys
# the caller sees. SONiC VS may not populate per-port counters without
# ASIC traffic; missing fields are reported as 0.
_COUNTER_FIELDS: dict[str, str] = {
    "rx_packets": "SAI_PORT_STAT_IF_IN_UCAST_PKTS",
    "tx_packets": "SAI_PORT_STAT_IF_OUT_UCAST_PKTS",
    "rx_errors": "SAI_PORT_STAT_IF_IN_ERRORS",
    "tx_errors": "SAI_PORT_STAT_IF_OUT_ERRORS",
    "rx_discards": "SAI_PORT_STAT_IF_IN_DISCARDS",
    "tx_discards": "SAI_PORT_STAT_IF_OUT_DISCARDS",
}


class CollectorError(Exception):
    """Raised by _docker_exec on subprocess failure. Each collect_* function
    catches this internally and converts it to an "error" key in its result.
    """


def _docker_exec(args: list[str]) -> str:
    """Run a command inside CONTAINER and return stdout (newline-stripped).

    Raises CollectorError on timeout, non-zero exit, or missing docker.
    Same docker exec pattern as faults/interface_admin_down.py.
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
        raise CollectorError(
            f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: {cmd}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise CollectorError(
            f"command failed with exit code {exc.returncode}: {cmd}; "
            f"stderr: {exc.stderr.strip()}"
        ) from exc
    except FileNotFoundError as exc:
        raise CollectorError(
            "docker executable not found on PATH; is Docker Desktop running?"
        ) from exc
    return result.stdout.rstrip("\n")


def _parse_redis_hgetall(raw: str) -> dict[str, str]:
    """Turn redis-cli HGETALL line-pair output into a dict.

    redis-cli (text mode) emits one field/value per line, alternating:
        field1\nvalue1\nfield2\nvalue2\n
    Empty input returns an empty dict.
    """
    lines = raw.splitlines()
    return dict(zip(lines[0::2], lines[1::2]))


def collect_interface_state(interface_name: str) -> dict:
    """Return admin_status (CONFIG_DB) and oper_status (APP_DB) for a port.

    Confirms the port exists in CONFIG_DB before reading fields, so that
    a missing admin_status on a nonexistent port is reported as an error
    rather than silently defaulting to "up". Only when PORT|<name> exists
    does an absent admin_status field default to "up" (SONiC convention).
    A port with no oper_status field in APP_DB is reported as "unknown"
    because we can't infer it.
    """
    try:
        exists = _docker_exec(
            ["redis-cli", "-n", str(CONFIG_DB),
             "EXISTS", f"PORT|{interface_name}"]
        ).strip()
        if exists != "1":
            return {
                "interface": interface_name,
                "error": f"PORT|{interface_name} not in CONFIG_DB",
                "source": "CONFIG_DB and APP_DB",
            }

        admin_raw = _docker_exec(
            ["redis-cli", "-n", str(CONFIG_DB),
             "HGET", f"PORT|{interface_name}", "admin_status"]
        ).strip().lower()
        admin_status = admin_raw if admin_raw else "up"

        oper_raw = _docker_exec(
            ["redis-cli", "-n", str(APP_DB),
             "HGET", f"PORT_TABLE:{interface_name}", "oper_status"]
        ).strip().lower()
        oper_status = oper_raw if oper_raw else "unknown"

        return {
            "interface": interface_name,
            "admin_status": admin_status,
            "oper_status": oper_status,
            "source": "CONFIG_DB and APP_DB",
        }
    except CollectorError as exc:
        return {
            "interface": interface_name,
            "error": str(exc),
            "source": "CONFIG_DB and APP_DB",
        }


def collect_interface_counters(interface_name: str) -> dict:
    """Return rx/tx packet/error/discard counters from COUNTERS_DB.

    SONiC VS does not always populate per-port counters (no real ASIC
    traffic), so missing fields are reported as 0 and the source string
    notes when the COUNTERS hash exists but is empty.
    """
    result: dict = {"interface": interface_name, "source": "COUNTERS_DB"}
    try:
        oid = _docker_exec(
            ["redis-cli", "-n", str(COUNTERS_DB),
             "HGET", "COUNTERS_PORT_NAME_MAP", interface_name]
        ).strip()
        if not oid:
            result["error"] = (
                f"{interface_name} not in COUNTERS_PORT_NAME_MAP"
            )
            for key in _COUNTER_FIELDS:
                result[key] = 0
            return result

        raw = _docker_exec(
            ["redis-cli", "-n", str(COUNTERS_DB),
             "HGETALL", f"COUNTERS:{oid}"]
        )
        counters = _parse_redis_hgetall(raw)
        for key, sai_name in _COUNTER_FIELDS.items():
            try:
                result[key] = int(counters.get(sai_name, "0"))
            except ValueError:
                result[key] = 0

        if not counters:
            result["source"] = (
                "COUNTERS_DB (oid present but COUNTERS hash empty; "
                "flex_counter has not populated stats on this SONiC VS)"
            )
        return result
    except CollectorError as exc:
        for key in _COUNTER_FIELDS:
            result.setdefault(key, 0)
        result["error"] = str(exc)
        return result


def collect_bgp_summary() -> dict:
    """Return parsed `vtysh show bgp summary json` output.

    When no BGP instance is configured, FRR returns an empty JSON object
    (or a text message); both are reported as bgp_instance_present=False
    with an empty neighbors list.
    """
    source = "vtysh show bgp summary json"
    try:
        raw = _docker_exec(["vtysh", "-c", "show bgp summary json"]).strip()
    except CollectorError as exc:
        return {
            "bgp_instance_present": False,
            "neighbors": [],
            "error": str(exc),
            "source": source,
        }

    if not raw:
        return {
            "bgp_instance_present": False,
            "neighbors": [],
            "source": source,
        }

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # FRR sometimes emits "No BGP process is configured" plain text
        return {
            "bgp_instance_present": False,
            "neighbors": [],
            "source": source,
        }

    neighbors: list[dict] = []
    for af_key in ("ipv4Unicast", "ipv6Unicast"):
        af = data.get(af_key, {}) if isinstance(data, dict) else {}
        peers = af.get("peers", {}) if isinstance(af, dict) else {}
        for addr, peer_info in peers.items():
            neighbors.append({
                "neighbor": addr,
                "asn": peer_info.get("remoteAs"),
                "state": peer_info.get("state", "unknown"),
            })

    bgp_present = bool(neighbors) or any(
        isinstance(data.get(k), dict) and "as" in data.get(k, {})
        for k in ("ipv4Unicast", "ipv6Unicast")
    )

    return {
        "bgp_instance_present": bgp_present,
        "neighbors": neighbors,
        "source": source,
    }


def collect_recent_logs(lines: int = 50) -> dict:
    """Return the last `lines` lines from /var/log/syslog inside the container.

    Uses a single docker exec that prints a sentinel when the file is
    missing and otherwise emits the tail. This way the "syslog not
    available" branch does not swallow real docker-exec failures
    (container stopped, docker not on PATH, etc.) — those propagate to
    the "error" key.
    """
    syslog_path = "/var/log/syslog"
    missing_sentinel = "__SYSLOG_MISSING__"
    # Normalize and clamp `lines` to a safe integer before interpolating
    # into the shell command. Defends against non-int input and unbounded
    # tail requests; the f-string would otherwise inject `lines` verbatim.
    try:
        line_count = int(lines)
    except (TypeError, ValueError):
        line_count = 50
    line_count = max(0, min(line_count, 500))
    shell_cmd = (
        f'if [ ! -f "{syslog_path}" ]; then '
        f'  echo "{missing_sentinel}"; exit 0; '
        f'fi; '
        f'tail -n {line_count} "{syslog_path}"'
    )
    try:
        raw = _docker_exec(["sh", "-c", shell_cmd])
    except CollectorError as exc:
        return {
            "log_lines": [],
            "error": str(exc),
            "source": syslog_path,
        }

    if raw.strip() == missing_sentinel:
        return {
            "log_lines": [],
            "source": f"{syslog_path} not available",
        }
    return {
        "log_lines": raw.splitlines(),
        "source": syslog_path,
    }


if __name__ == "__main__":
    interface = "Ethernet4"
    sections = {
        f"interface_state({interface})": collect_interface_state(interface),
        f"interface_counters({interface})": collect_interface_counters(interface),
        "bgp_summary": collect_bgp_summary(),
        "recent_logs(20)": collect_recent_logs(20),
    }
    for name, payload in sections.items():
        print(f"=== {name} ===")
        print(json.dumps(payload, indent=2))
        print()
