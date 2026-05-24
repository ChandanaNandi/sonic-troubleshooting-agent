# Autonomous Network Troubleshooting Agent for SONiC

A local SONiC troubleshooting agent that injects a reversible fault on a SONiC virtual switch, reads live state from CONFIG_DB / APP_DB / COUNTERS_DB / `vtysh` / syslog, posts evidence to a blackboard-style shared workspace, runs four specialist LLM agents in fan-out, and synthesizes a diagnosis. Runs entirely locally with Docker, SONiC VS, FRR, Ollama, and `qwen2.5:7b-instruct`. No cloud APIs.


## What it does

- Runs three reversible troubleshooting scenarios end-to-end (inject → collect → diagnose → restore):
  - `interface_admin_down` — admin-shutdown of `Ethernet4`
  - `bgp_neighbor_removal` — BGP neighbor removed via `vtysh`
  - `bgp_asn_mismatch` — wrong remote-as on the BGP neighbor
- Collects structured evidence from CONFIG_DB, APP_DB, COUNTERS_DB, `vtysh`, and `/var/log/syslog` inside the switch container
- Runs four specialist agents in fan-out (`triage`, `interface`, `bgp`, `logs`), each scoped to a single evidence slice
- Synthesizes evidence plus specialist hypotheses with a diagnosis agent (fan-in) and emits a single JSON diagnosis to stdout
- Brings up and tears down a two-container BGP lab fixture automatically for the BGP scenarios
- Restores injected faults after each run (lab cleanup, not autonomous remediation)


## Demo

```
./scripts/bringup.sh
python3 main.py --scenario interface_admin_down
python3 main.py --scenario bgp_neighbor_removal
python3 main.py --scenario bgp_asn_mismatch
```

`stdout` is the diagnosis JSON only; section headers, snapshots, and inject/restore progress go to `stderr`, so the diagnosis pipes cleanly:

```
python3 main.py --scenario bgp_neighbor_removal | jq -r .diagnosis
```

Excerpt from a real run of `bgp_neighbor_removal` (abbreviated):

```
=== INJECT (bgp_neighbor_removal) ===
  before: peer 10.10.10.2 state=established
  after:  peer 10.10.10.2 state=removed
=== SPECIALISTS (fan-out) ===
  interface / triage / logs / bgp: posted hypotheses
=== FAN-IN: DIAGNOSIS ===
{
  "diagnosis": "Based on the evidence, there is no active BGP session,
   as indicated by the absence of any neighbors in the BGP summary
   (claim [bgp], confidence 0.8) ...",
  ...
}
=== RESTORE / BGP LAB DOWN (test cleanup, not remediation) ===
```

Also: `--dry-run` lists the planned steps without mutating or calling Ollama; `--keep-fault` skips restore so the injected state can be inspected manually.


## Architecture

A blackboard-inspired shared workspace with fixed fan-out / fan-in over a local 7B model.

```mermaid
flowchart TD
    User[User complaint<br/>plain text]
    Runner[main.py<br/>runner with --scenario dispatch]
    Fault[fault inject / restore]
    Collectors[4 collectors<br/>CONFIG_DB / APP_DB /<br/>COUNTERS_DB / vtysh / syslog]
    BGPLab[configure_bgp.sh<br/>two-container BGP lab fixture]
    SONiC[SONiC VS + BGP peer<br/>Docker]
    BB[blackboard-style shared workspace<br/>evidence + hypotheses]
    Diag[diagnosis agent<br/>fan-in synthesis]
    Out[JSON diagnosis<br/>stdout]

    subgraph Specialists [fan-out: 4 specialists, qwen2.5:7b-instruct, concurrent]
        Triage[triage]
        Iface[interface]
        Bgp[bgp]
        Logs[logs]
    end

    User -->|natural language| Runner
    Runner --> Fault
    Runner --> Collectors
    Runner --> BGPLab
    Fault --> SONiC
    Collectors --> SONiC
    BGPLab --> SONiC
    SONiC -->|evidence| BB
    BB -->|fan-out| Specialists
    Specialists -->|hypotheses| BB
    BB -->|fan-in: evidence + hypotheses| Diag
    Diag -->|JSON| Out
```

Linear form for non-Mermaid viewers: user complaint → `main.py` runner → fault inject + collectors (plus `configure_bgp.sh` for BGP scenarios) → SONiC VS + BGP peer (Docker) → blackboard-style shared workspace → fan-out to four specialist agents → fan-in to the diagnosis agent → JSON diagnosis on stdout.

All five agents share one `qwen2.5:7b-instruct` instance via Ollama; specialization comes from prompt constraints and each specialist's evidence slice, not from model capability. Each specialist prefixes its claim with a source tag (`[triage]`, `[interface]`, `[bgp]`, `[logs]`) for attribution at synthesis. Fan-out uses `ThreadPoolExecutor(max_workers=4)`. This is not a full opportunistic blackboard scheduler; the specialist set is fixed per run.


## Repository map

```
main.py                           end-to-end runner with --scenario dispatch
scripts/bringup.sh                brings SONiC services up
scripts/configure_bgp.sh          two-container BGP lab fixture (up / down / status)
faults/                           reversible fault scripts (one per scenario)
collectors/sonic_state.py         four evidence collectors
blackboard/blackboard.py          shared workspace with deep-copy isolation
agents/                           triage, interface, bgp, logs specialists +
                                  diagnosis synthesis agent
phase1/, phase2/, phase3/         design, spike, decision, and findings docs
```


## Requirements

- Docker Desktop on macOS, Apple Silicon (M4 Pro reference: ≥12 CPUs and ≥7.5 GB RAM allocated)
- The `docker-sonic-vs-fixed:latest` SONiC VS image built locally — see the companion [`sonic-intent-agent`](https://github.com/ChandanaNandi/sonic-intent-agent) repository, which contains the SONiC VS build infrastructure
- Python 3.11+ (stdlib only; no `requirements.txt`)
- [Ollama](https://ollama.com) on `localhost:11434` with `qwen2.5:7b-instruct` pulled (`ollama pull qwen2.5:7b-instruct`)


## Scenarios

| Scenario | Fault injected | Mutation path | BGP lab fixture |
|---|---|---|---|
| `interface_admin_down` | `Ethernet4` admin-shutdown | CONFIG_DB via `config interface shutdown` | no |
| `bgp_neighbor_removal` | Removes BGP neighbor `10.10.10.2` | `vtysh` | yes |
| `bgp_asn_mismatch` | Sets `remote-as` to wrong AS (`65002`) | `vtysh` | yes |

All three are reversible; the runner restores after the diagnosis step.


## Limitations

- Scenario-bound: the runner dispatches `--scenario <one-of-three>`, not arbitrary natural-language complaints.
- No evaluation harness yet (no detection / localization / root-cause-analysis scoring).
- BGP scenarios mutate via `vtysh`; the CONFIG_DB + `bgpcfgd` path was deferred — see [`phase2/2C_CONTROL_PLANE_DECISION.md`](phase2/2C_CONTROL_PLANE_DECISION.md).
- Not a full opportunistic blackboard scheduler; the specialist set is fixed per run.
- The runner filters SONiC VS's synthetic oper-error syslog cascade for the admin-down scenario; other scenarios may need their own per-scenario log hygiene.
- Not production-ready: no authentication, no audit logging beyond the in-memory blackboard, no multi-operator coordination.


## Engineering notes

Design, spike, and decision documents:

- [`phase1/README.md`](phase1/README.md) — single-scenario end-to-end design
- [`phase2/2C_CONTROL_PLANE_DECISION.md`](phase2/2C_CONTROL_PLANE_DECISION.md) — choosing `vtysh` over `bgpcfgd` for BGP mutation
- [`phase2/2D_ASN_MISMATCH_SPIKE_FINDINGS.md`](phase2/2D_ASN_MISMATCH_SPIKE_FINDINGS.md) — ASN-mismatch evidence-shape spike
- [`phase2/2D_ASN_MISMATCH_RESTORE_FINDINGS.md`](phase2/2D_ASN_MISMATCH_RESTORE_FINDINGS.md) — comparing restore methods under deep BGP backoff
- [`phase3/README.md`](phase3/README.md) — multi-agent fan-out / fan-in design, concurrency, attribution scheme


## Related work

Architectural reference points. Where details beyond an arxiv ID, a short description, and (where known) author and affiliation are not stated here, they were not verified.

- arxiv 2507.01701 — blackboard architecture for LLM multi-agent systems (Han, Zhang, July 2025). <https://arxiv.org/abs/2507.01701>
- arxiv 2509.20600 — LLM agent framework compiling YANG to SONiC (Lin, Zhou, Yu — Meta / Stony Brook / Harvard, September 2025). <https://arxiv.org/abs/2509.20600>
- arxiv 2512.16381 — NIKA benchmark for LLM agents on network troubleshooting using Kathara (December 2025). <https://arxiv.org/abs/2512.16381>
- Aviz Network Copilot — commercial reference using a fine-tuned Llama 70B on SONiC. <https://aviznetworks.com>
- Cisco AgenticOps — autonomous troubleshooting product announced February 2026. <https://newsroom.cisco.com/c/r/newsroom/en/us/a/y2026/m02/cisco-expands-agenticops-innovations-across-portfolio.html>


## Companion project

[`sonic-intent-agent`](https://github.com/ChandanaNandi/sonic-intent-agent) — intent-based SONiC configuration with formal verification. The first project in this two-project portfolio.


## License

MIT License. See [`LICENSE`](LICENSE).


## Author

Chandana Nandi. <https://github.com/ChandanaNandi>
