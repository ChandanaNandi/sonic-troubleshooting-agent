# Phase 2C BGP Neighbor Removal Findings

## Purpose

Document the first Phase 2C BGP fault scenario: removing neighbor
`10.10.10.2` from `router bgp 65000` on `sonic-vs-troubleshoot` via
vtysh, captured during a re-run of the fault script
(`faults/bgp_neighbor_removal.py`) against the two-container BGP
lab. Every output quoted below was captured during this session;
nothing is reconstructed from memory.

This work follows `phase2/2C_CONTROL_PLANE_DECISION.md`: Phase 2C
BGP faults mutate state via vtysh on the SUT, not via CONFIG_DB +
`bgpcfgd`. The `bgpcfgd` path is deferred until separately
validated.


## Test setup

The lab was brought up from a clean state using the Phase 2B
automation:

    cd ~/sonic-troubleshooting-agent
    ./scripts/configure_bgp.sh up

Tail of the script output:

    [configure_bgp] configuring SUT BGP via vtysh: router bgp 65000, neighbor 10.10.10.2 remote-as 65001
    [configure_bgp] polling SUT BGP for Established (up to 60s)
    [configure_bgp] BGP session Established

Baseline status from the fault script:

    $ python3 faults/bgp_neighbor_removal.py status
    established

Baseline `show bgp summary json` on the SUT (full output):

    {
      "ipv4Unicast": {
        "routerId": "172.17.0.4",
        "as": 65000,
        "vrfId": 0,
        "vrfName": "default",
        "tableVersion": 0,
        "ribCount": 0,
        "ribMemory": 0,
        "peerCount": 1,
        "peerMemory": 24072,
        "peers": {
          "10.10.10.2": {
            "hostname": "1900d3838167",
            "softwareVersion": "n/a",
            "remoteAs": 65001,
            "localAs": 65000,
            "version": 4,
            "msgRcvd": 3,
            "msgSent": 3,
            "tableVersion": 0,
            "outq": 0,
            "inq": 0,
            "peerUptime": "00:00:02",
            "peerUptimeMsec": 2000,
            "peerUptimeEstablishedEpoch": 1779575209,
            "pfxRcd": 0,
            "pfxSnt": 0,
            "state": "Established",
            "peerState": "Policy",
            "connectionsEstablished": 1,
            "connectionsDropped": 0,
            "idType": "ipv4"
          }
        },
        "failedPeers": 0,
        "displayedPeers": 1,
        "totalPeers": 1,
        "dynamicPeers": 0,
        "bestPath": { "multiPathRelax": "false" }
      }
    }


## Inject behavior

Command:

    python3 faults/bgp_neighbor_removal.py inject

Precondition: the peer must be in `established` state. If not, the
script raises and exits non-zero (see "Precondition and safety
behavior" below). Mutation, when the precondition is met, is the
vtysh equivalent of:

    configure terminal
    router bgp 65000
    no neighbor 10.10.10.2

Observed output:

    before: peer 10.10.10.2 state=established
    injecting: removing neighbor 10.10.10.2 via vtysh
    after:  peer 10.10.10.2 state=removed
    inject ok: neighbor 10.10.10.2 removed

Exit code: `0`.

Post-inject `show bgp summary json` on the SUT:

    {
    }

**This is the key Phase 2C finding for this scenario.** Removing the
only neighbor from the `router bgp 65000` block causes FRR to
collapse `show bgp summary json` to `{}` — the same shape an empty
`router bgp` block produces (observed in
`phase2/2B_TOPOLOGY_FINDINGS.md` and exercised by the
`sut_bgp_config_present` helper in `scripts/configure_bgp.sh`). The
fault script's `read_peer_state()` categorizes this case as
`"removed"`, not as `"noinstance"` or as a state machine value.


## Restore behavior

Command:

    python3 faults/bgp_neighbor_removal.py restore

Mutation, when the peer is not already established and the peer
fixture is reachable, is the vtysh equivalent of:

    configure terminal
    router bgp 65000
    neighbor 10.10.10.2 remote-as 65001

Observed output:

    before: peer 10.10.10.2 state=removed
    restoring: neighbor 10.10.10.2 remote-as 65001 via vtysh
    after:  peer 10.10.10.2 state=established
    restore ok: neighbor 10.10.10.2 is established

Exit code: `0`. Total wall-clock from `restore` invocation to
`Established` was under a few seconds (the script polls every 0.5s
with a 60s deadline; FRR re-established well within the deadline).

Post-restore peer state observed on the SUT:

    peer states: {'10.10.10.2': 'Established'}


## Precondition and safety behavior

Two failure paths were captured against the lab in its
`down (clean)` state.

### `inject` when the lab is down

Command:

    python3 faults/bgp_neighbor_removal.py inject

Observed output (stderr followed by stdout):

    error: BGP lab is not ready (peer state='removed', expected established). Run scripts/configure_bgp.sh up
    before: peer 10.10.10.2 state=removed

Exit code: `1`. After this failure,
`show running-config | grep '^router bgp'` returned nothing — the
script does not create any SUT-side BGP config when the precondition
check on `read_peer_state() == "established"` fails.

### `restore` when the lab is down

Command:

    python3 faults/bgp_neighbor_removal.py restore

Observed output:

    error: BGP lab is not ready; peer 10.10.10.2 is unreachable. Run scripts/configure_bgp.sh up
    before: peer 10.10.10.2 state=removed

Exit code: `1`. After this failure, `show running-config | grep
'^router bgp'` returned nothing and `show bgp summary json` returned
`{}` — no `router bgp 65000` block was created.

This safety guard was added during review of the original fault
script. The first version of `restore()` did not check peer
reachability and would have happily applied
`router bgp 65000` / `neighbor 10.10.10.2 remote-as 65001` from a
clean state, leaving a stale SUT-side BGP block after the failure.
The guard implementation is a single ICMP echo from the SUT to
`PEER_IP` using `docker exec sonic-vs-troubleshoot ping -c 1 -W 1
10.10.10.2`, wrapped in `_peer_reachable()`. It is placed after the
already-established early return (so the no-op path does not ping)
and before `_apply_add_neighbor()` (so no SUT config can be created
on failure).


## Collector behavior

`collectors/sonic_state.py:collect_bgp_summary` reads
`vtysh show bgp summary json` on the SUT and returns a structured
dict. Captured at both lab states:

### Baseline (lab up, neighbor Established)

    {
      "bgp_instance_present": true,
      "neighbors": [
        {
          "neighbor": "10.10.10.2",
          "asn": 65001,
          "state": "Established"
        }
      ],
      "source": "vtysh show bgp summary json"
    }

### Post-inject (neighbor removed, summary JSON is `{}`)

    {
      "bgp_instance_present": false,
      "neighbors": [],
      "source": "vtysh show bgp summary json"
    }

Both outputs are accurate relative to what FRR returned. The
collector handles both cases without raising and without producing
malformed output. **No code change was needed.**

### Important limitation

The post-inject collector output is accurate with respect to FRR,
but it loses intent and context. From a single snapshot of
`bgp_instance_present=False, neighbors=[]`, the collector cannot
distinguish among three different real-world situations:

- BGP was never configured on the switch.
- BGP exists but happens to have no neighbors.
- A neighbor was just intentionally removed (the Phase 2C scenario).

This limitation does **NOT** block Phase 2C. The runner that will
eventually drive the end-to-end flow can supply
before/after evidence and the scenario name as additional context
on the blackboard, and the diagnosis agent can interpret the
post-inject snapshot against the baseline. For a future single-
snapshot diagnosis use case (no baseline available), the collector
could be enriched by also consuming `show running-config` (or
`show bgp summary` text with its different empty-state formatting,
or a CONFIG_DB read). That enrichment is a Phase 2D+ concern; not
part of Phase 2C scope.


## Final cleanup

End-of-session teardown via the same Phase 2B automation:

    ./scripts/configure_bgp.sh down

Tail of the script output:

    [configure_bgp] removing SUT BGP config (no router bgp 65000)
    [configure_bgp] disconnecting SUT from sonic-bgp-lab
    [configure_bgp] removing peer container sonic-bgp-peer
    [configure_bgp] removing network sonic-bgp-lab
    [configure_bgp] down: clean state confirmed

Final state verified:

- `vtysh show bgp summary json` returns `{}`.
- `docker ps -a --filter name=sonic-bgp-peer` returns no rows.
- `docker network ls` does not list `sonic-bgp-lab`.


## What this scenario proves

- The two-container BGP lab from Phase 2B supports a real
  neighbor-removal fault on the SUT side end-to-end.
- vtysh mutation on the SUT works consistently with the vtysh
  setup performed by `scripts/configure_bgp.sh`. No control-plane
  mixing introduced.
- `restore` brings the session back to `Established` and the script
  polls correctly within the 60-second deadline.
- The current `collect_bgp_summary` handles both the
  Established-baseline and the post-inject `{}` cases without
  raising or producing malformed output.
- The precondition checks on both `inject` and `restore` reject
  clean-state invocations loudly and leave the SUT untouched.
  Specifically, the `_peer_reachable()` guard on `restore` prevents
  the script from creating a stale `router bgp 65000` block when the
  lab is down.


## What this scenario does NOT prove

- Does not exercise the CONFIG_DB + `bgpcfgd` path. The decision in
  `phase2/2C_CONTROL_PLANE_DECISION.md` defers that.
- Does not test ASN mismatch. That is a later Phase 2C/D scenario.
- Does not test route advertisement or withdrawal. Neither side
  advertises any prefix; `pfxRcd = 0` and `pfxSnt = 0` in the
  baseline JSON.
- Does not test `bgpd` restart. That is a separate Phase 2E
  scenario, with its own timing considerations.
- Does not yet integrate with `main.py` runner-side scenario
  dispatch. The fault script is callable standalone today; runner
  generalization is a separate concern.
- Does not test the collector's behavior on mid-life FRR states
  (`Idle`, `Connect`, `OpenSent`, `OpenConfirm`, or `Active` after
  a previously-Established session drops). The post-inject case
  here is the "peer entry removed from JSON entirely" case, not
  "peer entry present in non-Established state".
