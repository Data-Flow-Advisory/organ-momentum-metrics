#!/usr/bin/env python3
"""Tests for the momentum-metrics classification organ.

These pin the faithful mapping from app/services/momentum_metrics.py's pure
helpers (`_is_useful`, `_is_summary_throttle_skip`, `_has_tenant_scope_memory`,
the memory-growth count, the sharpening collection) onto decide()'s output.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import organ
from organ import decide, run_organ


HERE = Path(__file__).parent


# --------------------------------------------------------------------------- #
# _is_useful mapping                                                          #
# --------------------------------------------------------------------------- #

def test_summary_plus_findings_is_useful():
    rj = {"summary": "Did real substantive work here", "findings": ["a", "b"]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_useful"] is True
    assert "summary+findings" in out["useful_reasons"]
    assert out["classification"] == "useful"


def test_short_summary_with_findings_not_useful_via_that_signal():
    # Summary too short (<=20 chars) so the summary+findings signal fails.
    rj = {"summary": "short", "findings": ["a"]}
    out = decide({"result_json": rj}, {})["output"]
    assert "summary+findings" not in out["useful_reasons"]
    assert out["is_useful"] is False
    assert out["classification"] == "empty"


def test_summary_without_findings_not_useful():
    rj = {"summary": "A long enough summary but no findings list at all here"}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_useful"] is False


def test_memory_candidates_alone_makes_useful():
    rj = {"memory_candidates": [{"name": "x", "scope": "persona"}]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_useful"] is True
    assert "memory_candidates" in out["useful_reasons"]


def test_files_changed_alone_makes_useful():
    rj = {"files_changed": ["app/foo.py"]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_useful"] is True
    assert out["useful_reasons"] == ["files_changed"]


def test_spawn_tasks_alone_makes_useful():
    rj = {"spawn_tasks": [{"directive": "do the thing"}]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_useful"] is True
    assert "spawn_tasks" in out["useful_reasons"]


def test_empty_result_not_useful():
    out = decide({"result_json": {}}, {})["output"]
    assert out["is_useful"] is False
    assert out["useful_reasons"] == []
    assert out["classification"] == "empty"


def test_non_dict_result_not_useful():
    out = decide({"result_json": None}, {})["output"]
    assert out["is_useful"] is False
    assert out["classification"] == "empty"


def test_multiple_signals_collected_in_order():
    rj = {
        "summary": "A nice long substantive summary of the work",
        "findings": ["x"],
        "memory_candidates": [{"name": "m"}],
        "files_changed": ["a.py"],
        "spawn_tasks": [{"d": "y"}],
    }
    out = decide({"result_json": rj}, {})["output"]
    assert out["useful_reasons"] == [
        "summary+findings", "memory_candidates", "files_changed", "spawn_tasks",
    ]


# --------------------------------------------------------------------------- #
# throttle-skip mapping                                                       #
# --------------------------------------------------------------------------- #

def test_null_cycle_summary_is_throttle_skip():
    rj = {"summary": "null cycle — nothing to do", "findings": ["x"]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_throttle_skip"] is True
    # Throttle-skip overrides usefulness even with concrete work present.
    assert out["is_useful"] is False
    assert out["classification"] == "throttle_skip"


def test_noop_variants_match():
    for s in ["no-op result", "noop", "wrapper fallback", "all draft, no send"]:
        rj = {"summary": s, "findings": ["x"]}
        out = decide({"result_json": rj}, {})["output"]
        assert out["is_throttle_skip"] is True, s


def test_stale_dedup_throttle():
    rj = {"summary": "stale dedup — all stale, none prs stale", "findings": ["x"]}
    assert decide({"result_json": rj}, {})["output"]["is_throttle_skip"] is True


def test_no_drift_throttle():
    rj = {"summary": "no drift detected this cycle", "files_changed": ["a.py"]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_throttle_skip"] is True
    assert out["is_useful"] is False


def test_normal_summary_not_throttle_skip():
    rj = {"summary": "Implemented the new feature and added tests", "findings": ["x"]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_throttle_skip"] is False
    assert out["is_useful"] is True


def test_blank_summary_not_throttle_skip():
    rj = {"summary": "   ", "files_changed": ["a.py"]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_throttle_skip"] is False


def test_non_string_summary_not_throttle_skip():
    rj = {"summary": 12345, "files_changed": ["a.py"]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["is_throttle_skip"] is False


def test_context_skip_patterns_override():
    rj = {"summary": "FROBNICATE everything", "findings": ["x"]}
    ctx = {"skip_patterns": ["frobnicate"]}
    out = decide({"result_json": rj}, ctx)["output"]
    assert out["is_throttle_skip"] is True
    # And the built-in patterns no longer apply when overridden.
    rj2 = {"summary": "null cycle", "findings": ["x"]}
    out2 = decide({"result_json": rj2}, ctx)["output"]
    assert out2["is_throttle_skip"] is False


def test_bad_override_pattern_skipped_not_fatal():
    rj = {"summary": "a sufficiently long substantive summary", "findings": ["x"]}
    ctx = {"skip_patterns": ["(unclosed"]}  # invalid regex
    out = decide({"result_json": rj}, ctx)["output"]
    # invalid pattern dropped → no throttle match → useful
    assert out["is_throttle_skip"] is False
    assert out["is_useful"] is True


# --------------------------------------------------------------------------- #
# tenant-memory / improvement mapping                                         #
# --------------------------------------------------------------------------- #

def test_tenant_scope_memory_is_improvement():
    rj = {"memory_candidates": [{"name": "x", "scope": "tenant"}]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["has_tenant_memory"] is True
    assert out["is_improvement"] is True


def test_persona_scope_memory_not_improvement():
    rj = {"memory_candidates": [{"name": "x", "scope": "persona"}]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["has_tenant_memory"] is False
    assert out["is_improvement"] is False


def test_improvement_independent_of_throttle_skip():
    # A throttle-skipped summary can still carry a tenant memory candidate;
    # the source bumps improvement_by_day regardless of usefulness.
    rj = {
        "summary": "null cycle",
        "memory_candidates": [{"name": "x", "scope": "tenant"}],
    }
    out = decide({"result_json": rj}, {})["output"]
    assert out["classification"] == "throttle_skip"
    assert out["is_improvement"] is True


def test_mixed_scope_memory_detects_tenant():
    rj = {"memory_candidates": [{"scope": "persona"}, {"scope": "tenant"}]}
    assert decide({"result_json": rj}, {})["output"]["has_tenant_memory"] is True


# --------------------------------------------------------------------------- #
# memory count + sharpening mapping                                           #
# --------------------------------------------------------------------------- #

def test_memory_candidate_count():
    rj = {"memory_candidates": [{"a": 1}, {"b": 2}, {"c": 3}]}
    out = decide({"result_json": rj}, {})["output"]
    assert out["memory_candidate_count"] == 3


def test_memory_candidate_count_zero_when_absent():
    out = decide({"result_json": {"summary": "x"}}, {})["output"]
    assert out["memory_candidate_count"] == 0


def test_sharpening_pct_numeric():
    rj = {"summary": "x", "sharpening_pct": 12.5}
    assert decide({"result_json": rj}, {})["output"]["sharpening_pct"] == 12.5


def test_sharpening_pct_int_coerced_to_float():
    rj = {"sharpening_pct": 7}
    assert decide({"result_json": rj}, {})["output"]["sharpening_pct"] == 7.0


def test_sharpening_pct_non_numeric_is_none():
    rj = {"sharpening_pct": "n/a"}
    assert decide({"result_json": rj}, {})["output"]["sharpening_pct"] is None


def test_sharpening_pct_bool_rejected():
    rj = {"sharpening_pct": True}
    assert decide({"result_json": rj}, {})["output"]["sharpening_pct"] is None


# --------------------------------------------------------------------------- #
# state-shape flexibility + fail-safe                                         #
# --------------------------------------------------------------------------- #

def test_state_is_envelope_directly():
    # Caller passes the result envelope as state itself (no result_json wrapper).
    out = decide({"summary": "long enough substantive summary", "findings": ["a"]}, {})["output"]
    assert out["is_useful"] is True


def test_empty_state_fail_safe():
    res = decide({}, {})
    assert res["output"]["classification"] == "empty"
    assert res["output"]["is_useful"] is False
    assert res["self_metric"]["confidence"] == 0.0


def test_run_organ_non_dict_input():
    res = run_organ([1, 2, 3])
    assert res["output"]["is_useful"] is False
    assert res["self_metric"]["confidence"] == 0.0


def test_confidence_high_on_throttle_skip():
    rj = {"summary": "no-op", "findings": ["x"]}
    assert decide({"result_json": rj}, {})["self_metric"]["confidence"] >= 0.9


def test_confidence_high_on_multi_signal():
    rj = {"summary": "substantive long summary here", "findings": ["a"], "files_changed": ["b.py"]}
    assert decide({"result_json": rj}, {})["self_metric"]["confidence"] >= 0.9


def test_useful_summary_min_len_override():
    rj = {"summary": "tiny", "findings": ["a"]}
    out = decide({"result_json": rj}, {"useful_summary_min_len": 2})["output"]
    assert out["is_useful"] is True


def test_self_metric_signal_count():
    rj = {"summary": "a substantive summary of work", "findings": ["a"], "spawn_tasks": [{"d": "x"}]}
    sm = decide({"result_json": rj}, {})["self_metric"]
    assert sm["signal_count"] == 2


# --------------------------------------------------------------------------- #
# contract / CLI                                                              #
# --------------------------------------------------------------------------- #

def test_contract_keys_present():
    res = decide({"result_json": {"summary": "x"}}, {})
    assert set(res.keys()) == {"output", "rationale", "self_metric"}
    assert isinstance(res["rationale"], str) and res["rationale"]
    assert 0.0 <= res["self_metric"]["confidence"] <= 1.0


def test_cli_via_env(tmp_path):
    sample = tmp_path / "in.json"
    sample.write_text(json.dumps({"state": {"result_json": {"files_changed": ["a.py"]}}}))
    env = os.environ.copy()
    env["ORGAN_INPUT"] = str(sample)
    out = subprocess.run([sys.executable, str(HERE / "organ.py")],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0
    data = json.loads(out.stdout)
    assert data["output"]["is_useful"] is True


def test_cli_invalid_json_failsafe():
    env = os.environ.copy()
    env["ORGAN_INPUT"] = "{not json"
    out = subprocess.run([sys.executable, str(HERE / "organ.py")],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 1
    data = json.loads(out.stdout)
    assert data["self_metric"]["confidence"] == 0.0
    assert data["output"]["classification"] == "empty"


def test_all_samples_conform():
    for sp in sorted((HERE / "samples").glob("*.json")):
        data = json.loads(sp.read_text())
        res = run_organ(data)
        assert "output" in res and "rationale" in res and "self_metric" in res
        assert 0.0 <= res["self_metric"]["confidence"] <= 1.0
