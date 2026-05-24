# Phase 2D ASN Mismatch Restore Findings

## Purpose

The Phase 2D ASN mismatch evidence spike
(`phase2/2D_ASN_MISMATCH_SPIKE_FINDINGS.md`, commit `1caad7c`)
proved injection is viable but observed that a simple revert of
`remote-as` did not return the session to Established within a 30 s
wait under deeper FRR connect-retry backoff. This spike chooses
the restore method that the future `faults/bgp_asn_mismatch.py`
will use.

Every measurement quoted below was captured during this spike
session. No fault script is written in this commit.


## Method

For each candidate, one full lab cycle:

1. `scripts/configure_bgp.sh up` (peer reaches Established).
2. Inject mismatch via vtysh:
   `router bgp 65000 / neighbor 10.10.10.2 remote-as 65002`.
3. Poll `show bgp summary json` every 2 s for `state=Idle` (up to
   90 s).
4. Apply the candidate's restore commands.
5. Poll `show bgp summary json` every 2 s for `state=Established`
   (up to 120 s for the short-dwell run, 180 s for the deep-dwell
   run).
6. Record the elapsed time at the first observation of
   `Established` (or report timeout + last observed state).
7. `scripts/configure_bgp.sh down`.

Two dwell conditions were exercised:

- **Short dwell** (candidates A–D): the restore was applied
  immediately after the first observation of `Idle` (which
  consistently happened within the 2 s poll interval, i.e.
  "0 s" elapsed). Roughly one NOTIFICATION exchanged before
  restore.
- **Deep dwell** (candidates A_deep and C_deep): a 60 s wait was
  inserted between the inject and the restore so FRR could
  accumulate connect-retry backoff. By the time of restore,
  `messageStats.notificationsSent = 5` and
  `nextStartTimerDueInMsecs = 1000` (FRR was 1 s away from its
  next OPEN retry). This deep-dwell condition reproduces the
  failure mode observed in the earlier 2D evidence spike.

The deep-dwell runs were added because the short-dwell runs alone
could not distinguish the four candidates: they all reached
Established in ~2 s. Differentiation only appeared once FRR was in
deeper backoff.


## Candidate results

### A. Bare revert (correct remote-as only)

**Commands:**

    vtysh -c "configure terminal" \
          -c "router bgp 65000" \
          -c "neighbor 10.10.10.2 remote-as 65001"

**Short dwell:** Idle reached in <2 s; revert applied; **Established
in 2 s**.

**Deep dwell (60 s mismatch wait, 5 NOTIFICATIONs accumulated,
`nextStartTimerDueInMsecs: 1000`):** revert applied; **Established
in 15 s**. Worked, but noticeably slower than the short-dwell run.
The session re-established at FRR's next natural OPEN retry tick.

This is consistent with the earlier 2D evidence spike's observation
that a 30 s wait was insufficient — the earlier spike's timing
window probably caught FRR mid-backoff with a higher
`nextStartTimerDueInMsecs`. With a 180 s poll deadline, bare revert
is reliable in both conditions tested here.

### B. Revert + `clear bgp 10.10.10.2 soft`

**Commands accepted by vtysh:**

    vtysh -c "configure terminal" \
          -c "router bgp 65000" \
          -c "neighbor 10.10.10.2 remote-as 65001"
    vtysh -c "clear bgp 10.10.10.2 soft"

The first form (`clear bgp 10.10.10.2 soft`) was accepted with
exit 0. The fallback form (`clear bgp neighbor 10.10.10.2 soft`)
was not exercised in this spike.

**Short dwell:** **Established in 2 s**.

**Deep dwell:** not tested. Soft clear is documented in BGP
implementations as a route-refresh operation rather than a session
reset, so its expected effect on a session that is currently in
Idle (not Established) is unclear. The deep-dwell case would test
whether a soft clear is sufficient to escape the connect-retry
backoff; this spike did not capture that.

### C. Revert + `clear bgp 10.10.10.2` (hard)

**Commands accepted by vtysh:**

    vtysh -c "configure terminal" \
          -c "router bgp 65000" \
          -c "neighbor 10.10.10.2 remote-as 65001"
    vtysh -c "clear bgp 10.10.10.2"

The first form (`clear bgp 10.10.10.2`) was accepted with exit 0.
The fallback form (`clear bgp neighbor 10.10.10.2`) was not
exercised.

**Short dwell:** **Established in 2 s**.

**Deep dwell (same conditions as A_deep — 60 s wait, 5
NOTIFICATIONs, `nextStartTimerDueInMsecs: 1000`):**
**Established in 2 s**. The hard clear forced an immediate session
reset rather than waiting for FRR's natural retry tick.

This is the only candidate observed to keep its fast convergence
time under deep-dwell conditions. Roughly 7× faster than bare
revert in the deep-dwell run.

### D. Remove + re-add neighbor

**Commands:**

    vtysh -c "configure terminal" \
          -c "router bgp 65000" \
          -c "no neighbor 10.10.10.2" \
          -c "neighbor 10.10.10.2 remote-as 65001"

**Short dwell:** **Established in 2 s**.

**Deep dwell:** not tested. Remove + re-add removes the neighbor
entry from the FRR config entirely, which should reset FRR's
per-peer state more aggressively than a clear. Whether it is
faster than `clear bgp 10.10.10.2` under deep dwell, or just
equivalent, was not measured.


## Decision

**Recommended restore method for `faults/bgp_asn_mismatch.py`:
candidate C — revert remote-as, then `clear bgp 10.10.10.2`.**

Criteria considered:

- **Reliable.** Both short-dwell and deep-dwell runs reached
  Established within the polling deadline. The only candidate
  measured to retain its fast convergence (~2 s) under deep
  backoff.
- **Fast.** 2 s in both runs. The runner's polling loop won't
  block long on this step.
- **Least disruptive.** A vtysh `clear` is one extra command on
  top of the revert. It does not delete and recreate the neighbor
  entry (which would shake more state in FRR).
- **Simple to implement.** Two vtysh invocations: one
  `configure terminal / router bgp / neighbor ... remote-as 65001`,
  one `clear bgp 10.10.10.2`. Mirrors the style of the existing
  `faults/bgp_neighbor_removal.py` mutations.

Other candidates ranked:

- **A (bare revert)** also reliable in the two conditions tested
  (15 s under deep dwell). Recommended fallback if the explicit
  `clear` ever proves problematic for an unforeseen reason. The
  polling deadline must be generous (60–90 s) to accommodate the
  worst case.
- **D (remove + re-add)** worked under short dwell, untested under
  deep. Touches more FRR state than necessary. Reasonable
  candidate if C ever proves unreliable; not preferred otherwise.
- **B (clear soft)** worked under short dwell, untested under
  deep. Soft clear's semantics are "refresh routes on a working
  session", which does not obviously apply to a session that is
  currently in Idle. Not recommended without further testing.


## Implications for `faults/bgp_asn_mismatch.py`

The fault script should look very similar in shape to
`faults/bgp_neighbor_removal.py`. Specifically:

- **Inject method:** vtysh direct change of `remote-as`. A wrong
  value distinct from both 65000 (SUT) and 65001 (peer) — 65002
  used throughout this spike.

      vtysh -c "configure terminal" \
            -c "router bgp 65000" \
            -c "neighbor 10.10.10.2 remote-as 65002"

- **Inject polling:** poll `show bgp summary json` for
  `state=Idle`. Observed transition was within the 2 s poll
  interval in every run; a 30 s deadline is generous.

- **Restore method:** revert remote-as to 65001, then
  `clear bgp 10.10.10.2`.

      vtysh -c "configure terminal" \
            -c "router bgp 65000" \
            -c "neighbor 10.10.10.2 remote-as 65001"
      vtysh -c "clear bgp 10.10.10.2"

- **Restore polling:** poll `show bgp summary json` for
  `state=Established`. Observed convergence was 2 s in this
  spike's two C runs. A 60 s deadline gives ample margin and
  matches the bgp_neighbor_removal restore deadline.

- **Precondition checks:** same as `bgp_neighbor_removal`.
  `_check_container_running()` for the container; `_peer_reachable()`
  via ICMP on restore (avoids creating stale SUT-side config when
  the lab is down). Inject precondition: peer state must currently
  be `Established`.

- **Collector changes:** still not needed. The current
  `collect_bgp_summary` already exposes the `Idle`/`Established`
  transition via `state`. The richer per-neighbor JSON fields
  (`lastErrorCodeSubcode`, `lastNotificationReason`,
  `messageStats.notificationsSent`) that distinguish ASN-mismatch
  from generic session-down would improve diagnosis narrative
  quality but are not required for the fault script itself, and
  are explicitly out of scope here (a Phase 2D+ evidence
  enrichment concern, not a fault-script blocker).


## Final cleanup

End-of-spike state verified:

- `vtysh show bgp summary json` returns `{}`.
- `docker ps -a --filter name=sonic-bgp-peer` returns no rows.
- `docker network ls` does not list `sonic-bgp-lab`.
- `show running-config` has no `router bgp` block.

All six lab cycles (A, B, C, D short + A_deep + C_deep) ended with
`scripts/configure_bgp.sh down` returning the SUT to `{}` and
removing the peer container and the lab network. No residual state.
