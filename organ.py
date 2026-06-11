#!/usr/bin/env python3
"""
Momentum-Metrics Organ — extracted pure classification logic from discovery-engine.

A pure, stdlib-only decider that reads {state, context} JSON on stdin (or via the
ORGAN_INPUT env var) and writes {output, rationale, self_metric} on stdout.

It does NOT touch the database. The original `app/services/momentum_metrics.py`
mixed two things:
  1. side-effecting SQLAlchemy aggregation (querying LocalCronWorkItem,
     PendingWidgetAction, ApiCall, Project; bucketing per-day; building the
     full dashboard payload), and
  2. pure CLASSIFICATION policy — the per-completion decisions that decide
     whether a single completed work item counts toward the momentum curves.

This organ extracts only (2): given ONE completed work item's `result_json`
(plus optional throttle-pattern overrides), it decides:
  - is the result "useful" (produced concrete work AND not a throttle-meta skip)?
  - does the summary match a scheduler-throttle skip pattern?
  - does the result emit a tenant-scoped memory candidate (the strongest
    "system learning" / self-improvement signal)?
  - how many memory candidates did it emit (memory-pool growth contribution)?
  - what sharpening_pct (if any) does it report?

Faithful mapping from momentum_metrics.py:
  - `_is_summary_throttle_skip(summary)`        -> output.is_throttle_skip
  - `_is_useful(result_json)`                   -> output.is_useful + useful_reasons
  - `_has_tenant_scope_memory(result_json)`     -> output.has_tenant_memory
  - `memory_growth_by_day += len(mcs)`          -> output.memory_candidate_count
  - `sharpening_inputs` collection              -> output.sharpening_pct
  - the `improvement_by_day` tenant-memory bump -> output.is_improvement

The throttle regexes default to the same `_FALLBACK_SKIP_PATTERNS` the source
ships (the local fallback the source uses when the canonical
`persona_service.SKIP_PATTERNS` import is unavailable). A caller can override
them via `context.skip_patterns` (a list of regex strings) so a new upstream
throttle automatically tightens this filter — exactly the source's intent.

Contract:
  INPUT:  {
    "state": {
      "result_json": {            # the completed item's result envelope
        "summary": "...",
        "findings": [...],
        "memory_candidates": [{"scope": "tenant", ...}, ...],
        "files_changed": [...],
        "spawn_tasks": [...],
        "sharpening_pct": 12.5
      }
    },
    "context": {
      "skip_patterns": ["null\\s+cycle|no-?op", ...],  # optional regex overrides
      "useful_summary_min_len": 20                      # optional, default 20
    }
  }

  OUTPUT: {
    "output": {
      "is_useful": true,
      "is_throttle_skip": false,
      "is_improvement": false,
      "has_tenant_memory": false,
      "memory_candidate_count": 2,
      "sharpening_pct": 12.5,
      "useful_reasons": ["summary+findings"],
      "classification": "useful" | "throttle_skip" | "empty"
    },
    "rationale": "<why>",
    "self_metric": {"confidence": 0.0, ...}
  }

The organ is pure: all inputs arrive via JSON, it makes no DB/network calls, it
is deterministic, and it fails safe to a NON-useful / NON-improvement
classification (never a confident false "useful") on malformed or empty state.
"""

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional


# --- Local copies of the source's throttle regexes (used as defaults) --------
# In production momentum_metrics.py imports the canonical SKIP_PATTERNS from
# persona_service so a new throttle automatically tightens the filter; these
# fallbacks exist for callers that don't want the full dependency chain. A
# caller can override them via context.skip_patterns. Mirrors the
# `_FALLBACK_SKIP_PATTERNS` dict in app/services/momentum_metrics.py.
_FALLBACK_SKIP_PATTERNS: List[str] = [
    r"null\s+cycle|all\s+draft|no[\s\-]?op|wrapper\s+fallback",
    r"stale[\s\-]?dedup|all\s+stale|none\s+prs?\s+stale",
    r"convergent[\s\-_]?skip|sequential\s+convergent",
    r"no[\s_\-]?new[\s_\-]?commands?|zero[\s_\-]?command",
    r"no[\s_\-]?drift|drift[\s_\-]?(?:resolved|closed)|"
    r"prod\s+\w+\s+(?:==|exactly\s+matches)\s+local",
]

_DEFAULT_USEFUL_SUMMARY_MIN_LEN = 20


def _compiled_skip_patterns(context: Dict[str, Any]) -> List["re.Pattern[str]"]:
    """Compile the active throttle patterns. Prefers context.skip_patterns
    (list of regex strings); falls back to the source's built-in set. Any
    individual pattern that fails to compile is skipped (fail-open, like the
    source's try/except around the canonical import)."""
    raw = None
    if isinstance(context, dict):
        cand = context.get("skip_patterns")
        if isinstance(cand, list) and cand:
            raw = [p for p in cand if isinstance(p, str)]
    if not raw:
        raw = _FALLBACK_SKIP_PATTERNS
    compiled: List["re.Pattern[str]"] = []
    for pat in raw:
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error:
            continue
    return compiled


def _is_summary_throttle_skip(summary: Any, patterns: List["re.Pattern[str]"]) -> bool:
    """True if the result summary matches any active scheduler-throttle
    pattern. Mirrors `_is_summary_throttle_skip` in the source."""
    if not isinstance(summary, str) or not summary.strip():
        return False
    return any(pat.search(summary) for pat in patterns)


def _useful_reasons(result_json: Dict[str, Any], min_len: int) -> List[str]:
    """The concrete-work signals present, in the source's evaluation order.
    Mirrors `_is_useful`: substantive summary+findings, memory_candidates,
    files_changed, or spawn_tasks."""
    reasons: List[str] = []
    summary = result_json.get("summary")
    findings = result_json.get("findings") or []
    if (
        isinstance(summary, str) and len(summary.strip()) > min_len
        and isinstance(findings, list) and len(findings) > 0
    ):
        reasons.append("summary+findings")
    if isinstance(result_json.get("memory_candidates"), list) and result_json["memory_candidates"]:
        reasons.append("memory_candidates")
    if isinstance(result_json.get("files_changed"), list) and result_json["files_changed"]:
        reasons.append("files_changed")
    if isinstance(result_json.get("spawn_tasks"), list) and result_json["spawn_tasks"]:
        reasons.append("spawn_tasks")
    return reasons


def _has_tenant_scope_memory(result_json: Dict[str, Any]) -> bool:
    """True if the result emits at least one tenant-scoped memory_candidate.
    Tenant scope means the knowledge persists for the whole tenant — the
    strongest 'system learning' signal. Mirrors `_has_tenant_scope_memory`."""
    mcs = result_json.get("memory_candidates") or []
    if not isinstance(mcs, list):
        return False
    for m in mcs:
        if isinstance(m, dict) and m.get("scope") == "tenant":
            return True
    return False


def _memory_candidate_count(result_json: Dict[str, Any]) -> int:
    """Number of memory candidates emitted — the per-item contribution to the
    memory-pool growth curve. Mirrors `memory_growth_by_day += len(mcs)`."""
    mcs = result_json.get("memory_candidates")
    return len(mcs) if isinstance(mcs, list) else 0


def _sharpening_pct(result_json: Dict[str, Any]) -> Optional[float]:
    """The numeric sharpening_pct if present, else None. Mirrors the source's
    `sharpening_inputs` collection (it only keeps int/float values)."""
    sp = result_json.get("sharpening_pct")
    if isinstance(sp, bool):
        return None  # bool is an int subclass; the source's isinstance check
        # accepts it, but a boolean sharpening% is never a real measurement.
    if isinstance(sp, (int, float)):
        return float(sp)
    return None


def decide(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a single completed work item for the momentum curves.

    Pure: no DB, no network. Deterministic. Fails safe to a non-useful /
    non-improvement classification.
    """
    if not isinstance(state, dict):
        state = {}
    if not isinstance(context, dict):
        context = {}

    # The result envelope may be passed as state.result_json, or state itself
    # may BE the result envelope (convenience for callers).
    result_json = state.get("result_json")
    if not isinstance(result_json, dict):
        # Treat the state minus the wrapper keys as the envelope if it looks
        # like one; otherwise empty.
        if "result_json" in state:
            result_json = {}
        elif any(k in state for k in ("summary", "findings", "memory_candidates",
                                      "files_changed", "spawn_tasks", "sharpening_pct")):
            result_json = state
        else:
            result_json = {}

    min_len = context.get("useful_summary_min_len", _DEFAULT_USEFUL_SUMMARY_MIN_LEN)
    if not isinstance(min_len, int) or isinstance(min_len, bool) or min_len < 0:
        min_len = _DEFAULT_USEFUL_SUMMARY_MIN_LEN

    patterns = _compiled_skip_patterns(context)

    summary = result_json.get("summary")
    is_throttle_skip = _is_summary_throttle_skip(summary, patterns)

    reasons = _useful_reasons(result_json, min_len)
    # The source: useful = concrete work AND NOT throttle-skip.
    is_useful = bool(reasons) and not is_throttle_skip

    has_tenant_memory = _has_tenant_scope_memory(result_json)
    # In the source a tenant-scoped memory candidate bumps improvement_by_day
    # (the self-improvement curve) regardless of usefulness.
    is_improvement = has_tenant_memory

    mem_count = _memory_candidate_count(result_json)
    sharpening = _sharpening_pct(result_json)

    if is_throttle_skip:
        classification = "throttle_skip"
    elif is_useful:
        classification = "useful"
    else:
        classification = "empty"

    output = {
        "is_useful": is_useful,
        "is_throttle_skip": is_throttle_skip,
        "is_improvement": is_improvement,
        "has_tenant_memory": has_tenant_memory,
        "memory_candidate_count": mem_count,
        "sharpening_pct": sharpening,
        "useful_reasons": reasons,
        "classification": classification,
    }

    rationale = _build_rationale(
        result_present=bool(result_json),
        classification=classification,
        is_throttle_skip=is_throttle_skip,
        reasons=reasons,
        has_tenant_memory=has_tenant_memory,
        mem_count=mem_count,
    )
    confidence = _confidence(
        result_present=bool(result_json),
        is_throttle_skip=is_throttle_skip,
        reasons=reasons,
        has_tenant_memory=has_tenant_memory,
    )

    return {
        "output": output,
        "rationale": rationale,
        "self_metric": {
            "confidence": confidence,
            "signal_count": len(reasons),
            "memory_candidate_count": mem_count,
        },
    }


def _build_rationale(**kw: Any) -> str:
    if not kw.get("result_present"):
        return ("Empty / missing result envelope — classified 'empty' "
                "(not useful, not an improvement).")
    parts: List[str] = []
    if kw.get("is_throttle_skip"):
        parts.append("summary matches a scheduler-throttle skip pattern → "
                     "classification 'throttle_skip', not useful")
    else:
        reasons = kw.get("reasons") or []
        if reasons:
            parts.append(f"concrete work present ({', '.join(reasons)}) → useful")
        else:
            parts.append("no concrete-work signal (substantive summary+findings, "
                         "memory_candidates, files_changed, or spawn_tasks) → 'empty'")
    if kw.get("has_tenant_memory"):
        parts.append("emits a tenant-scoped memory candidate → counts as a "
                     "self-improvement event")
    mc = kw.get("mem_count") or 0
    if mc:
        parts.append(f"{mc} memory candidate(s) feed memory-pool growth")
    return "; ".join(parts) + "."


def _confidence(**kw: Any) -> float:
    """How sure are we of the classification? High when signals are
    unambiguous (clear throttle skip, or multiple concrete-work signals);
    lower for borderline cases; 0.0 for an empty envelope (fail-safe)."""
    if not kw.get("result_present"):
        return 0.0
    if kw.get("is_throttle_skip"):
        return 0.95
    reasons = kw.get("reasons") or []
    if len(reasons) >= 2:
        return 0.95
    if len(reasons) == 1:
        return 0.85
    # No concrete work — confidently 'empty'.
    return 0.8


def run_organ(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Top-level entry: parse {state, context}, never raise (fail-safe)."""
    try:
        if not isinstance(input_data, dict):
            input_data = {}
        state = input_data.get("state", {})
        context = input_data.get("context", {})
        return decide(state, context)
    except Exception as exc:  # fail-safe: conservative non-useful classification
        return {
            "output": {
                "is_useful": False,
                "is_throttle_skip": False,
                "is_improvement": False,
                "has_tenant_memory": False,
                "memory_candidate_count": 0,
                "sharpening_pct": None,
                "useful_reasons": [],
                "classification": "empty",
            },
            "rationale": f"Error during decision, failing safe to 'empty': {exc}",
            "self_metric": {"confidence": 0.0},
        }


def _failsafe_payload(rationale: str) -> Dict[str, Any]:
    return {
        "output": {
            "is_useful": False,
            "is_throttle_skip": False,
            "is_improvement": False,
            "has_tenant_memory": False,
            "memory_candidate_count": 0,
            "sharpening_pct": None,
            "useful_reasons": [],
            "classification": "empty",
        },
        "rationale": rationale,
        "self_metric": {"confidence": 0.0},
    }


def main() -> None:
    """CLI entry: read JSON from ORGAN_INPUT (value or file path) or stdin."""
    try:
        input_str = os.environ.get("ORGAN_INPUT")
        if input_str:
            if os.path.isfile(input_str):
                with open(input_str, "r") as f:
                    input_str = f.read()
        else:
            input_str = sys.stdin.read()

        input_data = json.loads(input_str) if input_str.strip() else {}
        result = run_organ(input_data)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except json.JSONDecodeError as exc:
        json.dump(
            _failsafe_payload(f"Invalid JSON input: {exc}"),
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
