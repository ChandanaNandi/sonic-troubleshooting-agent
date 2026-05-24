# Phase 2D ASN Mismatch Spike Findings

## Purpose

Evidence spike before writing any ASN-mismatch fault script. The
Phase 2C control-plane decision doc said the fault "can produce
OPEN/NOTIFICATION evidence, such as Bad Peer AS, if the
two-container setup reaches the point of exchanging BGP OPEN
messages" and required capturing the actual FRR JSON/log shape
before the scenario is written. This document records the captured
evidence from a single-session spike against the two-container BGP
lab.

Every output quoted below was captured during this spike. Nothing
is reconstructed from transcript memory or assumed from FRR
documentation.

The spike does NOT produce a fault script. The follow-up
`faults/bgp_asn_mismatch.py` is a separate commit, gated on this
finding.


## Baseline

Started from a clean state (no `sonic-bgp-lab` network, no
`sonic-bgp-peer` container, SUT BGP `{}`). Lab was brought up:

    scripts/configure_bgp.sh up
    # ...
    # [configure_bgp] BGP session Established

`show bgp summary json` from the SUT immediately after `up`
(decision-relevant fields excerpted; the full output is ~32 lines
and reproducible by re-running the spike):

    {
      "ipv4Unicast": {
        "as": 65000,
        "peers": {
          "10.10.10.2": {
            "remoteAs": 65001,
            "state": "Established",
            "peerUptime": "00:00:02",
            "connectionsEstablished": 1,
            "connectionsDropped": 0
          }
        },
        "failedPeers": 0
      }
    }

`show bgp neighbors 10.10.10.2 json` baseline (decision-relevant
fields):

    {
      "10.10.10.2": {
        "remoteAs": 65001,
        "bgpState": "Established",
        "connectionsEstablished": 1,
        "connectionsDropped": 0,
        "lastReset": "never",
        "messageStats": {
          "opensSent": 1,
          "opensRecv": 1,
          "notificationsSent": 0,
          "notificationsRecv": 0,
          "keepalivesSent": 1,
          "keepalivesRecv": 1
        }
      }
    }


## Injection method

Direct `remote-as` change worked without requiring remove+add.
The single vtysh invocation:

    docker exec sonic-vs-troubleshoot vtysh \
      -c "configure terminal" \
      -c "router bgp 65000" \
      -c "neighbor 10.10.10.2 remote-as 65002"

Exit code: 0. `show running-config` immediately afterwards confirmed
the change:

    router bgp 65000
     neighbor 10.10.10.2 remote-as 65002
    exit

The peer's actual remote AS is 65001 (configured by
`scripts/configure_bgp.sh up`), so the SUT was now expecting AS
65002 from a peer that would announce AS 65001. The OPEN exchange
would fail validation on the SUT side.

The spike did NOT need the remove+add fallback path described in
the spike plan.

After applying the change, waited 60 seconds for FRR to react
through its OPEN-retry cycle.


## Post-mismatch SUT evidence

`show bgp summary` (text view):

    IPv4 Unicast Summary:
    BGP router identifier 172.17.0.4, local AS number 65000 VRF default vrf-id 0
    BGP table version 0
    RIB entries 0, using 0 bytes of memory
    Peers 1, using 24 KiB of memory

    Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
    10.10.10.2      4      65002         8        14        0    0    0 00:01:25         Idle        0 N/A

    Total number of neighbors 1

`show bgp summary json` decision-relevant fields:

    {
      "ipv4Unicast": {
        "as": 65000,
        "peerCount": 1,
        "peers": {
          "10.10.10.2": {
            "remoteAs": 65002,
            "state": "Idle",
            "peerUptime": "00:01:25",
            "connectionsEstablished": 1,
            "connectionsDropped": 1
          }
        },
        "failedPeers": 1
      }
    }

Summary-level signals visible:

- `state: "Idle"` (transitioned from Established).
- `remoteAs: 65002` (now shows the wrong configured AS; this is the
  state held against the peer, not what the peer is advertising).
- `connectionsDropped: 1` (incremented from 0).
- `failedPeers: 1` (incremented from 0).

`show bgp neighbors 10.10.10.2 json` decision-relevant fields
(this is the key per-peer evidence):

    {
      "10.10.10.2": {
        "remoteAs": 65002,
        "bgpState": "Idle",
        "remoteRouterId": "0.0.0.0",
        "hostname": "Unknown",
        "connectionsEstablished": 1,
        "connectionsDropped": 1,
        "lastResetTimerMsecs": 103000,
        "lastErrorCodeSubcode": "0202",
        "lastNotificationReason": "OPEN Message Error/Bad Peer AS",
        "lastNotificationHardReset": false,
        "lastResetDueTo": "BGP Notification send",
        "lastResetCode": 14,
        "nextStartTimerDueInMsecs": 22000,
        "readThread": "off",
        "writeThread": "off",
        "messageStats": {
          "opensSent": 6,
          "opensRecv": 6,
          "notificationsSent": 6,
          "notificationsRecv": 0,
          "updatesSent": 1,
          "updatesRecv": 1,
          "keepalivesSent": 1,
          "keepalivesRecv": 1,
          "totalSent": 14,
          "totalRecv": 8
        }
      }
    }

The per-neighbor JSON exposes the mismatch in multiple distinct,
machine-readable fields:

- `lastErrorCodeSubcode: "0202"` — BGP NOTIFICATION code 2
  (OPEN Message Error) subcode 2 (Bad Peer AS) per the BGP-4
  protocol numbering.
- `lastNotificationReason: "OPEN Message Error/Bad Peer AS"` —
  human-readable string FRR emits.
- `lastResetDueTo: "BGP Notification send"` — the direction (SUT
  sent the NOTIFICATION because it observed the mismatch in the
  peer's OPEN).
- `messageStats.notificationsSent: 6` — FRR has already retried the
  OPEN/NOTIFICATION cycle six times during the 60-second wait.
- `readThread: "off"`, `writeThread: "off"` — the session is not
  actively reading/writing.

The text view of `show bgp neighbors` additionally surfaces a
human-readable reset reason in plain prose and includes a hex dump
of the received OPEN that triggered the NOTIFICATION:

    Last reset 00:01:43,  Notification sent (OPEN Message Error/Bad Peer AS)


## Log evidence

Three log sources were checked. All quoted negatively where
relevant.

**`docker logs sonic-vs-troubleshoot | grep -iE 'bgp|bad peer|notification|asn|as ' | tail -50`** —
only contained supervisord-process startup lines from container
boot at `2026-05-23 18:30:xx` (the `success: bgpd entered RUNNING
state` line and similar for the other SONiC services). **No
mismatch-related entries.** Same finding as the 2B/2C spikes.

**`grep -iE 'bgp|bad peer|notification|asn|as ' /var/log/syslog | tail -80`** —
returned only syncd FDB-warning lines (`process_packet_for_fdb_event:
skipping mac learn ...`) and orchagent port-state-change lines for
`Ethernet4` (the latter are from SUT being attached to / detached
from `sonic-bgp-lab` during prior runs, not from this spike's BGP
mismatch). **No FRR/BGP transition entries, no Bad Peer AS, no
NOTIFICATION strings.**

**`find /var/log -maxdepth 3 -type f | grep -Ei 'frr|bgp|zebra'`** —
**returned no files at all.** There is no `/var/log/frr/` directory
on this image, no `bgpd.log`, no `zebra.log`. FRR-on-SONiC-VS in
this image is not configured to write any service-specific log
file under `/var/log`.

Conclusion: **No BGP mismatch log evidence was found in the checked
locations (`docker logs`, `/var/log/syslog`, or FRR/BGP/Zebra files
under `/var/log`). The only captured mismatch evidence is
vtysh-queryable state.** This extends the syslog gap finding from
the 2B topology spike and the 2C neighbor-removal spike: the same
gap applies to NOTIFICATION events, not only to session-up events,
across every log source examined in this spike.

The narrator agent will get zero log evidence of the mismatch
unless the collector or runner explicitly reads the per-neighbor
JSON.


## Collector behavior

`collectors/sonic_state.py:collect_bgp_summary` reads
`vtysh show bgp summary json`. Applied to the mismatch summary
JSON above, it would return:

    {
      "bgp_instance_present": True,
      "neighbors": [
        {"neighbor": "10.10.10.2", "asn": 65002, "state": "Idle"}
      ],
      "source": "vtysh show bgp summary json"
    }

This is accurate — the peer is configured (with the wrong AS) and
the FRR FSM state is `Idle`. The collector handles the mismatch
shape without raising and without code change.

**Limitation:** the collector cannot distinguish "session is in
Idle because of ASN mismatch (Bad Peer AS NOTIFICATION exchanged)"
from "session is in Idle for any other reason" (TCP failure
backoff, manual `shutdown`, etc.). The distinguishing fields —
`lastErrorCodeSubcode`, `lastNotificationReason`,
`lastResetDueTo`, `messageStats.notificationsSent` — are in
`show bgp neighbors <ip> json`, which the current collector does
not call.

This is **not a blocker** for writing a Phase 2D fault script. The
fault can be injected via vtysh and observed via the existing
collector (peer transitions Established → Idle, that is
observable). It is a **diagnosis-quality limitation**: without
per-neighbor enrichment, the diagnosis agent will see
"peer 10.10.10.2 state=Idle" and report a session-down condition
without naming "ASN mismatch / Bad Peer AS" as the cause.

Two follow-up options if richer diagnosis is wanted in a later
phase:

- Extend `collect_bgp_summary` (or add `collect_bgp_neighbor`) to
  also read `show bgp neighbors <ip> json` for each peer and
  surface `lastNotificationReason` and `lastErrorCodeSubcode` into
  the structured evidence.
- Add a scenario-specific `evidence_filter` for `bgp_asn_mismatch`
  that pulls neighbor JSON during the AFTER snapshot and attaches
  it as additional evidence.

Both are out of scope for this spike. The decision belongs in the
fault-script commit or in a later evidence-quality pass.


## Restore and cleanup

Restore was attempted by reverting the SUT's neighbor remote-as to
the correct 65001:

    docker exec sonic-vs-troubleshoot vtysh \
      -c "configure terminal" \
      -c "router bgp 65000" \
      -c "neighbor 10.10.10.2 remote-as 65001"

Exit 0. After waiting 30 seconds, `show bgp summary json` reported:

    peer states: {'10.10.10.2': 'Idle'}

The session did NOT re-establish within the 30-second window. By
the time of the post-mismatch capture, FRR had already sent 6
NOTIFICATIONs, suggesting it had backed off the retry cadence;
`nextStartTimerDueInMsecs: 22000` in the per-neighbor JSON
indicated the retry timer is part of FRR's normal connect-retry
state machine. A 30s wait was insufficient given that history.

**Implication for the eventual fault-script `restore()`:** simply
correcting the configured remote-as is not enough to bring the
session back quickly. Options for the fault script to consider:

- `clear bgp neighbor 10.10.10.2 soft` (vtysh command) to force an
  immediate retry.
- Remove and re-add the neighbor (this would reset FRR's per-peer
  state more aggressively).
- Poll longer (60s+) and accept the slower convergence.

The spike did not pick one. Cleanup via the standard
`scripts/configure_bgp.sh down` worked regardless: the script's
`no router bgp 65000` invocation cleared all SUT-side BGP config,
followed by tearing down the peer container and the network.

Final cleanup output:

    [configure_bgp] removing SUT BGP config (no router bgp 65000)
    [configure_bgp] disconnecting SUT from sonic-bgp-lab
    [configure_bgp] removing peer container sonic-bgp-peer
    [configure_bgp] removing network sonic-bgp-lab
    [configure_bgp] down: clean state confirmed

Verified final state:

- `vtysh show bgp summary json` → `{}`
- `docker ps -a --filter name=sonic-bgp-peer` → no rows
- `docker network ls` → no `sonic-bgp-lab`


## Decision for future fault script

ASN mismatch is **viable** as a Phase 2D fault scenario.

What the eventual `faults/bgp_asn_mismatch.py` should do:

- **Inject** via direct vtysh change: `neighbor 10.10.10.2 remote-as
  <wrong>`. No need for remove+add fallback. Wrong AS should be
  distinctly different from both 65000 (SUT) and 65001 (peer's
  actual); 65002 used in this spike worked.
- **Poll for `state=Idle`** via the existing
  `collect_bgp_summary` shape, with a deadline (the transition
  takes ~10-60 seconds depending on where FRR is in its OPEN
  exchange).
- **Restore** by correcting the remote-as, then test whether
  `clear bgp neighbor 10.10.10.2 soft`,
  `clear bgp neighbor 10.10.10.2`, remove/re-add, or a longer poll
  is the most reliable way to force reconvergence. The spike
  observed that a simple revert alone left the session in Idle for
  the duration of FRR's connect-retry backoff (the spike's 30s
  wait did not see Established return); the spike did not test any
  of the listed reconvergence-forcing alternatives.
- **Precondition checks** matching the pattern in
  `faults/bgp_neighbor_removal.py`: container running, peer
  reachable for restore, peer state Established before inject.

The diagnosis-quality limitation (collector sees "Idle" not "Bad
Peer AS") is acceptable for the first version of the scenario. If
the diagnosis agent's output during runner-integration testing
proves too vague to be useful, the right next step is either
extending `collect_bgp_summary` to read per-neighbor JSON or
adding a scenario-specific `evidence_filter` that pulls
`lastNotificationReason` into the snapshot. Either change is its
own committed step, not part of the fault script.

The runner integration (`main.py` SCENARIOS registry entry) will
match the `bgp_neighbor_removal` pattern: `requires_bgp_lab=True`,
`evidence_filter=None` for the first version,
`post_inject_delay_seconds` around 1.0 (FRR's NOTIFICATION send is
fast once the OPEN exchange happens), and a clear
`manual_restore_command` for `--keep-fault`.
