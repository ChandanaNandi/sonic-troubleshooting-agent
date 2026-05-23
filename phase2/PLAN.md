# Phase 2 Plan

This is a planning document, not an implementation. It captures the
dependency map, prerequisites, scenario order, collector and runner
changes, risks, and effort estimates for Phase 2, so the actual work
can be reviewed and approved in pieces.

Phase 2 adds five fault scenarios on top of the Phase 1 baseline
(`interface_admin_down`):

1. BGP neighbor removal (CONFIG_DB edit)
2. BGP ASN mismatch (CONFIG_DB edit)
3. `bgpd` container restart
4. Route missing / prefix not advertised
5. Counter / log-based degradation (packet drops, interface errors)

It also generalizes `main.py` to dispatch by scenario name. The
blackboard and the diagnosis agent stay unchanged — they are
scenario-agnostic by design.

Phase 2 does NOT include: new agents (Phase 3), an evaluation harness
(Phase 4), or multi-switch topology.


## 1. Dependency map

The five scenarios are not five independent units of work. Four of
them share a single upstream dependency: BGP must be configured on
`sonic-vs-troubleshoot`, which Phase 1 explicitly left unconfigured
(`vtysh show bgp summary json` returns `{}`). The fifth depends on
whether SONiC VS can produce observable counter changes at all,
which Phase 1 also did not confirm.

    PHASE 1 BASELINE
      sonic-vs-troubleshoot operational (bringup.sh)
      collect_bgp_summary exercised only on {} baseline
      interface counters confirmed empty on SONiC VS

    PHASE 2 PREREQUISITE WORK (Phase 2B)
      BGP lab topology design (see section 2 + BGP baseline comparison)
      scripts/configure_bgp.sh (new, separate from bringup.sh)
      collect_bgp_summary validation against real FRR neighbor JSON
      |
      +--> Scenario 1: BGP neighbor removal       (Phase 2C)
      +--> Scenario 2: BGP ASN mismatch           (Phase 2D)
      +--> Scenario 3: bgpd container restart     (Phase 2E)
      |
      Possibly:
      +--> Scenario 4: route missing
           (depends on BGP if BGP-driven, or on static-route mechanism
            if not; see section 6)

    INDEPENDENT (no BGP dependency, but its own blocker)
      Scenario 5: counter / log degradation
        blocked on: can SONiC VS produce non-zero per-port counter
        changes in a single-container setup? Phase 1 evidence says no.

**Implication:** building any BGP scenario before the BGP baseline is
working and `collect_bgp_summary` is validated against real neighbor
JSON would mean debugging fault scripts against an unknown collector
contract. Phase 2C is gated on Phase 2B.


## BGP baseline comparison

Four of the five scenarios require some BGP configuration on
`sonic-vs-troubleshoot`. Phase 2 scope says single SONiC VS
container. Three viable baseline designs, none ideal:

**Option A — neighbor to a non-existent peer IP.** Configure a BGP
neighbor with a remote AS, pointing at an IP that no one is
listening on. FRR will keep the session in Active or Connect state,
retrying TCP connections that never succeed.
- Single container: yes.
- Exercises `collect_bgp_summary` on a real (non-`{}`) JSON shape:
  yes, but only the never-Established branch.
- Supports the planned scenarios:
  - Scenario 1 (neighbor removal): observable but weak — removing a
    neighbor that was never Established produces a less interesting
    diagnosis than removing a working one.
  - Scenario 2 (ASN mismatch): not meaningfully observable — ASN
    mismatch is detected during OPEN message exchange, which
    requires the peer to actually respond.
  - Scenario 3 (bgpd restart): observable. The restart is about
    process state, not session state, so the peer's existence is
    incidental.
  - Scenario 4 (route missing): not observable via BGP without a
    peer that advertises routes.

**Option B — self-neighbor / loopback peering.** Configure FRR to
peer with its own loopback interface (or a local non-loopback IP).
- Single container: yes.
- Whether FRR cleanly supports self-peering on SONiC VS is an open
  question. Self-peering in upstream FRR is allowed in some
  configurations but its state-machine behavior is not always
  realistic.
- Supports the planned scenarios:
  - Scenario 1 (neighbor removal): if the session establishes, this
    is observable like a real removal.
  - Scenario 2 (ASN mismatch): if the local-and-remote ends both
    speak BGP, ASN mismatch can be exercised by editing one side's
    configured remote AS — assuming FRR's self-peering loop produces
    distinct local and remote views.
  - Scenario 3 (bgpd restart): observable.
  - Scenario 4 (route missing): potentially observable via
    redistribute connected or static route advertisements.

  *Open question:* does FRR on SONiC VS actually establish a
  self-neighbor session and emit realistic neighbor JSON? Verifying
  this is part of Phase 2B prerequisite work, not Phase 2 planning.
  If self-peering does not work or produces degenerate JSON, Option
  B collapses to Option A in practice.

**Option C — second lightweight FRR container as a peer.** Run a
second container (FRR alone, or a small Linux + FRR image) that
peers with `sonic-vs-troubleshoot` over a docker network.
- Single container: NO. This violates the Phase 2 scope statement.
- Realism: highest. Real eBGP session, real Established state, real
  ASN mismatch behavior, real route advertisement / withdrawal.
- Cost: container management, network setup, peer-side
  configuration, two-container bringup, extra teardown logic. Pushes
  the project from "single-switch lab" toward "two-node lab", which
  is a Phase 3+ scope item per the top-level README's planned-work
  section.

**This is a scope decision for human review, not an automatic
change.** Option A is safe but produces weak BGP scenarios that may
not exercise the diagnosis loop meaningfully. Option B is the most
promising single-container design but rests on an unverified
assumption about FRR self-peering. Option C produces the best
evidence but breaks a Phase 2 scope statement.

Recommendation: start Phase 2B with a short, time-boxed spike
comparing Option A and Option B using direct vtysh output only.
Limit this to one session. If Option B does not establish cleanly
and produce realistic neighbor JSON quickly, do not keep debugging
it. Either accept Option A as a weak single-container baseline for
bgpd restart + neighbor-config presence scenarios, or make an
explicit scope decision to use Option C.


## 2. Prerequisites before any scenario work (Phase 2B)

Three things must exist before any of the five fault scripts can be
implemented.

**BGP lab topology design.** A specific decision on Option A / B / C
from the comparison above, with the chosen design documented enough
that the configure script and the scenarios can refer to it (AS
numbers, neighbor IPs, redistribution, anything that varies between
scenarios).

**`scripts/configure_bgp.sh`.** A new script, separate from
`bringup.sh`. The current bringup script intentionally only brings
the SONiC runtime to operational state (redis, swss, syncd, FRR
core, bgpd RUNNING but no BGP instance configured). Configuring BGP
neighbors is scenario / test setup, not base container boot. Mixing
the two would make `bringup.sh` carry assumptions about which
scenarios are being run. The configure script should be idempotent
(re-running puts the BGP config into the known state, regardless of
previous state), and it should be runnable standalone for inspection
and for the scenario implementations that call it.

**Validation of `collect_bgp_summary` against real FRR JSON.** The
collector currently handles the `{}` baseline correctly and includes
code paths for `ipv4Unicast.peers` parsing, but those code paths
have not been exercised against actual FRR output. Phase 2B must run
the chosen baseline, capture real `vtysh show bgp summary json`
output, and verify that the collector returns the expected shape for
the actual states FRR produces (Idle, Active, Connect, OpenSent,
OpenConfirm, Established, possibly others). Discrepancies between
what the collector expects and what FRR emits get fixed before any
BGP fault script is written.

These three pieces are Phase 2B. Phases 2C onward are gated on them.


## 3. Scenario order with explicit justification

**Phase 2C — BGP neighbor removal.** First BGP fault. Forces a full
shakedown: the configure script must work, the collector parser must
handle a real session's JSON, the fault script must edit CONFIG_DB
`BGP_NEIGHBOR` in a way that propagates through `bgpcfgd` to FRR,
and the diagnosis agent must produce a useful narrative on a real
neighbor disappearing. Picking neighbor-removal first (rather than
ASN mismatch) is deliberate: removal is the simplest CONFIG_DB
mutation and the resulting state change (neighbor present →
neighbor absent) is unambiguous.

**Phase 2D — BGP ASN mismatch.** Second BGP fault. Reuses the
configure script, the collector parser, and the
CONFIG_DB-mutation pattern from 2C. The fault is editing the
neighbor's remote AS to a value the peer does not advertise. Risk:
this only produces observable evidence if the baseline establishes a
real session in the first place (Option A in the baseline comparison
does not). 2D is conditional on 2C producing a working Established
session.

**Phase 2E — `bgpd` container restart.** Third BGP fault. Different
mechanism: `docker exec supervisorctl stop bgpd` and `start bgpd`
(or `restart`). Does not depend on a neighbor being Established —
the fault is "bgpd process disappeared", which would be observable
via `supervisorctl status` and via vtysh failing, but neither is
produced by any current collector. 2E therefore has a new
prerequisite of its own (see section 6's scenario 3 entry for the
required evidence source). Ordering 2E after 2C / 2D keeps the BGP
work contiguous and lets the collector additions accumulate
naturally. Also: `bgpd` restart timing (5-10 seconds for re-init)
interacts with the rest of the runner's polling, which is worth
designing last in the BGP group.

**Phase 2F (may cut).** Decide between scenario 4 (route missing)
and scenario 5 (counter degradation) based on what SONiC VS can
actually show. Both are at the end of the list because both have
plausible "the virtual switch cannot produce useful evidence"
failure modes.

- Scenario 4 (route missing) is observable if the baseline can
  generate routes (Option B or C with redistribution or static
  routes) and the fault mechanism is "withdraw the route" or
  "delete the static route". If the baseline cannot generate
  routes meaningfully, scenario 4 is cut or downgraded to
  "static-route deletion that does not exercise BGP".

- Scenario 5 (counter degradation) is observable only if SONiC
  VS can produce non-zero per-port counters. Phase 1 confirmed
  the COUNTERS_DB per-port hash is empty without ASIC traffic.
  Without a traffic source inside the container (which would
  require adding a tool like `iperf` or a Python packet
  generator), this scenario has no observable evidence and
  should be cut from Phase 2. If cut, document the cut in
  phase2/README.md when Phase 2 wraps up.


## 4. Collector validation needs

For each existing collector, what new state Phase 2 will exercise
and what risks that introduces.

**`collect_bgp_summary`.** Phase 1 only exercised `{}`. Phase 2 will
exercise at minimum:

- A neighbor in a non-Established state (Idle / Active / Connect) —
  whichever the baseline produces before scenario injection.
- A neighbor that was previously present and is now absent (after
  Phase 2C inject).
- A neighbor in OpenSent / OpenConfirm or in error states after
  ASN mismatch (Phase 2D), if the baseline establishes real sessions.
- The "no BGP instance" state after `bgpd` restart (Phase 2E), which
  may briefly be observable while bgpd is restarting and the BGP
  configuration is being re-read.

  *Open question:* the FRR JSON shape for these states. Phase 2B
  prerequisite work captures and verifies the actual shapes; any
  parser adjustment goes in alongside the prerequisite work, not
  during Phase 2C-2F scenario implementation.

**`collect_interface_counters`.** Phase 1 confirmed all six fields
are zero on SONiC VS because `flex_counter` does not populate
per-port hashes without ASIC traffic. Phase 2 will not change this
on its own. Scenario 5 (counter degradation) is the only one that
needs non-zero counters; that scenario is a may-cut for exactly
this reason.

  *Open question:* is there a way to generate traffic in a
  single-container setup that actually moves these counters? On real
  SONiC, an external traffic source moves the ASIC; in SONiC VS, the
  ASIC is mocked. Investigation belongs in Phase 2B, not in scenario
  planning.

**`collect_interface_state`.** Phase 1 fully exercised this for
admin-down. No new states expected in Phase 2. The collector is
considered stable.

**`collect_recent_logs`.** Phase 1 exercised the baseline-noise case
and the synthetic oper-error cascade case (filtered at the runner
layer). Phase 2 will produce new log content:

- BGP session establishment / teardown messages from FRR.
- `bgpd` start / stop messages from supervisor and from FRR itself.
- Possibly route insertion / withdrawal messages.

  *Open question:* what runner-layer log hygiene each new scenario
  needs. Phase 1 established the pattern (per-scenario filter
  function applied between AFTER-snapshot and blackboard population).
  Each new scenario will need its own filter, written and tested
  during that scenario's implementation. The PLAN does not predict
  the filter contents; that surfaces only after running the
  scenario and looking at the actual log output.


## 5. Runner generalization

`main.py` currently hardcodes the interface-admin-down scenario:
the `INTERFACE` constant, the `USER_COMPLAINT` string, the calls to
`fault_inject` and `fault_restore`, and the
`_filter_logs_for_interface` evidence-hygiene step. Phase 2 needs to
dispatch by scenario name.

**Proposed CLI shape:**

    python3 main.py --scenario interface_admin_down
    python3 main.py --scenario bgp_neighbor_removal
    python3 main.py --scenario bgp_asn_mismatch
    python3 main.py --scenario bgpd_restart
    python3 main.py --list-scenarios
    python3 main.py --scenario <name> --dry-run
    python3 main.py --scenario <name> --keep-fault

**Structure (described, not coded):** a scenarios registry — a
dict or equivalent module-level structure mapping scenario name to
a small dataclass-like record holding everything the runner needs:

- `inject` callable (no args, returns None, raises on failure)
- `restore` callable (no args, returns None, raises on failure)
- `user_complaint` string used when populating the Blackboard
- `evidence_filters` mapping of evidence key → optional callable that
  transforms that key's dict before it reaches the blackboard. The
  Phase 1 `_filter_logs_for_interface` is the prototype: it becomes
  `evidence_filters["recent_logs"]` for the admin-down scenario, and
  scenarios that need no filtering simply omit the key.
- `before_inject_delay_seconds` and `after_inject_delay_seconds` if
  scenario timing differs (admin-down currently uses 1.0s for APP_DB
  propagation; bgpd-restart will likely need longer).

The scenarios registry lives in the runner module or in a sibling
module (`scenarios/registry.py`); the per-scenario fault scripts
themselves stay in `faults/<name>.py` so each scenario remains
runnable standalone via its existing `__main__` block, the way
`faults/interface_admin_down.py` is today.

**Where scenario-specific evidence hygiene lives.** Inside the
scenarios registry entry, as a callable. The runner has a single
generic loop: take AFTER snapshot → apply each evidence_filter to
its matching evidence key → populate blackboard → call diagnosis
agent. No scenario-specific branching in the runner body.

**Unknown scenario name.** Exit code 5 with a message listing the
valid scenarios. Should not silently default to anything.

**Backward compatibility.** *Open question for human decision.* Two
options:

- `python3 main.py` with no flag continues to run
  `interface_admin_down` (least disruptive; matches existing
  documentation, existing tests, existing demo output).
- `python3 main.py` with no flag prints the scenarios list and exits
  non-zero (more honest about Phase 2 being multi-scenario; forces
  callers to be explicit).

Recommendation: the second option. It eliminates a hidden default
that would silently mask scenario-name typos. The phase1/README.md
and top-level README will need a one-line update to add
`--scenario interface_admin_down` to the example commands, but that
is small.

**Failure modes the runner should make explicit.** Unknown scenario
name → exit 5. Scenario registry entry missing a required field →
exit 6. Scenario's inject raises → exit code currently used for
inject failure path (no change). Beyond that, the runner's
exit-code structure from Phase 1 stays as-is.


## 6. Honest risks and what could go wrong

One specific risk per scenario.

**Scenario 1 (BGP neighbor removal).** If the chosen baseline is
Option A (peer to a non-existent IP), the neighbor never reaches
Established, so the "fault" of removing it is observably similar to
the baseline. The diagnosis becomes "BGP had a configured neighbor
in Active state, now there is no configured neighbor", which is a
much weaker scenario than "an Established session disappeared". The
useful version of this scenario depends on a baseline that
establishes a real session.

**Scenario 2 (BGP ASN mismatch).** ASN mismatch is detected during
the OPEN message exchange. With Option A (no real peer), no OPEN is
exchanged, so the mismatch is never detected — the session stays in
Active/Connect, exactly as in the baseline. With Option B
(self-peering), an open question is whether the local FRR state
machine actually compares configured-remote-AS against the AS it
sees coming back from itself; if it short-circuits self-peering,
the scenario produces no observable error. With Option C (second
container), the scenario works as expected.

**Scenario 3 (`bgpd` restart).** Two distinct issues to resolve.

First, an evidence-source prerequisite: Phase 2E needs an explicit
evidence source for bgpd process state. The four current collectors
do not look at supervisor. Options:
- add a small `collect_service_status("bgpd")` collector, or
- have the bgpd_restart fault script return pre/post
  `supervisorctl status` as evidence, which would be a new pattern.
Recommendation: add a collector, because service state is evidence,
not fault-script output.

Second, a timing-design issue independent of the evidence source.
After `supervisorctl restart bgpd`, the agent could observe either
the mid-restart state (bgpd absent or initializing) or the
post-restart state (bgpd RUNNING again, possibly with zero neighbor
state until the configure-load completes). The runner needs to
choose which state to diagnose. If it captures the mid-restart
state, the diagnosis is "bgpd is not running"; if it captures the
post-restart state, the diagnosis is "bgpd is running but no BGP
instance is present", which is misleading. This is a timing-design
question, not a fault-script-correctness question.

**Scenario 4 (route missing).** Two risks. First, if BGP-driven, the
risk is the same as scenarios 1 and 2 — depends on the baseline
establishing real sessions. Second, "route missing" on SONiC VS may
not actually be observable through the existing collectors:
`collect_bgp_summary` does not look at the RIB or FIB. Adding a
`collect_routes` collector to inspect `vtysh show ip route` output
would be a Phase 2 collector addition, which expands scope. The
honest version of this scenario may require either dropping to
static routes (and writing a new collector) or cutting the scenario.

**Scenario 5 (counter / log degradation).** SONiC VS does not
populate per-port counters without ASIC traffic. Phase 1 confirmed
this. Without a traffic generator inside the container, this
scenario produces zero observable counter changes. Adding a traffic
generator (iperf, scapy-based, or similar) is a Phase 2 scope
expansion that the project has not committed to. Recommendation:
cut this scenario from Phase 2 and document the cut in the eventual
phase2 wrap-up README. If a Phase 3+ scenario needs realistic
counter behavior, the right answer there is a traffic generator or
moving to a multi-container topology, not a Phase 2 workaround.

**May-cut summary.**

- Scenario 5: strong cut candidate. Probably cut at Phase 2B if no
  practical traffic-generation path exists.
- Scenario 4: cut OR downgrade candidate. Decide during Phase 2B
  once the baseline approach is chosen.
- Scenarios 1, 2: viability depends on baseline option chosen in
  Phase 2B. If Option A is the only feasible single-container
  baseline, both scenarios produce weak evidence; surface that to
  the user before writing them.


## 7. Effort estimate

Calibration: the Phase 1 `interface_admin_down` deliverable is
roughly:
- `faults/interface_admin_down.py` — 200 lines including docstrings
  and the standalone `__main__`.
- `main.py` integration — about 30 lines of integration plus the
  scenario-specific log filter.
- Collector additions — none (Phase 1 used the existing collectors
  as-is).

Treating that as one unit, the estimate per Phase 2 scenario:

- **Phase 2B prerequisite work** (BGP baseline + configure script +
  collector validation) — *larger than one Phase 1 scenario unit.*
  This is foundational and benefits all the BGP scenarios; it should
  be tracked as its own milestone with its own commit, not folded
  into the first scenario.
- **Scenario 1, BGP neighbor removal** — similar effort to admin-down
  for the fault script and runner integration, PLUS any parser
  adjustments that fall out of 2B validation. Call it 1.2x.
- **Scenario 2, BGP ASN mismatch** — similar effort to scenario 1.
  The collector and baseline are already exercised; this is largely
  a copy-and-adapt of the neighbor-removal pattern with a different
  CONFIG_DB field. Call it 0.7x.
- **Scenario 3, `bgpd` restart** — different mechanism, modest in
  fault-script code volume, but it requires the new evidence source
  flagged in section 6 (a `collect_service_status` collector is the
  recommended path). That collector addition plus the timing-design
  question pushes the estimate above admin-down. Call it 1.1x.
- **Scenario 4, route missing** — *uncertain.* If it requires a new
  `collect_routes` collector or a static-route mechanism, it is
  1.5x. If it cannot work meaningfully with the chosen baseline, it
  is zero (cut).
- **Scenario 5, counter degradation** — *may cut entirely.* If kept,
  it requires a traffic generator, which is a Phase 2 scope
  expansion not yet committed to. Treat as zero effort for planning
  purposes; revisit only if Phase 2B finds a way to produce non-zero
  counters that does not break single-container scope.
- **Runner generalization (section 5)** — separate from any scenario.
  About 0.5x: registry data structure, CLI parsing, the generic
  evidence-hygiene loop, the backward-compatibility decision once
  the human picks an answer.

Rough total Phase 2 effort if all five scenarios survive: roughly
5.3 units. If scenarios 4 and 5 are cut, roughly 3.8 units, of
which the prerequisite work and runner generalization are
foundational and benefit any future phase.


## Open questions for human decision before Phase 2B starts

These are not blockers for the plan itself, but they are blockers for
Phase 2B implementation. Listed in the order they need answers.

1. Which BGP baseline design? (Option A non-existent peer, Option B
   self-peering, Option C second FRR container.) Recommendation:
   one-session time-boxed spike comparing A and B via direct vtysh
   output. Do not debug B beyond that session. If B does not produce
   realistic neighbor JSON quickly, either accept A's weaker
   scenarios or make an explicit scope decision to use C.
2. Does `python3 main.py` with no `--scenario` flag default to
   `interface_admin_down`, or print the scenarios list and exit
   non-zero? Recommendation: the latter (explicit > silent default).
3. Cut decisions for scenarios 4 and 5. Recommendation: cut
   scenario 5 unless Phase 2B finds a way to produce non-zero
   counters without expanding scope; defer scenario 4's cut
   decision until the BGP baseline is chosen.
4. If Option B self-peering fails or produces degenerate JSON, do
   we drop to Option A (single-container, weak scenarios) or
   expand to Option C (second container, Phase 2 scope expansion)?
   This is a scope decision the plan deliberately does not
   pre-empt.
