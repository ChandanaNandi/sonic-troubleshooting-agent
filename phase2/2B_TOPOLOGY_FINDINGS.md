# Phase 2B Topology Setup Findings

Single-session implementation spike to bring up the two-container BGP
lab decided in `phase2/2B_DECISION.md` (Option C) and establish one
real BGP session between `sonic-vs-troubleshoot` (SUT) and a new FRR
peer container (test fixture). The spike's purpose is to validate
that the topology works, capture Established-state JSON from the
SUT, and check whether `collect_bgp_summary` handles the new output
shape. No configure script is produced here; that is the next
session's work.

All evidence from the SUT side is labeled "SUT evidence". Anything
captured from the peer container is labeled "fixture/debug" and is
not part of what the diagnosis agent should ever see.


## Preflight state

Before any spike action:

- Git working tree clean. `## main...origin/main`.
- `sonic-vs-troubleshoot` Up 3 hours.
- SUT BGP baseline: `vtysh show bgp summary json` returned `{}`.
- Existing Docker networks: `bridge`, `host`, `kind`, `none`. No
  `sonic-bgp-lab` present.
- Existing containers: `sonic-vs-troubleshoot`, `sonic-vs-fixed`,
  `batfish`. No `sonic-bgp-peer` present.
- No `frrouting/frr` image cached locally; Docker Hub pull required.

No conflicts. No prior-attempt artifacts to clean up.


## Decisions made for the seven open questions

| # | Question | Answer | One-line justification |
|---|---|---|---|
| 1 | FRR image | `frrouting/frr:latest` (pulled from Docker Hub during this spike) | Official FRR project image; BGP-complete; well-maintained. No local FRR image existed; surfaced and pulled explicitly. |
| 2 | Docker network | Custom bridge `sonic-bgp-lab`, subnet `10.10.10.0/24` | Avoids collision with the in-use `172.17.0.0/16` (default bridge) and `172.18.0.0/16` (kind); user-defined bridge enables static IPs. |
| 3 | AS / IP scheme | eBGP. SONiC AS `65000` at `10.10.10.3`, peer AS `65001` at `10.10.10.2`. Network gateway is `10.10.10.1`. | Distinct ASNs produce a real eBGP OPEN exchange. SONiC moved to `.3` because Docker reserves `x.x.x.1` as the bridge gateway (see "Discovered constraints" below). |
| 4 | Peer container lifecycle (broader question) | **Scenario-only** is the recommendation for the eventual script: peer runs only when BGP scenarios need it; non-BGP scenarios run against bare SUT without fixture state present. | Keeps fixture state from leaking into non-BGP runs. |
| 5 | Script ownership (next session) | **Single `scripts/configure_bgp.sh`** that handles both peer container lifecycle and BGP configuration on both sides, with internal factoring so a future split is possible | Peer lifecycle and BGP config are coupled in time (cannot configure peer BGP unless container exists); one script is simpler for the caller. |
| 6 | Route advertisement | **Deferred.** This spike configures no `network` statements and no `redistribute` on either side. | Route-missing scenario is Phase 2D/2E; not needed for an Established-state spike. |
| 7 | vtysh vs CONFIG_DB on SUT side | **This spike: vtysh** (lower risk; proven by prior Option A spike). **Next session's `scripts/configure_bgp.sh`: CONFIG_DB + `bgpcfgd`** (SONiC-canonical; matches how fault scripts will mutate state, consistent with Phase 1's `config interface shutdown` writing CONFIG_DB). | Spike validates topology cheaply; production script must use the path the fault scripts will operate on. The CONFIG_DB path is NOT validated by this spike — that is a Phase 2B implementation prerequisite. |


## Discovered constraint (not on the original question list)

Docker reserves the first usable address (`x.x.x.1`) in a
user-defined bridge subnet as the bridge gateway. The initial plan of
"SONiC at `10.10.10.1`, peer at `10.10.10.2`" failed at
`docker network connect ... --ip 10.10.10.1` with
`Error response from daemon: Address already in use`. SONiC was
reassigned to `10.10.10.3`. Peer at `10.10.10.2` was accepted because
`.2` was free. Worth knowing for the configure script.


## Setup commands (reproducible)

Each command is labeled with the side it ran against. **SUT** =
`sonic-vs-troubleshoot`; **fixture** = `sonic-bgp-peer` or the
Docker host context for network/lifecycle work.

    # fixture: pull FRR image
    docker pull frrouting/frr:latest

    # fixture: create the BGP lab network
    docker network create sonic-bgp-lab --subnet 10.10.10.0/24

    # fixture: start the peer container
    docker run -d --name sonic-bgp-peer \
        --network sonic-bgp-lab --ip 10.10.10.2 \
        --privileged frrouting/frr:latest

    # fixture: attach SONiC to the BGP lab network at 10.10.10.3
    # (10.10.10.1 is the bridge gateway, not assignable)
    docker network connect sonic-bgp-lab --ip 10.10.10.3 \
        sonic-vs-troubleshoot

    # fixture: enable bgpd on the peer (FRR image ships bgpd=no)
    docker exec sonic-bgp-peer sh -c \
        "sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons"
    docker exec sonic-bgp-peer /usr/lib/frr/bgpd -d -A 127.0.0.1

    # fixture: configure peer BGP
    docker exec sonic-bgp-peer vtysh \
        -c "configure terminal" \
        -c "router bgp 65001" \
        -c "neighbor 10.10.10.3 remote-as 65000"

    # SUT: configure BGP on the SONiC side (vtysh, per Q7)
    docker exec sonic-vs-troubleshoot vtysh \
        -c "configure terminal" \
        -c "router bgp 65000" \
        -c "neighbor 10.10.10.2 remote-as 65001"

    # wait ~30s for the session to converge to Established

Reachability test (before BGP config): SUT-side ping
`sonic-vs-troubleshoot -> 10.10.10.2`, 3/3 received, ~0.14 ms RTT.
Reverse direction also succeeded — 3/3 received, ~0.30 ms RTT (this
is fixture/debug, used to confirm L3 connectivity before configuring
BGP).


## Captured SUT evidence (from sonic-vs-troubleshoot)

### `show bgp summary json` — full output

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
            "hostname": "9c75b959c189",
            "softwareVersion": "n/a",
            "remoteAs": 65001,
            "localAs": 65000,
            "version": 4,
            "msgRcvd": 3,
            "msgSent": 3,
            "tableVersion": 0,
            "outq": 0,
            "inq": 0,
            "peerUptime": "00:00:43",
            "peerUptimeMsec": 43000,
            "peerUptimeEstablishedEpoch": 1779571756,
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

The critical field is `peers["10.10.10.2"]["state"] = "Established"`.

### `show bgp summary` — text equivalent

    IPv4 Unicast Summary:
    BGP router identifier 172.17.0.4, local AS number 65000 VRF default vrf-id 0
    BGP table version 0
    RIB entries 0, using 0 bytes of memory
    Peers 1, using 24 KiB of memory

    Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
    10.10.10.2      4      65001         3         3        0    0    0 00:00:43     (Policy) (Policy) N/A

    Total number of neighbors 1

Worth noting: the text view shows `(Policy)` in the State column for
an Established peer when ebgp-requires-policy is in effect (no
inbound/outbound policy applied → updates discarded). The JSON above
still reports `state: "Established"`. The text format is misleading
for state but the JSON is unambiguous.

### `show bgp neighbors 10.10.10.2 json` — decision-relevant fields

The full output is ~140 lines including `neighborCapabilities`,
`gracefulRestartInfo`, `prefixStats`, `addressFamilyInfo.ipv4Unicast`,
and per-thread state. Reproducible by re-running the spike. The
fields below are the ones used by anything in this document.

    {
      "10.10.10.2": {
        "neighborAddr": "10.10.10.2",
        "remoteAs": 65001,
        "localAs": 65000,
        "nbrExternalLink": true,
        "hostname": "9c75b959c189",
        "bgpVersion": 4,
        "remoteRouterId": "10.10.10.2",
        "localRouterId": "172.17.0.4",
        "bgpState": "Established",
        "bgpTimerUpMsec": 50000,
        "bgpTimerUpString": "00:00:50",
        "bgpTimerUpEstablishedEpoch": 1779571756,
        "connectionsEstablished": 1,
        "connectionsDropped": 0,
        "lastResetTimerMsecs": 51000,
        "lastResetDueTo": "No path to specified Neighbor",
        "lastResetCode": 27,
        "hostLocal": "10.10.10.3",
        "portLocal": 37784,
        "hostForeign": "10.10.10.2",
        "portForeign": 179,
        "bgpConnection": "sharedNetwork",
        "readThread": "on",
        "writeThread": "on",
        "messageStats": {
          "opensSent": 1,
          "opensRecv": 1,
          "notificationsSent": 0,
          "notificationsRecv": 0,
          "updatesSent": 1,
          "updatesRecv": 1,
          "keepalivesSent": 1,
          "keepalivesRecv": 1,
          "totalSent": 3,
          "totalRecv": 3
        },
        "addressFamilyInfo": {
          "ipv4Unicast": {
            "acceptedPrefixCounter": 0,
            "sentPrefixCounter": 0,
            "commAttriSentToNbr": "extendedAndStandard",
            "inboundEbgpRequiresPolicy": "Inbound updates discarded due to missing policy",
            "outboundEbgpRequiresPolicy": "Outbound updates discarded due to missing policy"
          }
        }
      }
    }

`messageStats.opensSent = 1` and `opensRecv = 1` confirm the OPEN
exchange happened — the prerequisite that Option A could never meet.
`connectionsEstablished = 1`, `readThread = "on"`, `writeThread =
"on"`, and the four-tuple (`hostLocal:portLocal`,
`hostForeign:portForeign`) all corroborate a live TCP/179 session.

### Log evidence (both sources)

`docker logs sonic-vs-troubleshoot 2>&1 | grep -i bgp | tail -30`
returned only the two bgpd startup lines from container boot 3h
earlier:

    2026-05-23 18:30:37,925 INFO spawned: 'bgpd' with pid 766
    2026-05-23 18:30:39,137 INFO success: bgpd entered RUNNING state, process has stayed up for > than 1 seconds (startsecs)

`grep -i bgp /var/log/syslog | tail -50` returned only the six
container-boot-related lines (timestamps `18:30:xx`). No new entries
appeared after configuring BGP, after the peer's bgpd came up, or
after the session reached Established.

**This is a load-bearing finding for Phase 2 scenario design.** The
narrator agent will have no syslog-based evidence for "BGP session
came up" or "BGP session is currently Established". Whether
FRR-on-SONiC-VS is emitting BGP session-state events to a different
log destination (for example, a separate `bgpd.log` under
`/var/log/frr/` if one exists) was not investigated in this spike.
That investigation is a precondition for any scenario whose
diagnosis would rely on log evidence of session transitions.

### Reachability tests captured at L3 (before BGP)

SUT-side ping `10.10.10.3 -> 10.10.10.2`: 3 packets transmitted, 3
received, 0% loss, rtt avg 0.144 ms.

(Reverse-direction ping from the peer is recorded in
"Fixture/debug observations" below.)


## Fixture/debug observations (from peer container)

These are NOT SUT evidence. They were captured only to confirm the
fixture was healthy and that L3 connectivity existed.

- Peer's `/etc/frr/daemons` initially had `bgpd=no`. Default for
  the `frrouting/frr:latest` image. Required edit-and-start before
  any BGP configuration would work.
- After enabling, peer processes: `watchfrr zebra staticd`,
  `zebra -d -F traditional -A 127.0.0.1 -s 90000000`, and (newly
  started) `bgpd -d -A 127.0.0.1`. `watchfrr` was NOT updated to
  monitor bgpd; bgpd was started directly. This works for the spike
  but means a peer-bgpd crash would not be auto-restarted by
  watchfrr. The configure script for the next session should either
  restart watchfrr with the updated daemon list, or use a different
  startup mechanism that keeps watchfrr in the loop.
- Peer vtysh emits a non-fatal warning:
  `% Can't open configuration file /etc/frr/vtysh.conf due to 'No
  such file or directory'.` The configuration was applied anyway
  and was visible in `show running-config`. Worth knowing; not a
  blocker.
- Peer-side reachability: ping `10.10.10.2 -> 10.10.10.3` succeeded
  3/3, ~0.30 ms RTT.
- Peer's running-config after configuration: `router bgp 65001` /
  `neighbor 10.10.10.3 remote-as 65000`.


## Collector validation

The current `collect_bgp_summary` in
`collectors/sonic_state.py` reads `vtysh show bgp summary json`,
iterates over `data["ipv4Unicast"]["peers"]` and
`data["ipv6Unicast"]["peers"]`, and for each peer extracts
`remoteAs` → `asn` and `state` → `state`. It also marks
`bgp_instance_present = True` when peers are present or when any
address-family dict contains an `as` key.

Applied to this spike's captured `show bgp summary json`:

| Collector reads | Captured JSON has | Result |
|---|---|---|
| `data["ipv4Unicast"]` | present (dict) | iterated |
| `data["ipv4Unicast"]["as"]` | `65000` | `bgp_instance_present=True` |
| `data["ipv4Unicast"]["peers"]` | dict with key `"10.10.10.2"` | iterated |
| `peer["remoteAs"]` | `65001` | `asn = 65001` |
| `peer["state"]` | `"Established"` | `state = "Established"` |
| `data["ipv6Unicast"]` | absent | no peers added |

Predicted collector return value:

    {
      "bgp_instance_present": True,
      "neighbors": [
        {"neighbor": "10.10.10.2", "asn": 65001, "state": "Established"}
      ],
      "source": "vtysh show bgp summary json"
    }

This is exactly the shape Phase 2 scenarios need. **The collector
handles Established-state JSON correctly with no changes.** No
modification to `collectors/sonic_state.py` is required for the
neighbor-removal, ASN-mismatch, or `bgpd_restart` scenarios on the
basis of this Established-state output.

Caveats for the collector that this spike does NOT exercise and
that Phase 2 will need to revisit:

- The richer per-neighbor JSON from `show bgp neighbors <ip> json`
  (capabilities, gracefulRestart info, messageStats, addressFamily
  policy) is not consumed by the current collector. If a future
  scenario needs to diagnose graceful-restart or capability-related
  faults, the collector would need a new endpoint or a new function.
- The collector has not been exercised on non-Established mid-life
  states (Idle, Connect, OpenSent, OpenConfirm, Active after a real
  session drops). The Phase 2C neighbor-removal scenario will be
  the first to do that.
- `ipv6Unicast` is untouched by this spike.


## Final state

End-of-spike teardown was performed. State now:

- SUT: `show bgp summary json` returns `{}`. `show running-config`
  has no `router bgp` block. `sonic-vs-troubleshoot` retains only
  its original `eth0` (`172.17.0.4/16` on default bridge) and `lo`
  (`127.0.0.1/8`). It was disconnected from `sonic-bgp-lab` before
  the network was removed.
- Peer: `sonic-bgp-peer` container removed (`docker rm -f`).
- Network: `sonic-bgp-lab` removed (`docker network rm`).
- Docker network list back to the original four: `bridge`, `host`,
  `kind`, `none`. No leftover artifacts.

The choice to tear down rather than leave the fixture running: the
next session's work is `scripts/configure_bgp.sh`, which must itself
bring up the peer and network from a cold start. Leaving the
fixture in place would let the script silently rely on
already-running state. A cold-start environment forces the script
to be honest.


## What this spike does NOT establish

- Whether the CONFIG_DB + `bgpcfgd` path produces the same FRR
  state and JSON as the vtysh path used here. The recommendation
  for `scripts/configure_bgp.sh` is CONFIG_DB; the spike did NOT
  validate it.
- ASN mismatch behavior. Will be exercised in Phase 2D.
- Route advertisement / withdrawal evidence. Neither side
  advertised any prefix; `pfxRcd = 0`, `pfxSnt = 0`. Phase 2D/2E
  concern.
- Multi-peer / multi-session behavior. Single peer only.
- `bgpd_restart` timing on either side. Phase 2E concern.
- Whether FRR-on-SONiC-VS writes BGP session-state events to any
  log destination other than `/var/log/syslog` (the configured grep
  source). Worth investigating before any scenario whose diagnosis
  needs log evidence of BGP transitions.
- Whether `watchfrr` on the peer side handles a `bgpd` crash now
  that bgpd was started directly outside watchfrr's daemon list.
- The actual `scripts/configure_bgp.sh` script — explicitly NOT
  written in this spike. That is the next session's work.
