# Phase 2C Control Plane Decision: vtysh vs CONFIG_DB for BGP fault scripts

## Decision

Phase 2C BGP fault scripts will mutate BGP state via vtysh on
`sonic-vs-troubleshoot`, matching the path that
`scripts/configure_bgp.sh` uses for setup. CONFIG_DB + `bgpcfgd`
remains the canonical SONiC mutation path but is deferred for later
validation; using it now would create ambiguous evidence because
`bgpcfgd`'s behavior on this image has not been verified.

Recommended fault implementations for the first Phase 2C scenario:

    docker exec sonic-vs-troubleshoot vtysh \
        -c "configure terminal" \
        -c "router bgp 65000" \
        -c "no neighbor 10.10.10.2"

Restore re-adds the neighbor with the same parameters used at setup:

    docker exec sonic-vs-troubleshoot vtysh \
        -c "configure terminal" \
        -c "router bgp 65000" \
        -c "neighbor 10.10.10.2 remote-as 65001"

Both inject and restore poll `show bgp summary json` to confirm the
state transition before returning.


## Context

Two mutation paths exist on SONiC.

**vtysh** issues FRR commands directly. It bypasses `bgpcfgd`.
Changes are reflected immediately in FRR's running-config and in
`show bgp summary json`. Changes may or may not persist to CONFIG_DB,
which can create state drift between FRR and CONFIG_DB.

**CONFIG_DB + `bgpcfgd`** is the canonical SONiC path. Writes to
`CONFIG_DB BGP_NEIGHBOR|` keys are observed by `bgpcfgd`, which
generates FRR config and reloads `bgpd`. Slower and indirect, but
matches how a SONiC user would actually configure BGP through
`config` CLI or sonic-cfggen.

The Phase 2B setup script chose vtysh for three reasons documented
in `scripts/configure_bgp.sh`'s header: the topology spike
(`phase2/2B_TOPOLOGY_FINDINGS.md`) used vtysh and it worked,
`bgpcfgd`'s behavior in this Docker image has not been verified, and
vtysh produces immediate observable state changes.


## Why this choice for Phase 2C

Four reasons.

**Consistency with setup.** `scripts/configure_bgp.sh` uses vtysh.
Fault scripts using CONFIG_DB while setup uses vtysh would create a
mixed control plane where the inject/restore pair operates on
different layers than the baseline. The diagnosis agent would see
evidence that is harder to interpret, and a future reviewer trying
to reproduce a fault would have to reason about two control planes
at once.

**`bgpcfgd` validation is its own piece of work.** To use CONFIG_DB
for faults, we would first need to verify: that writes to
`CONFIG_DB BGP_NEIGHBOR|` are observed by `bgpcfgd` in this image,
that `bgpcfgd` correctly translates them into FRR config, that the
resulting FRR state matches what we expect, and that removing a
CONFIG_DB key cleanly removes the FRR neighbor. That is a spike
worth doing separately, not embedded in the first fault script.

**The collector already reads via vtysh.** `collect_bgp_summary` in
`collectors/sonic_state.py` calls `vtysh -c "show bgp summary json"`.
Using vtysh for mutation means the entire control loop (setup,
inject, observe, restore) is on the same plane. Evidence
interpretation is straightforward.

**The fault behavior we want to test is BGP-level**, not
`bgpcfgd`-level. Neighbor removal, ASN mismatch, and session state
changes are real BGP scenarios whether they originated in CONFIG_DB
or in vtysh. The narrator agent's diagnosis quality depends on the
BGP-level evidence, not on the route the change took to FRR.


## What this choice does NOT close

This decision is honest about its limits.

- Phase 2C fault scripts do NOT exercise the `bgpcfgd` daemon path.
  If `bgpcfgd` has bugs or behaves differently than FRR does, those
  bugs will not be surfaced by Phase 2C scenarios.

- The fault scripts are not testing a "real SONiC operator
  workflow" where a configuration change would propagate through
  CONFIG_DB. They test what happens when BGP state changes,
  regardless of how the change was originated.

- CONFIG_DB-side observability of the fault (a CONFIG_DB collector
  watching `BGP_NEIGHBOR|` keys) is not part of Phase 2C evidence.
  If we want CONFIG_DB-aware diagnosis later, we will need to
  either add a CONFIG_DB collector, or switch to CONFIG_DB
  mutation, or both.

**Scope clarification:** this decision is scoped to Phase 2C BGP
fault scripts. Phase 2D+ must re-evaluate if a scenario needs
CONFIG_DB-mediated behavior. A future phase MAY revisit the choice
and add CONFIG_DB-mediated faults as an additional scenario class,
likely after a separate `bgpcfgd` validation spike.


## Risks of this choice

**vtysh-mediated changes may not survive a `bgpd` restart cleanly.**
If `bgpcfgd` reloads from CONFIG_DB when `bgpd` starts, a
vtysh-injected change could be "reset" by the restart, depending on
what is in CONFIG_DB at restart time. This is a real Phase 2E
design consideration for the `bgpd_restart` scenario, not a Phase
2C blocker — but it should be in mind when scripting that scenario.

**CONFIG_DB and FRR running-config can drift.** If a future
reviewer looks at CONFIG_DB and sees a BGP neighbor that is not
actually in FRR (or vice versa), the inconsistency may look like a
bug when it is actually a consequence of vtysh-direct mutation.
Fault script comments should mention this so a confused reader can
locate the explanation quickly.

**ASN mismatch and route-missing scenarios in Phase 2D might want
different mutation paths.** If vtysh cannot cleanly model them (for
example, if a particular ASN-mismatch test requires a specific
sequence of CONFIG_DB writes), the Phase 2D plan must re-evaluate.
Do not assume vtysh fits every BGP scenario just because it fits
`bgp_neighbor_removal`.


## What we are NOT deciding tonight

Deferred to Phase 2C implementation work, not papered over.

- Exact fault script file naming (likely
  `faults/bgp_neighbor_removal.py` by analogy with
  `faults/interface_admin_down.py`, but not formally decided).
- Whether the fault script calls `scripts/configure_bgp.sh` itself
  as a prereq, assumes the lab is already up, or fails loudly with
  a clear error if it is not. The Phase 1 pattern was the third
  (fail loudly with a pointer to the right setup command); Phase
  2C should likely follow.
- How the runner `main.py` generalization handles scenario-specific
  prereqs (e.g., calling `configure_bgp.sh up` before BGP
  scenarios, then `down` after). That is a Phase 2C+ runner
  concern.
- Whether scenario-specific evidence filters analogous to the
  synthetic oper-error cascade filter in `main.py` are needed for
  BGP scenarios.
  The Phase 2B topology spike showed no new syslog entries from
  BGP session events at default verbosity, so a filter may not be
  necessary, but per-scenario log content must be checked when
  each fault is implemented.


## Next implementation step

Phase 2C, first scenario:

1. Write `faults/bgp_neighbor_removal.py` using vtysh mutation per
   the Decision section above. Pattern follows
   `faults/interface_admin_down.py`: inject + restore + status,
   with a polling helper that accommodates FRR's response time.
2. Test inject and restore against a running
   `scripts/configure_bgp.sh up` lab.
3. Capture the evidence shape the diagnosis agent will see
   post-inject, including the possibility that the peer disappears
   entirely from `show bgp summary json` (`peers` dict empty or the
   specific peer key missing, rather than a state field changing
   to `Idle`).
4. Validate that `collect_bgp_summary` handles the post-inject
   shape. The Phase 2B topology spike confirmed it handles
   Established; it has not been exercised on the
   neighbor-just-removed case.
5. Document findings in `phase2/2C_NEIGHBOR_REMOVAL_FINDINGS.md`
   following the structure of `phase2/2B_TOPOLOGY_FINDINGS.md`.
6. Only after the first fault works end-to-end against the live
   lab, consider whether to generalize `main.py` for scenario
   dispatch or to write the next fault script.


## Reference

- `phase2/2B_DECISION.md` — the Option C architectural decision
  that brought the two-container BGP lab into scope.
- `phase2/2B_TOPOLOGY_FINDINGS.md` — the manual procedure that
  proved Option C works and captured Established-state JSON.
- `scripts/configure_bgp.sh` — the Phase 2B automation; its header
  records the vtysh-not-CONFIG_DB choice on the setup side, which
  this document extends to fault scripts.
- `collectors/sonic_state.py` — `collect_bgp_summary` reads BGP
  state via `vtysh -c "show bgp summary json"`; the choice in this
  document keeps mutation and observation on the same plane.
