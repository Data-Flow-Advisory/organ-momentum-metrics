# organ-momentum-metrics

A pure, stdlib-only **decision organ** extracted from discovery-engine's
`app/services/momentum_metrics.py`.

It classifies **one completed work item** for the platform's momentum curves:
*Useful Output Rate* and *Self-improvement Rate*. The original service mixed
side-effecting SQLAlchemy aggregation (per-day bucketing across
`LocalCronWorkItem`, `PendingWidgetAction`, `ApiCall`, `Project`) with the pure
per-completion classification policy that decides whether each item *counts*.
This organ extracts **only** that pure policy.

## Contract

`decide(state, context) -> {output, rationale, self_metric}`

The organ reads `{state, context}` JSON on stdin (or via the `ORGAN_INPUT`
env var — a literal JSON value or a path to a `.json` file) and writes
`{output, rationale, self_metric}` JSON on stdout.

### Input

```json
{
  "state": {
    "result_json": {
      "summary": "...",
      "findings": ["..."],
      "memory_candidates": [{"scope": "tenant"}],
      "files_changed": ["app/foo.py"],
      "spawn_tasks": [{"directive": "..."}],
      "sharpening_pct": 12.5
    }
  },
  "context": {
    "skip_patterns": ["null\\s+cycle|no-?op"],
    "useful_summary_min_len": 20
  }
}
```

- `state.result_json` — the completed item's result envelope. As a
  convenience the envelope may be passed as `state` directly (no wrapper).
- `context.skip_patterns` *(optional)* — list of regex strings overriding the
  built-in scheduler-throttle patterns. Mirrors the source's reuse of
  `persona_service.SKIP_PATTERNS` so a new upstream throttle automatically
  tightens this filter. Invalid patterns are skipped (fail-open).
- `context.useful_summary_min_len` *(optional, default 20)* — minimum
  stripped summary length for the `summary+findings` usefulness signal.

### Output

```json
{
  "output": {
    "is_useful": true,
    "is_throttle_skip": false,
    "is_improvement": false,
    "has_tenant_memory": false,
    "memory_candidate_count": 1,
    "sharpening_pct": 12.5,
    "useful_reasons": ["summary+findings", "memory_candidates"],
    "classification": "useful"
  },
  "rationale": "concrete work present (...) → useful; ...",
  "self_metric": {"confidence": 0.95, "signal_count": 2, "memory_candidate_count": 1}
}
```

`classification` ∈ `{"useful", "throttle_skip", "empty"}`.

## Faithful mapping from the source

| momentum_metrics.py | this organ |
|---|---|
| `_is_summary_throttle_skip(summary)` | `output.is_throttle_skip` |
| `_is_useful(result_json)` | `output.is_useful` + `output.useful_reasons` |
| `_has_tenant_scope_memory(result_json)` | `output.has_tenant_memory` |
| tenant-memory `improvement_by_day` bump | `output.is_improvement` |
| `memory_growth_by_day += len(mcs)` | `output.memory_candidate_count` |
| `sharpening_inputs` collection | `output.sharpening_pct` |
| `_FALLBACK_SKIP_PATTERNS` | built-in default patterns (override via context) |

**Key rule preserved:** a result is *useful* only if it produced concrete work
(substantive summary+findings, memory candidates, files changed, or spawn
tasks) **AND** its summary does not match a throttle-meta skip pattern — so the
curve isn't gameable by spamming cycle/pulse/delta reports.

## Purity & fail-safe

No DB, no network, deterministic. On malformed or empty state it fails safe to
a **non-useful / non-improvement** `"empty"` classification with
`confidence = 0.0` — never a confident false `"useful"`.

## Run

```bash
echo '{"state":{"result_json":{"files_changed":["a.py"]}}}' | python3 organ.py
python3 -m pytest -q          # tests
python3 check_contract.py     # contract check across samples/
```

## CI

`.github/workflows/conformance.yml` runs the contract check (every `samples/*.json`
plus an empty-state probe must satisfy the `{output, rationale, self_metric.confidence}`
shape) and the pytest suite on every push/PR.
