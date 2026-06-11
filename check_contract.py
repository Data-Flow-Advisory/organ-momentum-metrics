#!/usr/bin/env python3
"""Check that organ.py conforms to the contract on all samples."""

import json
import subprocess
import sys
import os
from pathlib import Path

def check_contract_on_sample(sample_path):
    """Run organ.py on a sample file and check the contract."""
    env = os.environ.copy()
    env["ORGAN_INPUT"] = str(sample_path)

    result = subprocess.run(
        ["python3", "organ.py"],
        env=env,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"::error::organ.py exited with code {result.returncode} on {sample_path}")
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        return False

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"::error::Invalid JSON output from organ.py on {sample_path}: {e}")
        print("Output:", result.stdout)
        return False

    # Check contract
    if not isinstance(data, dict):
        print(f"::error::Top-level output is not a JSON object on {sample_path}")
        return False

    for key in ("output", "rationale", "self_metric"):
        if key not in data:
            print(f"::error::Contract violation on {sample_path}: missing {key!r}")
            return False

    if not isinstance(data["self_metric"], dict):
        print(f"::error::Contract violation on {sample_path}: self_metric is not a dict")
        return False

    if "confidence" not in data["self_metric"]:
        print(f"::error::Contract violation on {sample_path}: self_metric.confidence is required")
        return False

    confidence = data["self_metric"]["confidence"]
    if not isinstance(confidence, (int, float)):
        print(f"::error::Contract violation on {sample_path}: confidence is not numeric (got {type(confidence).__name__})")
        return False

    if not (0.0 <= confidence <= 1.0):
        print(f"::error::Contract violation on {sample_path}: confidence {confidence} is out of range [0.0, 1.0]")
        return False

    if "error" in data["self_metric"]:
        print(f"::error::Contract violation on {sample_path}: self_metric has 'error' key (must be valid)")
        return False

    print(f"✓ Contract OK: {sample_path}")
    return True

def main():
    """Check contract on all samples and empty state."""
    if not Path("organ.py").exists():
        print("::error::no top-level organ.py — this is not a contract-conforming organ")
        sys.exit(1)

    # Check empty state
    env = os.environ.copy()
    env["ORGAN_INPUT"] = '{"state":{}}'
    result = subprocess.run(
        ["python3", "organ.py"],
        env=env,
        capture_output=True,
        text=True,
    )

    try:
        data = json.loads(result.stdout)
        if "confidence" not in data.get("self_metric", {}):
            print("::error::Empty state contract violation: self_metric.confidence is required")
            sys.exit(1)
        print("✓ Contract OK: empty state")
    except json.JSONDecodeError as e:
        print(f"::error::Invalid JSON on empty state: {e}")
        sys.exit(1)

    # Check all samples
    samples = sorted(Path("samples").glob("*.json"))
    if not samples:
        print("::warning::no samples/*.json to contract-check")
        return

    all_ok = True
    for sample in samples:
        if not check_contract_on_sample(sample):
            all_ok = False

    if not all_ok:
        sys.exit(1)

if __name__ == "__main__":
    main()
