# Phase 2B BGP Baseline Spike — Findings

Time-boxed spike comparing two single-container BGP baseline designs
on `sonic-vs-troubleshoot`, run in one session on 2026-05-23. Goal
was to gather enough captured evidence to choose between Option A
(neighbor to a non-existent peer) and Option B (FRR self-peering via
loopback) for the Phase 2 BGP scenarios. Option C (second FRR
container) was explicitly out of scope for this spike and remains a
scope decision for human review.

Everything in this document is derived from command output captured
during this session. No prior assumptions or "what FRR usually does"
inferences appear here.


## Methodology and caveats

The spike used `vtysh` directly (configure terminal + commands) to
set up BGP state. This is acceptable for the goal of discovering FRR
JSON shapes and answering whether a baseline is viable at all, but
it does NOT validate the Phase 2B target path of CONFIG_DB
`BGP_NEIGHBOR` entries flowing through `bgpcfgd` into FRR. The
future `scripts/configure_bgp.sh` will need to either use the SONiC
CONFIG_DB mechanism or explicitly justify why vtysh is acceptable
for test setup.

`collect_bgp_summary` in `collectors/sonic_state.py` only consumes
`show bgp summary json`. The `show bgp neighbors <ip> json` output
captured below is included as evidence of what the per-neighbor
detail looks like, but it is not consumed by the current collector.
A future enrichment of the collector could pull from it, but that is
out of scope for this spike.

Starting baseline before the spike: `show bgp summary json` returned
`{}`, `bgpd` was RUNNING for 2h 21m, and `lo` had only the default
`127.0.0.1/8` and `::1/128` addresses.


## Option A — neighbor to a non-existent peer

### Configuration applied

    docker exec sonic-vs-troubleshoot vtysh \
      -c "configure terminal" \
      -c "router bgp 65000" \
      -c "neighbor 192.0.2.1 remote-as 65001"

`192.0.2.1` is in TEST-NET-1 (RFC 5737) and was not routable in this
environment. The configuration was accepted without error. Verified
in `show running-config`:

    router bgp 65000
     neighbor 192.0.2.1 remote-as 65001
    exit

Waited 30 seconds for FRR to attempt connection and settle.

### `show bgp summary` (text)

    IPv4 Unicast Summary:
    BGP router identifier 172.17.0.4, local AS number 65000 VRF default vrf-id 0
    BGP table version 0
    RIB entries 0, using 0 bytes of memory
    Peers 1, using 24 KiB of memory

    Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
    192.0.2.1       4      65001         0         0        0    0    0    never       Active        0 N/A

    Total number of neighbors 1

### `show bgp summary json`

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
          "192.0.2.1": {
            "softwareVersion": "n/a",
            "remoteAs": 65001,
            "localAs": 65000,
            "version": 4,
            "msgRcvd": 0,
            "msgSent": 0,
            "tableVersion": 0,
            "outq": 0,
            "inq": 0,
            "peerUptime": "never",
            "peerUptimeMsec": 0,
            "pfxRcd": 0,
            "pfxSnt": 0,
            "state": "Active",
            "peerState": "Policy",
            "connectionsEstablished": 0,
            "connectionsDropped": 0,
            "idType": "ipv4"
          }
        },
        "failedPeers": 1,
        "displayedPeers": 1,
        "totalPeers": 1,
        "dynamicPeers": 0,
        "bestPath": { "multiPathRelax": "false" }
      }
    }

### `show bgp neighbors 192.0.2.1 json` (decision-relevant fields)

The full captured output is roughly 60 lines, including
`addressFamilyInfo.ipv4Unicast`, `prefixStats`, `gracefulRestartInfo`,
and per-thread state. It is reproducible by re-running the configure
step and the same vtysh query. The fields below are the ones this
document later uses for evidence; the rest of the output exists but
is not load-bearing for any conclusion drawn here.

    {
      "192.0.2.1": {
        "neighborAddr": "192.0.2.1",
        "remoteAs": 65001,
        "localAs": 65000,
        "nbrExternalLink": true,
        "bgpVersion": 4,
        "remoteRouterId": "0.0.0.0",
        "localRouterId": "172.17.0.4",
        "bgpState": "Active",
        "connectionsEstablished": 0,
        "connectionsDropped": 0,
        "lastResetTimerMsecs": 42000,
        "lastResetDueTo": "No path to specified Neighbor",
        "lastResetCode": 27,
        "connectRetryTimer": 60,
        "nextConnectTimerDueInMsecs": 49000,
        "readThread": "off",
        "writeThread": "off",
        "messageStats": {
          "depthInq": 0,
          "depthOutq": 0,
          "opensSent": 0,
          "opensRecv": 0,
          "notificationsSent": 0,
          "notificationsRecv": 0,
          "updatesSent": 0,
          "updatesRecv": 0,
          "keepalivesSent": 0,
          "keepalivesRecv": 0,
          "routeRefreshSent": 0,
          "routeRefreshRecv": 0,
          "capabilitySent": 0,
          "capabilityRecv": 0,
          "totalSent": 0,
          "totalRecv": 0
        }
      }
    }

`lastResetDueTo: "No path to specified Neighbor"` and
`lastResetCode: 27` identify the never-reachable state.
`messageStats.opensSent = 0` and `messageStats.opensRecv = 0` confirm
no OPEN message exchange ever happened — which is what the
conclusion's note about ASN-mismatch unobservability rests on.
The text version of the same query also shows
`BGP state = Active` and a never-ending connect retry cycle.

### Observed neighbor state

`Active` (never reached `Established`). This matches what FRR is
expected to do when the TCP connection cannot be opened.

### Log evidence

`docker logs sonic-vs-troubleshoot 2>&1 | grep -i bgp | tail -20`
returned only two lines, both from container startup 2h 21m before
the spike:

    2026-05-23 18:30:37,925 INFO spawned: 'bgpd' with pid 766
    2026-05-23 18:30:39,137 INFO success: bgpd entered RUNNING state, process has stayed up for > than 1 seconds (startsecs)

`grep -i bgp /var/log/syslog | tail -50` returned six lines, all
from container startup (timestamps `18:30:xx`):

    May 23 18:30:05 NOTICE #coppmgrd: setCoppTrapStateOk: Publish bgp(ok) to state db
    May 23 18:30:23 NOTICE #fpmsyncd: setWarmStartState: bgp warm start state changed to disabled
    May 23 18:30:37 INFO #supervisord: spawned: 'bgpd' with pid 766
    May 23 18:30:37 INFO #supervisord: bgpd ... BGP: [YDG3W-JND95] FD Limit set: 1048576 ...
    May 23 18:30:39 INFO #supervisord: success: bgpd entered RUNNING state
    May 23 18:30:37 INFO #supervisord: message repeated 2 times: [ FD Limit set: ... ]

**No log entries appeared in either source between the
configure-and-wait and the capture.** Adding a neighbor and watching
FRR retry-connect for 30s produced zero observable syslog evidence.
This matters for Phase 2 BGP scenarios: log-based diagnosis of
connection failures will not work against this baseline.

### Parser compatibility (Option A)

`collectors/sonic_state.py:collect_bgp_summary` reads `ipv4Unicast.peers`
and `ipv6Unicast.peers`, extracting `remoteAs` and `state` per
peer entry. Given Option A's `show bgp summary json` output, the
collector would produce:

    {
      "bgp_instance_present": True,
      "neighbors": [
        {"neighbor": "192.0.2.1", "asn": 65001, "state": "Active"}
      ],
      "source": "vtysh show bgp summary json"
    }

`bgp_instance_present=True` is driven both by the non-empty peers
dict and by the `"as": 65000` key under `ipv4Unicast`. The current
parser handles this output without modification.

### Teardown

    docker exec sonic-vs-troubleshoot vtysh \
      -c "configure terminal" -c "no router bgp 65000"

After teardown, `show bgp summary json` returned `{}` and
`show running-config` had no `router bgp` or `neighbor` lines. Clean.


## Option B — FRR self-peering via loopback

### Configuration attempt

    docker exec sonic-vs-troubleshoot vtysh \
      -c "configure terminal" \
      -c "interface lo" \
      -c "ip address 10.255.255.1/32"

The loopback address assignment succeeded; `ip addr show lo`
confirmed `inet 10.255.255.1/32 brd 10.255.255.1 scope global lo`.

Then the self-neighbor attempt:

    docker exec sonic-vs-troubleshoot vtysh \
      -c "configure terminal" \
      -c "router bgp 65000" \
      -c "neighbor 10.255.255.1 remote-as 65000" \
      -c "neighbor 10.255.255.1 update-source lo"

FRR responded:

    % Can not configure the local system as neighbor

This is a hard configuration-time rejection, not a runtime failure.
FRR refuses to register a neighbor that resolves to a local address.

Per the spike's stop conditions (single self-peer attempt; do not
invent multi-VRF or separate-ASN variations), no further
configuration was tried.

### Post-rejection state captured

`router bgp 65000` was created successfully before the neighbor
command was rejected. The running-config shows it as an empty
instance:

    interface lo
     ip address 10.255.255.1/32
    exit
    !
    router bgp 65000
    exit

`show bgp summary json` returned:

    { }

This is identical to a no-BGP-instance-at-all output as far as the
current collector is concerned — the empty `router bgp 65000` block
is invisible to it.

`show bgp neighbors 10.255.255.1 json` returned a JSON shape the
collector has not previously seen:

    { "bgpNoSuchNeighbor": true }

The text version returned `% No such neighbor in this view/vrf`.

### Log evidence

Neither `docker logs ... grep -i bgp` nor `grep -i bgp
/var/log/syslog` showed any new entries related to the rejection or
the configuration attempts. The only BGP-related log entries
remained the same six startup messages from 18:30.

### Parser compatibility (Option B)

`collect_bgp_summary` receives `{}` and returns
`{"bgp_instance_present": False, "neighbors": [], "source": ...}`.
The empty BGP instance in running-config is invisible. This is
consistent with the baseline state and is not a parser bug for this
output shape.

`{"bgpNoSuchNeighbor": true}` from `show bgp neighbors <ip> json` is
a new shape, but the current collector does not consume that
endpoint, so it is not relevant to today's parser surface.

### Teardown

    docker exec sonic-vs-troubleshoot vtysh \
      -c "configure terminal" -c "no router bgp 65000"
    docker exec sonic-vs-troubleshoot vtysh \
      -c "configure terminal" -c "interface lo" \
      -c "no ip address 10.255.255.1/32"

After teardown, `show bgp summary json` returned `{}`, running-config
had no BGP or extra loopback address, and `ip addr show lo` was back
to the baseline two addresses. Clean.


## Conclusion

**Option A is viable only as a weak single-container BGP baseline;
Option B is not viable at all.** Specifically:

- Option A configures cleanly, produces a non-empty BGP summary JSON
  that the current `collect_bgp_summary` parser handles without
  modification, and produces a stable `Active` neighbor state. It
  also produces a usable per-neighbor JSON with
  `lastResetDueTo: "No path to specified Neighbor"` if a future
  collector enrichment wants to consume `show bgp neighbors <ip> json`.

- Option B was rejected by FRR at configuration time with
  `% Can not configure the local system as neighbor`. The single
  self-peer attempt described in the spike plan failed
  deterministically. Per the spike's stop conditions, no variations
  were tried.

**What Option A does not give us.** Per the Phase 2 PLAN's risk
section for scenarios 1 and 2:

- Scenario 1 (BGP neighbor removal): observable but weak. Removing a
  neighbor that was never `Established` produces a less
  interesting diagnosis than removing a working session.
- Scenario 2 (BGP ASN mismatch): not meaningfully observable.
  ASN mismatch is detected during the OPEN message exchange, which
  did not happen against an unreachable peer in this spike (the
  captured `messageStats.opensRecv = 0`, `opensSent = 0` confirms
  this directly).
- Scenario 3 (`bgpd` restart): unaffected by baseline choice; works
  with Option A.
- Scenario 4 (route missing): not observable via BGP without a peer
  that advertises routes. Static routes would be a separate
  mechanism, not exercising BGP.

**The decision the spike does not make.** This finding does not
silently choose between accepting Option A's weaker scenarios and
escalating to Option C (second FRR container). Per the four working
answers for Phase 2B, that decision is explicitly reserved for human
review.


## Open questions remaining

These need answers before Phase 2C starts.

1. **Accept Option A's weak baseline, or escalate to Option C?**
   Option A supports `bgpd_restart` (scenario 3) cleanly. It supports
   `bgp_neighbor_removal` (scenario 1) in a degraded form. It does
   not meaningfully support `bgp_asn_mismatch` (scenario 2) or the
   BGP-driven path of `route_missing` (scenario 4). Option C would
   support all four well but expands Phase 2 scope beyond
   single-container, which the project has not committed to.

2. **If Option A is accepted, does scenario 2 (ASN mismatch) get cut
   from Phase 2, or get implemented as a "configure mismatched ASN,
   observe that the session still does not establish, diagnose that
   no OPEN exchange occurred" weak version?** The weak version is
   honest but produces a less interesting diagnosis than the planned
   scenario.

3. **Does scenario 4 (route missing) pivot to a static-route
   mechanism under Option A, or get cut?** Static routes do not
   exercise BGP, which changes the scenario's character even if the
   implementation is straightforward.

4. **Configuration mechanism for `scripts/configure_bgp.sh`.**
   This spike used `vtysh -c "configure terminal" ...` directly,
   which is not the intended SONiC mechanism (CONFIG_DB
   `BGP_NEIGHBOR` + bgpcfgd). The actual configure script must
   either go through CONFIG_DB or explicitly document why vtysh is
   the chosen path for test setup. The spike output is sufficient to
   know the FRR-side state and JSON shapes; it does not validate
   that those same shapes appear when BGP is configured through
   CONFIG_DB. Phase 2B implementation will need to verify.


## State at end of spike

Final cleanup verification was performed at the end of each option.
At session end, `sonic-vs-troubleshoot` is back to its pre-spike
state: `show bgp summary json` returns `{}`, no BGP configuration in
running-config, and `lo` has only `127.0.0.1/8` and `::1/128`. The
next session starts clean.
