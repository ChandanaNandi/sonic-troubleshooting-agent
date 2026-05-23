# Phase 2B Decision: Use a second FRR peer container for BGP scenarios

## Decision

Phase 2 will expand scope from single-container-only to a two-container
BGP lab for BGP scenarios. The existing `sonic-vs-troubleshoot`
container remains the SONiC system under test. A second lightweight
FRR peer container will be added only to provide real BGP neighbor
behavior. Non-BGP scenarios stay single-container.

This decision supersedes parts of `phase2/PLAN.md`, which was written
before the Phase 2B spike resolved the baseline question. See "What
changes in phase2/PLAN.md" below for the supersession map.


## Evidence from the spike

See `phase2/2B_SPIKE_FINDINGS.md` for the full captured output. In
summary:

- **Option A (neighbor to `192.0.2.1`).** Configured cleanly,
  produced parser-compatible `show bgp summary json`, neighbor
  reached state `Active`. But `messageStats.opensSent = 0` and
  `messageStats.opensRecv = 0` — no OPEN message was ever exchanged
  because no peer responded. Without OPEN exchange, ASN negotiation
  cannot happen and route advertisement cannot occur. The diagnostic
  signal for the planned BGP scenarios was therefore not present.

- **Option B (self-peering via loopback).** Failed at configuration
  time with `% Can not configure the local system as neighbor`. Hard
  rejection from FRR. No state-machine attempt. Per the spike's stop
  conditions, no variations were tried.

Conclusion of the spike: Option A is viable only as a weak baseline
(supports `bgpd_restart` cleanly; supports `bgp_neighbor_removal`
degraded; does not meaningfully support `bgp_asn_mismatch` or the
BGP-driven path of `route_missing`). Option B is dead.


## Why Option C is worth the added scope

Five reasons specific to the planned Phase 2 scenarios.

- **Real Established BGP session as the baseline.** Scenarios 1-4
  all benefit from starting against a session that has reached
  `Established`, not `Active`. A real peer produces real session
  state transitions; without one, the scenarios reduce to
  configuration-presence checks.

- **`bgp_neighbor_removal` becomes a real state transition.**
  Removing a neighbor that was actually exchanging KEEPALIVE
  messages should produce a clean, observable state transition in
  `show bgp summary json` and neighbor-detail JSON. Whether syslog
  carries useful BGP transition lines is something Phase 2B
  implementation must verify.

- **`bgp_asn_mismatch` becomes meaningful.** ASN mismatch is
  detected during the OPEN message exchange. With no peer, no OPEN
  is sent; with a real peer, the mismatch can produce
  OPEN/NOTIFICATION evidence, such as Bad Peer AS, if the
  two-container setup reaches the point of exchanging BGP OPEN
  messages. Phase 2B implementation must capture the actual FRR
  JSON/log shape before the scenario is written.

- **`route_missing` becomes possible.** A real peer can advertise a
  prefix, the SUT learns it, and the fault can be the peer
  withdrawing it. Without a peer, prefix advertisement does not
  happen at all and the scenario reduces to static-route deletion,
  which does not exercise BGP.

- **Collector validation becomes stronger.** `collect_bgp_summary`
  was designed to handle `ipv4Unicast.peers[*]` with `remoteAs` and
  `state` fields, but it has only ever been exercised on `{}` and
  on a single `Active`-state peer. A real Established session
  exercises the realistic shape the collector will see in
  production-style scenarios.

`bgpd_restart` (scenario 3) does not strictly need a peer to be
useful, but running it within the two-container setup keeps the BGP
scenarios contiguous and lets the restart be diagnosed against a
real session being interrupted — a richer signal than restarting
bgpd in isolation.


## What changes in phase2/PLAN.md

The PLAN was correct planning written before the spike. This
decision resolves some of its open questions and shifts some of its
scope assumptions. PLAN.md itself is not rewritten; this decision
document supersedes it on the points below.

**Resolved by this decision:**

- The "BGP baseline comparison" three-way comparison
  (A / B / C). Option B is rejected (FRR config-time refusal).
  Option A is rejected as a primary baseline. Option C is chosen.
- PLAN.md open question 1 ("Which BGP baseline design?") — answered:
  Option C.
- PLAN.md open question 4 ("If Option B self-peering fails or
  produces degenerate JSON, do we drop to Option A or expand to
  Option C?") — answered: expand to Option C.

**Shifted by this decision:**

- Phase 2 scope is no longer "single SONiC VS container". It is
  "single SONiC VS container as system under test, plus a second
  FRR container as a BGP peer test fixture". The top-level README's
  honest-scope statement will need a follow-up update to reflect
  this; that update is not made by this document and is deferred to
  Phase 2 wrap-up.
- Effort estimates in PLAN.md section 7 will increase. The
  prerequisite work (Phase 2B) now includes peer-container topology
  design, image choice, network setup, and lifecycle management —
  none of which were in the original PLAN. Re-estimating is a Phase
  2B implementation concern; the original PLAN numbers should be
  treated as lower bounds.
- Scenario 4 (`route_missing`) changes from "may cut" to "possible
  under Option C". With a real peer that can advertise and withdraw
  prefixes, the BGP-driven version of the scenario becomes
  implementable. Whether to keep it in Phase 2 or push to a later
  phase remains an implementation prioritization decision.

**Unchanged by this decision:**

- Scenario 5 (`counter_degradation`) is still cut by default. The
  second container is a BGP peer, not a traffic generator; per-port
  counters are still unobservable on SONiC VS without ASIC traffic.
  Re-considering scenario 5 would require a separately justified
  scope change.
- PLAN.md open question 2 ("Does `python3 main.py` with no
  `--scenario` flag default to `interface_admin_down`, or print the
  scenarios list and exit non-zero?") remains open. The recommended
  answer (explicit `--scenario` required) is the working assumption
  for Phase 2C runner generalization.
- The blackboard, the diagnosis agent, and `scripts/bringup.sh`
  remain unchanged in Phase 2.
- No new agents. No evaluation harness. No multi-switch SONiC.

**Still open after this decision (the spike's own open questions):**

- Configuration mechanism for `scripts/configure_bgp.sh` on the
  SONiC side — vtysh (used in the spike) vs CONFIG_DB
  `BGP_NEIGHBOR` + `bgpcfgd` (the SONiC-canonical path). The spike
  did not validate the latter. Phase 2B implementation must either
  use the SONiC mechanism or explicitly justify vtysh for test
  setup.


## Scope guardrails

The following constraints define what this decision does and does
NOT change.

- This is NOT multi-switch SONiC. There is one SONiC system under
  test (`sonic-vs-troubleshoot`).
- The second container is a BGP peer test fixture, nothing more. It
  does not run SONiC services. It does not have collectors. It does
  not have a blackboard.
- The blackboard MUST only hold evidence collected from
  `sonic-vs-troubleshoot`. The peer container exists to create real
  BGP state on the SUT side; its own internal state is not evidence
  for the diagnosis agent. A future phase may revise this if
  multi-system diagnosis becomes scope, but it is out of scope here.
- The diagnosis agent's narrator role is unchanged. It reads SUT
  evidence and explains it. It does not get peer-container output.
- The Blackboard class is unchanged.
- `scripts/bringup.sh` is unchanged. It continues to bring
  `sonic-vs-troubleshoot` into operational state and nothing more.
  Peer-container lifecycle is a separate script's responsibility.
- Runner scenario dispatch (`--scenario` flag, scenarios registry)
  remains the planned shape from PLAN.md section 5.
- Remediation is out of scope. The agent diagnoses; it does not
  act. Restore steps in the runner remain lab cleanup.


## What we are NOT deciding tonight

These are real implementation decisions deliberately deferred to
Phase 2B implementation work, not papered over.

- **FRR image choice.** Whether to use the `frrouting/frr` official
  image, build a small custom image, or extract an FRR-only layer
  from another source.
- **Docker network configuration.** Whether the peer joins a
  user-defined bridge network with `sonic-vs-troubleshoot`, uses
  host networking, or another arrangement that allows TCP/179
  between the two.
- **AS numbers and IP scheme.** Local AS on the SUT side, remote AS
  on the peer side (must differ if eBGP), specific neighbor IPs.
- **Peer container lifecycle.** Whether the peer runs persistently
  alongside `sonic-vs-troubleshoot` (started once and reused across
  scenario runs), or is started and torn down per scenario.
- **Configure script ownership.** Whether `scripts/configure_bgp.sh`
  manages peer container lifecycle, or a separate
  `scripts/bgp_peer.sh` (or similar) does, with `configure_bgp.sh`
  only handling SUT-side configuration.
- **Route advertisement mechanism.** For scenario 4, whether the
  peer advertises via static `network` statements, redistribute
  connected, or another method.
- **Whether the SONiC side uses vtysh or CONFIG_DB.** Open from the
  spike. Phase 2B implementation must choose and justify.


## Next implementation step

Phase 2B implementation, in this order:

1. Design the two-container BGP topology — image choice, network
   model, AS numbers, IP scheme, who owns peer container lifecycle.
2. Add a setup script (location and naming TBD per the deferred
   decisions above). The script must be idempotent and runnable
   standalone, matching the pattern of `scripts/bringup.sh`.
3. Bring up or configure the FRR peer.
4. Establish one BGP session and verify it reaches `Established` on
   the SUT side.
5. Capture `show bgp summary json` and
   `show bgp neighbors <peer> json` from the SUT for the
   Established-state baseline.
6. Validate whether `collect_bgp_summary` needs changes against
   captured Established-state JSON. Document expected and observed
   shapes side by side. Any required collector change is a separate
   committed step before the first fault script.
7. Only then start fault scripts, beginning with
   `bgp_neighbor_removal` (Phase 2C) per the original PLAN ordering.


## Risks

- **Setup complexity.** Two containers, a Docker network, two
  separate FRR configurations. More moving parts than Phase 1.
- **Teardown discipline.** The peer container must be removable
  cleanly. Leaving it running across sessions wastes resources and
  may interfere with `sonic-vs-troubleshoot` restarts. The Phase 2B
  setup script needs a matching teardown path.
- **Image choice risk.** An off-the-shelf FRR image may be larger
  than needed; a hand-built image is more work. Either path is
  reasonable; the decision belongs in implementation.
- **Architectural drift risk.** The peer container is a fixture,
  not a second SONiC switch. Code, scripts, or docs that start
  treating it as a SONiC switch (or that pipe peer-side state into
  the blackboard) drift away from this decision. Reviewers should
  push back on any such drift in subsequent commits.
- **Diagnosis-agent contamination risk.** If peer-container output
  is accidentally included in the evidence dict passed to
  `produce_diagnosis`, the model may describe the peer as if it
  were the SUT. The runner's evidence-population step must keep
  evidence sourced from `sonic-vs-troubleshoot` only, the same way
  Phase 1 does today.
- **Comparable-prior-art question.** Project 1 was strictly
  single-container. This decision is the first scope expansion in
  the portfolio. The top-level README's honest-scope section will
  need an update at Phase 2 wrap-up to reflect the
  two-container-for-BGP-only reality, so the portfolio framing
  stays accurate.
