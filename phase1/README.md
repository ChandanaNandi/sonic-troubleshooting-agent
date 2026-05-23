# Phase 1: Single-scenario troubleshooting end-to-end

## Project context

This is Phase 1 of a multi-phase project building an autonomous troubleshooting agent for SONiC. The user describes a network problem in plain English; the agent reads structured state from a live SONiC virtual switch (CONFIG_DB, APP_DB, COUNTERS_DB, vtysh, syslog), records what it finds on a shared blackboard, and asks a local 7B model to narrate a diagnosis grounded in that evidence. No remediation changes are applied. Phase 1 does apply and restore one lab fault so the troubleshooting loop has something real to diagnose. The agent diagnoses; it does not act.

Phase 1 builds the minimum end-to-end working version for a single hardcoded scenario: an admin-down fault on `Ethernet4`. Phase 2 and later will add more fault scenarios, broader agent participation, and an evaluation harness.

## What was built

Six files implement the Phase 1 scenario.

`scripts/bringup.sh` brings the `sonic-vs-troubleshoot` container into an operational SONiC state. It starts the container with the image's default supervisord entrypoint, polls until redis answers and `start.sh` enters `EXITED`, verifies CONFIG_DB is populated, and explicitly starts `bgpd` (which the image leaves with `autostart=false`). The script is idempotent: re-running removes any pre-existing container of the same name and creates a fresh one.

`faults/interface_admin_down.py` is the fault injector. The `inject` subcommand runs `config interface shutdown Ethernet4` inside the container and polls CONFIG_DB until `admin_status=down` is observable; `restore` runs the corresponding `startup` and polls until `admin_status=up`. The poll loop has a 2-second deadline at 50 ms intervals to accommodate the 60-80 ms CONFIG_DB read-after-write lag that project 1 first measured. Run standalone with `python3 faults/interface_admin_down.py inject` or `restore`.

`collectors/sonic_state.py` defines four pure-Python read functions, each returning a structured dict. `collect_interface_state` reads `admin_status` from CONFIG_DB `PORT|<name>` and `oper_status` from APP_DB `PORT_TABLE:<name>` (note the different separators). `collect_interface_counters` resolves the interface to an OID via `COUNTERS_PORT_NAME_MAP` and HGETALLs `COUNTERS:<oid>`, mapping six SAI counter fields to `rx_packets`, `tx_packets`, `rx_errors`, `tx_errors`, `rx_discards`, `tx_discards`. `collect_bgp_summary` parses `vtysh show bgp summary json`. `collect_recent_logs` tails `/var/log/syslog` inside the container via a single shell command that returns a sentinel if the file is missing. Collector failures do not raise; they return a dict with an `"error"` key so a caller can include the failure in its evidence trail. A `__main__` block runs all four against `Ethernet4` and prints their outputs as JSON.

`blackboard/blackboard.py` is a plain Python container with one class, `Blackboard`. It holds the user complaint, an evidence dict keyed by collector name, a hypotheses list, and a final diagnosis. Mutation is explicit through `add_evidence`, `add_hypothesis`, and `set_diagnosis`; reads return defensive deep copies so callers cannot reach back into the audit trail through the returned values. `set_diagnosis` is set-once and raises `ValueError` on a second call. The `__main__` smoke test verifies deep-copy isolation on both writes and reads and the set-once enforcement.

`agents/diagnosis.py` is the LLM-facing module. The one public function `produce_diagnosis(blackboard) -> dict` reads the blackboard's contents, serializes the evidence as pretty JSON, and posts a chat request to Ollama at `http://localhost:11434/api/chat` against `qwen2.5:7b-instruct` with `temperature=0.2`. The system prompt constrains the model to a narrator role: it must describe what the evidence shows, must not propose commands or remediation steps, and must say "insufficient evidence" rather than invent facts. The function returns a dict containing the model text verbatim, the model id, a Python-built `evidence_summary` (one-line `"<collector> ok|error"` audit string), and the raw Ollama JSON response. Stdlib `urllib.request` is used rather than the `ollama` Python package to keep Phase 1 free of new dependencies.

`main.py` at the repo root is the end-to-end runner. It verifies the container is running, takes a BEFORE snapshot through all four collectors, calls `fault_inject`, sleeps 1 second so APP_DB `oper_status` has time to propagate from CONFIG_DB, takes an AFTER snapshot, filters the syslog evidence for relevance (see `What was learned` below), populates a Blackboard, calls `produce_diagnosis`, and prints the diagnosis dict as pretty JSON to stdout. Restore runs in a `try/finally` so a partial failure still cleans up the lab fault. Section headers and operational progress go to stderr; the diagnosis JSON is the only thing on stdout.

## Architecture decisions

Three decisions shape everything that follows.

Python owns investigation flow. The blackboard, the order of evidence collection, and the populate-then-diagnose handoff are all deterministic Python. Qwen reads structured facts and explains them; it does not plan investigation steps or pick which collector to run next. The reason is honest: 7B-scale models are too weak for reliable multi-step network troubleshooting. The NIKA benchmark (arxiv 2512.16381) reports GPT-OSS:20B at 19% / 5.5% / 5.5% on detection / localization / root-cause analysis tasks, and `qwen2.5:7b-instruct` is smaller. Treating the model as a narrator over structured evidence, rather than as the brain, is what makes a 7B model useful in this loop.

The blackboard is a deterministic shared-state container, not an emergent agent platform. One Python object. Methods, not message-passing. Defensive deep copies on every write and every read so the audit trail can only be mutated through `add_evidence`, `add_hypothesis`, and `set_diagnosis`. There is no scheduler in Phase 1, no other agent participants, no concurrency. Multi-agent extension is deferred to a later phase; the blackboard is built to support it without already being it. See arxiv 2507.01701 (Han, Zhang, July 2025) for the broader blackboard pattern as applied to LLM multi-agent systems.

Diagnose only, no remediation. The agent does not configure the network, does not propose fixes to the user, and does not loop the LLM back into more investigation. `main.py`'s `restore` step is lab cleanup for the injected fault, framed throughout the code as test cleanup rather than autonomous fix-application. The diagnosis system prompt explicitly forbids the model from recommending commands, checks, or remediation steps "even when the obvious fix would be a single command".

## Prerequisites

Hardware: macOS on Apple Silicon. Development setup used an M4 Pro with 12 CPUs and 7.65 GB allocated to Docker Desktop.

Software: Docker Desktop, Ollama running on `localhost:11434` with `qwen2.5:7b-instruct` pulled, and Python 3.11 or newer. No `requirements.txt` is needed in Phase 1 because every module uses only the standard library; the choice of `urllib.request` in the diagnosis agent (rather than the `ollama` package that project 1 uses) is what makes this possible.

The `docker-sonic-vs-fixed:latest` image must exist locally. The `Dockerfile.sonic-fixed` at the repo root builds it from `docker-sonic-vs:latest`; obtaining the base SONiC VS image is out of scope here.

## How to run

One-time bringup of the SONiC container per session:

    ./scripts/bringup.sh

The script removes any pre-existing `sonic-vs-troubleshoot` container and creates a fresh one. It runs in about 30 seconds and prints a supervisor status summary on completion.

Each individual module has a standalone smoke test:

    python3 faults/interface_admin_down.py inject
    python3 faults/interface_admin_down.py restore
    python3 collectors/sonic_state.py
    python3 blackboard/blackboard.py
    python3 agents/diagnosis.py

The end-to-end runner has three modes:

    python3 main.py
    python3 main.py --dry-run
    python3 main.py --keep-fault

The default run injects the fault, collects evidence, asks the model to diagnose, prints the diagnosis JSON to stdout, and restores. `--dry-run` verifies the container and prints the planned steps without mutating state or calling Ollama. `--keep-fault` injects and diagnoses but skips the restore so the fault can be inspected manually; the runner prints the manual restore command to stderr.

Output structure: section headers, before-and-after snapshots, and inject/restore progress all go to stderr. The diagnosis dict goes to stdout as a single JSON document, which lets the diagnosis be piped to `jq` or redirected to a file cleanly. Exit codes are distinct: `0` on full success, `2` on precondition failure (container down), `3` on diagnosis failure, `4` on restore failure after a successful diagnosis, `1` on any other unexpected failure. Total runtime is roughly 20-30 seconds, most of it Ollama inference.

## What was verified

The inject-collect-diagnose-restore chain runs end-to-end.

Container readiness is gated by three checks before `bringup.sh` exits zero: `redis-cli ping` returns PONG, `supervisorctl status start.sh` reports `EXITED` (which is the right boot-complete signal because `start.sh` is the orchestrator that brings up the SONiC `*mgrd`/`*syncd` services in sequence and then exits), and CONFIG_DB has a populated `PORT|Ethernet4` entry. `bgpd` is then explicitly started.

Fault injection mutates CONFIG_DB and the change is observable. The inject path polls CONFIG_DB after running `config interface shutdown` until `admin_status=down` is read back, accommodating the read-after-write lag. The restore path symmetrically polls for `admin_status=up`. Both paths can be independently verified through direct `docker exec sonic-vs-troubleshoot redis-cli -n 4 HGET PORT|Ethernet4 admin_status`.

All four collectors return structured dicts on both happy and degraded paths. Collector failures surface as an `"error"` key rather than raising, so a downstream caller (the runner, an agent) can include the failure as evidence without try/except scaffolding.

The Blackboard enforces set-once on diagnosis and deep-copy isolation on both writes and reads. The standalone smoke test asserts that mutating an evidence dict after `add_evidence`, and mutating the dict returned by `get_evidence`, both leave the blackboard unaffected.

The diagnosis agent's system prompt forbids the model from suggesting next-step commands, checks, or remediation. Observed runs on the admin-down scenario produce diagnoses that quote evidence field values (for example, `admin_status="down"`) and decline to suggest the obvious `config interface startup Ethernet4` fix, which matches the architectural intent.

The end-to-end runner's stdout/stderr split was verified by running with `python3 main.py >/tmp/diag.json 2>/tmp/diag.err` and confirming `/tmp/diag.json` parses as JSON with `python3 -m json.tool`.

## What was learned

Six findings worth surfacing.

**SONiC service bootstrap is not trivial.** The `docker-sonic-vs-fixed` image's default entrypoint is supervisord, but `bgpd` is configured with `autostart=false` and is left STOPPED, and the right boot-complete signal is `start.sh` entering `EXITED` rather than any particular `*mgrd` running. `supervisorctl status` returns exit code 3 whenever any queried program is not RUNNING, including the success-state `EXITED`, which silently kills naive readiness loops under `set -o pipefail`. `scripts/bringup.sh` encodes the right pattern.

**COUNTERS_DB per-port hashes are empty on SONiC VS.** The OID map (`COUNTERS_PORT_NAME_MAP`) is populated, but `HGETALL COUNTERS:<oid>` returns no SAI counter fields without ASIC traffic — `flex_counter` has not been observed to populate per-port stats on the virtual switch. `collect_interface_counters` reports zeros and writes the empty-hash state into its `source` field. On production hardware these counters are expected to be meaningful; Phase 1 only verified behavior against SONiC VS.

**SONiC VS emits a synthetic oper-error cascade on admin-down.** When `config interface shutdown Ethernet4` runs, SONiC VS writes roughly fifteen `oper error event:` log lines to syslog (`mac_local_fault`, `mac_remote_fault`, `fec_sync_loss`, `fec_alignment_loss`, `high_ser_error`, `high_ber_error`, `crc_rate`, `data_unit_crc_error`, `code_group_error`, `signal_local_error`, `no_rx_reachability`, and others) as if the port had hardware faults. These messages are literally present in `/var/log/syslog` but they are virtual-switch artifacts of the admin-shutdown transition, not real physical faults. On real hardware, `mac_local_fault` would mean an actual fault. Without filtering, the diagnosis narrator faithfully reports the cascade and a reader would misdiagnose intentional admin-shutdown as hardware failure. `main.py` filters these lines before passing logs to the diagnosis agent for this one scenario. The filter is scenario-specific to admin-down; later phases will need their own log hygiene per scenario.

**CONFIG_DB has measurable read-after-write lag.** The `config interface shutdown` CLI returns before the corresponding CONFIG_DB key is necessarily readable. Project 1 measured 60-80 ms across five iterations. The Phase 1 fault script uses a 2-second polling deadline with 50 ms intervals to accommodate this. A single immediate HGET after the CLI call would be racy and the failure mode would be confusing.

**qwen2.5:7b is a narrator, not an investigator.** Given structured evidence and the strict narrator system prompt, the model produces diagnoses that quote actual field values, identify gaps using the "insufficient evidence" phrase as instructed, and stay within the narrator role. Given noisy or misleading evidence (the synthetic oper-error cascade above) it produces plausible but wrong network narratives unless that evidence is filtered out at the runner layer. The decision to have Python collect facts and Qwen explain them is what makes a 7B model useful at all in this loop.

**The BGP collector parser has only been exercised on the no-instance-configured baseline.** `vtysh show bgp summary json` returns `{}` when no BGP is configured, and `collect_bgp_summary` handles that as `bgp_instance_present=False` with an empty `neighbors` list. The parser also handles `ipv4Unicast` and `ipv6Unicast` peer dicts in the FRR JSON, but Phase 1 has not exercised that code path against a real session. When later phases introduce BGP fault scenarios, the parser will need verification against live neighbor JSON and may need adjustment for richer FRR fields (RPKI, multipath, route-reflector state, and so on).

## What was deliberately scoped out

- More than one fault scenario. Phase 2 will add the other five planned scenarios (BGP neighbor removal, BGP ASN mismatch, `bgpd` container restart, route missing, counter/log-based degradation).
- More than one agent on the blackboard. The triage, interface-state, BGP/routing, and logs/counters agents do not exist yet. Their responsibilities are represented in Phase 1 by plain collector functions and main.py wiring. Multi-agent participation is a Phase 2-3 concern.
- Fan-out fan-in inside individual agents (Phase 3).
- Evaluation harness (Phase 4).
- Multi-switch topology. Single SONiC VS container.
- Automatic remediation. The agent diagnoses only.
- Memory across investigations.
- Multi-turn conversation with the user.
- Token, latency, or model-size optimization. `qwen2.5:7b-instruct` and the default Ollama configuration are used as-is.
- Production concerns: authentication, audit logging beyond the in-memory blackboard, concurrent investigators, multi-operator coordination.

## Known limitations

Single SONiC VS container, single switch. The whole runner assumes one container named `sonic-vs-troubleshoot`.

The target interface is hardcoded as `Ethernet4` in `main.py`, the fault script, and the runner-layer log filter. Generalizing to other interfaces is straightforward but out of scope for Phase 1.

The synthetic-fault filter in `main.py._filter_logs_for_interface` is scenario-specific. It drops lines containing `oper error event:` because those are virtual-switch artifacts for the admin-down case. Other Phase 2+ scenarios will need their own per-scenario log hygiene; there is no general log-noise filter today.

`restore` returns `admin_status` to the explicit value `up`, not the original "field absent" state that an unconfigured port has by default. This is a user-approved trade-off acceptable for Phase 1's reversibility requirement; a future test-fixture cleanup could `HDEL` the field if the original state matters.

Qwen output is non-deterministic. `temperature=0.2` reduces variance but does not eliminate it. The same evidence will produce slightly different narratives across runs.

The pyright static checker flags the `from blackboard.blackboard import Blackboard` and similar imports in `agents/diagnosis.py` and `main.py` as unresolved because the runtime `sys.path` insert is invisible to static analysis. The imports work at runtime when each file is run from the repo root or via `python3 main.py`. This is the cost of choosing namespace packages (no `__init__.py`) for Phase 1; later phases may move to a package layout if the cost grows.

## Citations

The arxiv links below were used as architectural reference points during Phase 1 design. Where details beyond an arxiv ID, a short description, and (where known) author and affiliation are not stated here, they were not verified.

- arxiv 2507.01701 — blackboard architecture for LLM multi-agent systems (Han, Zhang, Chinese Academy of Sciences, July 2025). <https://arxiv.org/abs/2507.01701>
- arxiv 2509.20600 — LLM agent framework compiling YANG to SONiC (Lin, Zhou, Yu — Meta / Stony Brook / Harvard, September 2025). Code at <https://github.com/jzhou316/LLM-networking-control>
- arxiv 2512.16381 — NIKA benchmark for LLM agents on network troubleshooting using Kathara (December 2025). Source of the GPT-OSS:20B 19 / 5.5 / 5.5% detection / localization / root-cause numbers cited in the architecture decisions. <https://arxiv.org/abs/2512.16381>
- Aviz Network Copilot — commercial reference using a fine-tuned Llama 70B on SONiC. <https://aviznetworks.com>
