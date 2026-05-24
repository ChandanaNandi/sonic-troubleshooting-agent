# Phase 2C Runner Dispatch Findings

## Purpose

Document the `main.py` scenario-dispatch generalization committed at
`39db908`. The runner no longer hardcodes the Phase 1
`interface_admin_down` scenario; it now selects from a `SCENARIOS`
registry by an explicit `--scenario` flag. Two scenarios are
registered: `interface_admin_down` (Phase 1 baseline) and
`bgp_neighbor_removal` (first Phase 2C BGP fault). BGP scenarios use
`scripts/configure_bgp.sh up` and `down` as lab fixture setup and
cleanup, called by the runner itself. stdout remains reserved for
the diagnosis JSON; all operational output goes to stderr.

The behavior excerpts below are from the test sequence run during
the dispatch commit's session. Where output was captured in `/tmp`
or directly to terminal during that session, the excerpts are
verbatim. Cheap tests (dry-runs, error paths) and the final-state
verification were also re-run during this documentation session and
matched.


## What changed

- Introduced a `Scenario` frozen dataclass at module level and a
  `SCENARIOS` registry dict mapping scenario name to a `Scenario`
  instance. Each entry carries `inject`, `restore`, `user_complaint`,
  `interface`, `requires_bgp_lab`, `evidence_filter`,
  `post_inject_delay_seconds`, and `manual_restore_command`.
- Moved the Phase 1 log filter into a wrapper
  (`_admin_down_evidence_filter(snapshot, interface)`) that is the
  admin-down scenario's `evidence_filter`. The underlying
  `_filter_logs_for_interface(logs_data, interface)` helper is
  unchanged. The wrapper now accepts the interface from the caller
  (`Scenario.interface`) rather than hardcoding a port.
- Added `requires_bgp_lab: bool` so the runner can drive
  `scripts/configure_bgp.sh up` / `down` only for scenarios that
  need it.
- Added `manual_restore_command: str` so the `--keep-fault` hint
  prints the literal cleanup command from the registry instead of
  constructing one from `scenario.name` (which would have been
  fragile for any future scenario whose script filename diverges
  from its registry name).
- Added per-scenario `user_complaint` and `post_inject_delay_seconds`
  fields so the runner is no longer hardcoded to admin-down's
  values.
- Added `_run_configure_bgp(action)` helper that calls
  `scripts/configure_bgp.sh <action>`, captures its stdout AND
  stderr, and re-emits to stderr. Symmetric with the existing
  `_call_with_stdout_to_stderr` pattern for fault-script output.
- `main()` rewritten to use scenario metadata throughout, with a
  `bgp_lab_up` flag in the finally block so BGP teardown runs after
  scenario restore.
- CLI changes:
  - `--scenario` is now required. `argparse(required=True,
    choices=sorted(SCENARIOS.keys()))` enforces this.
  - `--dry-run` and `--keep-fault` are preserved per-scenario.
  - No `--list-scenarios` flag was added; the argparse error on
    unknown scenario already prints the available choices.

Out of scope for this commit (explicit): no blackboard or diagnosis
agent changes, no collector changes, no fault-script changes, no
scripts/ changes, no docs changes.


## Test coverage

The seven-step test sequence from the dispatch session, plus two
follow-up patches' verification, all produced the expected results.
Concise excerpts:

### 1. `python3 -m py_compile main.py`

`compile: ok`. Re-run after each of the two post-review patches:
both still ok.

### 2. `python3 main.py --dry-run --scenario interface_admin_down`

Exit 0. Stderr first/last few lines:

    === DRY RUN (no mutation, no Ollama call) ===
    scenario:                          interface_admin_down
    requires BGP lab:                  False
    interface for per-port collectors: Ethernet4
    evidence filter:                   present
    post-inject delay seconds:         1.0
    user_complaint:                    'Ethernet4 stopped passing traffic. Something is wrong, figure it out.'
    planned steps:
      1. take BEFORE snapshot via 4 collectors (interface_state[Ethernet4], interface_counters[Ethernet4], bgp_summary, recent_logs)
      ...
      9. restore scenario via faults.interface_admin_down.restore (test cleanup)
    --keep-fault would skip the restore and BGP-lab-down steps.

No `scripts/configure_bgp.sh` step appears because
`requires_bgp_lab=False`.

### 3. `python3 main.py --dry-run --scenario bgp_neighbor_removal`

Exit 0. Plan steps include the BGP lab fixture lifecycle:

    === DRY RUN (no mutation, no Ollama call) ===
    scenario:                          bgp_neighbor_removal
    requires BGP lab:                  True
    interface for per-port collectors: Ethernet4
    evidence filter:                   none
    post-inject delay seconds:         1.0
    user_complaint:                    'Traffic to prefixes learned over BGP stopped working. Figure out what changed.'
    planned steps:
      1. scripts/configure_bgp.sh up (test fixture)
      2. take BEFORE snapshot via 4 collectors (...)
      ...
      9. restore scenario via faults.bgp_neighbor_removal.restore (test cleanup)
      10. scripts/configure_bgp.sh down (test fixture teardown)
    --keep-fault would skip the restore and BGP-lab-down steps.

### 4. `python3 main.py` with no `--scenario`

Exit 2 (argparse). Stderr:

    usage: main.py [-h] --scenario {bgp_neighbor_removal,interface_admin_down}
                   [--dry-run] [--keep-fault]
    main.py: error: the following arguments are required: --scenario

### 5. `python3 main.py --scenario does_not_exist`

Exit 2 (argparse). Stderr:

    usage: main.py [-h] --scenario {bgp_neighbor_removal,interface_admin_down}
                   [--dry-run] [--keep-fault]
    main.py: error: argument --scenario: invalid choice: 'does_not_exist' (choose from 'bgp_neighbor_removal', 'interface_admin_down')

Both error paths were re-verified during this documentation
session.

### 6. Full run `interface_admin_down`

    python3 main.py --scenario interface_admin_down >/tmp/iad.json 2>/tmp/iad.err
    # exit 0
    python3 -m json.tool /tmp/iad.json
    # OK

Stderr operational sections (verbatim from the captured run):

    === BEFORE ===
      interface_state: admin_status=up oper_status=up
      interface_counters: rx_packets=0 tx_packets=0 rx_errors=0 tx_errors=0
      bgp_summary: bgp_instance_present=False neighbors=0
      recent_logs: log_lines=20
    === INJECT (interface_admin_down) ===
      before: Ethernet4 admin_status=up
      injecting: shutting down Ethernet4
      after:  Ethernet4 admin_status=down
      inject ok: Ethernet4 is now admin down
    === AFTER ===
      interface_state: admin_status=down oper_status=down
      interface_counters: rx_packets=0 tx_packets=0 rx_errors=0 tx_errors=0
      bgp_summary: bgp_instance_present=False neighbors=0
      recent_logs: log_lines=20
    === DIAGNOSIS (calling qwen2.5:7b-instruct) ===
    === RESTORE (interface_admin_down, test cleanup, not remediation) ===
      before: Ethernet4 admin_status=down
      restoring: bringing Ethernet4 back up
      after:  Ethernet4 admin_status=up
      restore ok: Ethernet4 is now admin up

Post-run state confirmed: Ethernet4 `admin_status=up`, SUT BGP
remained `{}` throughout. The Phase 1 evidence filter
(`_admin_down_evidence_filter` wrapping `_filter_logs_for_interface`)
ran via the registry path.

### 7. Full run `bgp_neighbor_removal`

    python3 main.py --scenario bgp_neighbor_removal >/tmp/bgp.json 2>/tmp/bgp.err
    # exit 0
    python3 -m json.tool /tmp/bgp.json
    # OK

Stderr showed the full lifecycle in the expected order:

    === BGP LAB UP (test fixture, not remediation) ===
      [configure_bgp] creating network sonic-bgp-lab (10.10.10.0/24)
      ...
      [configure_bgp] BGP session Established
    === BEFORE ===
      interface_state: admin_status=up oper_status=up
      interface_counters: rx_packets=0 tx_packets=0 rx_errors=0 tx_errors=0
      bgp_summary: bgp_instance_present=True neighbors=1
      recent_logs: log_lines=20
    === INJECT (bgp_neighbor_removal) ===
      before: peer 10.10.10.2 state=established
      injecting: removing neighbor 10.10.10.2 via vtysh
      after:  peer 10.10.10.2 state=removed
      inject ok: neighbor 10.10.10.2 removed
    === AFTER ===
      interface_state: admin_status=up oper_status=up
      interface_counters: rx_packets=0 tx_packets=0 rx_errors=0 tx_errors=0
      bgp_summary: bgp_instance_present=False neighbors=0
      recent_logs: log_lines=20
    === DIAGNOSIS (calling qwen2.5:7b-instruct) ===
    === RESTORE (bgp_neighbor_removal, test cleanup, not remediation) ===
      before: peer 10.10.10.2 state=removed
      restoring: neighbor 10.10.10.2 remote-as 65001 via vtysh
      after:  peer 10.10.10.2 state=established
      restore ok: neighbor 10.10.10.2 is established
    === BGP LAB DOWN (test cleanup, not remediation) ===
      [configure_bgp] removing SUT BGP config (no router bgp 65000)
      ...
      [configure_bgp] down: clean state confirmed

Post-run state: SUT BGP `{}`, no `sonic-bgp-peer` container, no
`sonic-bgp-lab` network. The BGP scenario has
`evidence_filter=None`; no filter was applied between the AFTER
snapshot and blackboard population.

### 8. `--keep-fault` sanity for `interface_admin_down`

Confirmed the registry-owned `manual_restore_command` prints
verbatim and the configure_bgp.sh down hint is correctly omitted:

    === KEEPING FAULT (--keep-fault) ===
      Manual cleanup commands:
        python3 faults/interface_admin_down.py restore

After a manual restore via the printed command, Ethernet4 was back
to `admin_status=up`. The same `--keep-fault` path for
`bgp_neighbor_removal` would additionally print
`scripts/configure_bgp.sh down`; that path was verified by
inspection of the same three-line code block rather than by
re-running the full BGP cycle.


## Important behavior

- For BGP scenarios, the BEFORE snapshot is taken **after**
  `scripts/configure_bgp.sh up`, so the baseline collector output
  the diagnosis agent eventually sees reflects the healthy
  Established session.
- The AFTER snapshot is taken **after** inject and **after** the
  scenario's `post_inject_delay_seconds` sleep (1.0s for both
  scenarios currently).
- Scenario restore runs **before** `scripts/configure_bgp.sh down`,
  so for BGP scenarios the restore happens while the peer is still
  reachable (the fault script's `_peer_reachable()` guard would
  refuse otherwise).
- `--keep-fault` for BGP scenarios intentionally skips both
  scenario restore AND lab teardown, and prints both manual cleanup
  commands. This matches the non-BGP `--keep-fault` behavior of
  leaving the single injected fault in place.
- stdout JSON remained parseable for both full runs (`python3 -m
  json.tool` returned 0). The
  `_call_with_stdout_to_stderr` wrapper and the
  `_run_configure_bgp` capture both forward subordinate-process
  output to stderr so stdout stays pure.
- Exit codes: 0 success; 2 from argparse (no `--scenario` or
  unknown scenario); 3 diagnosis failure; 4 restore or
  configure_bgp.sh down failure during cleanup; 7
  configure_bgp.sh up failure (new); 1 unexpected.


## Known limitations / follow-ups

- Only two scenarios are registered today (`interface_admin_down`
  and `bgp_neighbor_removal`). The other Phase 2 fault scripts
  (ASN mismatch, bgpd restart, route missing, counter degradation)
  will register themselves as they are implemented.
- `bgp_neighbor_removal` has no evidence filter. The Phase 2B
  topology spike and the 2C neighbor-removal spike both found no
  BGP-specific syslog entries at default verbosity, so a filter is
  not obviously useful yet. The post-inject diagnosis output
  observed during the dispatch test mentioned PFC frame-counter
  warnings from baseline SONiC VS noise — addressing that with a
  scenario-specific filter is an open follow-up if the noise
  becomes a diagnosis-quality problem.
- No automated runner test harness yet; verification is manual
  command runs. A Phase 4 evaluation harness is the larger-scope
  way this gets addressed.
- The runner now auto-calls `scripts/configure_bgp.sh up`/`down` for
  BGP scenarios, while the standalone fault script
  (`faults/bgp_neighbor_removal.py`) keeps its fail-loud
  precondition behavior. Both patterns coexist: the runner
  absorbs the lab-lifecycle step; the standalone script remains a
  composable building block that does not surprise its caller with
  side effects on Docker networks or containers.
- **Next engineering step after this doc is an ASN mismatch
  spike, NOT immediate fault implementation.** Per
  `phase2/2C_CONTROL_PLANE_DECISION.md`, the actual FRR
  OPEN/NOTIFICATION JSON and log shape on an ASN mismatch needs to
  be captured against the two-container lab before the
  `bgp_asn_mismatch` fault script can be written.


## Final state

State at the end of this documentation session:

- Git working tree: `## main...origin/main` (clean, in sync).
- `sonic-vs-troubleshoot` is up; `vtysh show bgp summary json`
  returns `{}`.
- No `sonic-bgp-peer` container; no `sonic-bgp-lab` network.
- `Ethernet4` `admin_status` is `up`.

This is the same clean state the dispatch session left at
`39db908`, re-verified during this doc's writing.
