# Phase 3: Multi-agent participation on the blackboard

## What was built

Four specialist agents plus an updated diagnosis agent that
synthesizes their hypotheses alongside raw evidence. Specialists are
invoked via a fan-out / fan-in step in the runner using
`concurrent.futures.ThreadPoolExecutor`.

The four specialists:

- `agents/triage.py` — reads only the user complaint; posts 1-3
  initial hypotheses about what kind of network issue this might be.
  Has no access to collector evidence.
- `agents/interface_specialist.py` — reads `interface_state` and
  `interface_counters`; posts hypotheses scoped to admin/oper state,
  errors, and drops.
- `agents/bgp_specialist.py` — reads `bgp_summary`; posts hypotheses
  scoped to BGP session state (neighbor present/absent, session
  state, remote ASN).
- `agents/logs_specialist.py` — reads `recent_logs`; posts
  hypotheses scoped to what the log lines actually say. Refuses to
  speculate beyond the log content.

`agents/diagnosis.py` was modified only at the system-prompt level:
a paragraph appended explaining that the model now sees both
collector evidence and specialist hypotheses, with instructions to
validate each hypothesis against evidence, flag contradictions, and
weight agreement. The function signature and HTTP plumbing are
unchanged.

`main.py` adds the fan-out / fan-in section between blackboard
population and the diagnosis call.


## Architecture

The runner sequence is now:

    BGP LAB UP (if scenario requires it)
    BEFORE snapshot (4 collectors)
    INJECT fault
    sleep (post-inject delay)
    AFTER snapshot (4 collectors)
    apply scenario evidence filter (if any)
    populate Blackboard
    --- new: fan-out ---
    triage, interface, bgp, logs specialists run concurrently
    each reads its slice from the blackboard and posts hypotheses
    --- new: fan-in ---
    diagnosis agent reads evidence + all surviving hypotheses
    print diagnosis JSON
    RESTORE
    BGP LAB DOWN (if scenario requires it)

Each specialist talks to the blackboard exclusively. There are no
agent-to-agent direct calls; the blackboard is the only shared state.
The runner does not feed specialist outputs to other specialists.


## Authorship attribution via claim prefix

The blackboard schema records `{claim, confidence,
supporting_evidence}` per hypothesis but has no `author` field. To
let the diagnosis agent attribute hypotheses to their source
specialist without extending the schema, each specialist prefixes its
claim text with a bracketed tag — `[triage]`, `[interface]`, `[bgp]`,
`[logs]`. The diagnosis system prompt documents this convention so
the model can parse and weigh hypotheses by source. This is a
deliberately low-tech design choice; a richer audit trail would
deserve a real schema change but is out of scope here.


## Structured output via line-pair parsing

Each specialist's system prompt forces the model to emit one
`HYPOTHESIS:` line followed by one `CONFIDENCE: <high|medium|low>`
line per hypothesis, with no preamble or explanation. The Python
parser (`_parse_hypotheses` in each specialist file) walks the
output line-by-line, treating any line not matching the pair format
as ignorable. Confidence words map to floats via a constant table:
`high → 0.8`, `medium → 0.5`, `low → 0.2`. Unrecognized confidence
words fall back to 0.5.

This is the simplest structured-output trick that works with a 7B
model. It is not robust to a model that decides to ignore the format
entirely; that scenario would simply produce zero hypotheses for
that specialist, which is non-fatal at the runner level.


## Concurrency notes

`blackboard/blackboard.py` has no explicit lock. This is acceptable
for the fixed fan-out / fan-in pattern this phase implements because:

- Each specialist only appends to `_hypotheses` via
  `add_hypothesis`. Under CPython, `list.append` does not require
  external synchronization.
- Each specialist's read (`to_dict`) is a defensive deep copy and
  does not observe a partial write.
- `main.py` does not read hypotheses until all futures have
  completed via `as_completed`.

Wall-clock speedup from running the four specialists concurrently
is bounded by Ollama's internal serialization of inference requests
on a single local instance. On the development M4 Pro setup the
end-to-end runtime (lab up + scenario + fan-out + fan-in + restore
+ lab down) was ~30s for both verified scenarios; the architecture is
correct regardless of whether Ollama happens to pipeline the four
calls or queue them.

The output ordering of the "posted hypotheses" lines is by future
completion, not submission. The four lines may appear in any order.
That is expected.


## Why one model for all agents

A 7B model is the practical local option. Different agents do not
need different models; they need different system prompts and
different evidence slices. The specialization comes from prompt
constraints and the slice of blackboard data each specialist sees,
not from model capability. All five agents use `qwen2.5:7b-instruct`
at `temperature=0.2` against Ollama on `localhost:11434`.


## What works

Two scenarios verified end-to-end on the local development setup:

- `python3 main.py --scenario interface_admin_down` — fan-out
  posted hypotheses from all four specialists (no specialist
  failed). The diagnosis text named a `[triage]` hypothesis by
  attribution prefix and commented on whether the evidence
  supported it; the other three specialists' hypotheses were on
  the blackboard but were not explicitly named by tag in the
  diagnosis text on this observed run. Interface restored to
  `admin_status=up` afterwards.
- `python3 main.py --scenario bgp_neighbor_removal` — same
  structural verification under the two-container BGP lab.
  Fan-out posted hypotheses from all four specialists; the
  diagnosis text again named the `[triage]` prefix and grounded
  its narrative in collector evidence (referencing
  `bgp_summary` and `recent_logs` content). SUT BGP returns to
  `{}`, peer container and lab network removed.

These two runs show the architecture works, not that the
diagnosis model always cites every specialist by tag. Empirically,
the `[triage]` tag was the one consistently named in the diagnosis
text; the other specialists' hypotheses sit on the blackboard and
are available to the model regardless of whether the model chose
to quote them by tag.

The third Phase 2 scenario (`bgp_asn_mismatch`) uses the same
runner path and the same specialist set; nothing in Phase 3 is
scenario-specific.

The standard runner properties are preserved: stdout is the
diagnosis JSON only (parses as JSON cleanly); section headers,
specialist completion lines, and inject/restore output go to stderr.


## Honest limitations

- Specialists do not negotiate or cross-validate. Each writes
  independently and never sees another specialist's hypotheses.
  Synthesis happens once at the diagnosis-agent step.
- Confidence scores are model-self-reported and not calibrated.
  `high/medium/low` map to fixed floats; the mapping has not been
  evaluated against actual hypothesis-correctness rates.
- The blackboard pattern's full power — a controller picking which
  knowledge source runs next based on accumulated state — is not
  implemented. The specialists always run in parallel regardless of
  evidence content. A scheduler-driven version is Phase-4-or-later
  scope.
- Ollama serializes inference on a single local instance. Parallel
  specialist invocation is architecturally correct but may not show
  wall-clock speedup over a serial implementation.
- There is no per-specialist evaluation harness yet. Hypothesis
  quality is anecdotal from the two verified scenarios; rigorous
  scoring is Phase 4.
- The HYPOTHESIS:/CONFIDENCE: parser silently drops malformed
  output rather than retrying. A specialist whose model output
  ignores the format will produce zero hypotheses; the runner
  reports this as `<specialist>: posted hypotheses` (with zero
  effect) rather than as a failure, which is a minor observability
  gap.


## How to run

Same as Phase 2:

    ./scripts/bringup.sh
    python3 main.py --scenario interface_admin_down
    python3 main.py --scenario bgp_neighbor_removal
    python3 main.py --scenario bgp_asn_mismatch

Each scenario now exercises four specialists in fan-out and the
diagnosis agent in fan-in. Expected runtime is roughly 30 seconds
per scenario on the M4 Pro reference setup (most of it Ollama
inference). `--dry-run` lists the fan-out and fan-in steps in the
plan but does not invoke Ollama.
