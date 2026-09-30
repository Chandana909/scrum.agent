"""Fail if the patched aamt test run has failures that the unpatched run doesn't.

Usage: python compare_junit.py baseline.xml patched.xml

Tests that fail with and without the patch (e.g. ones needing a live model or a
platform-specific tool) are reported as notices; failures introduced by the patch are
reported as errors and make the exit code 1. Output uses GitHub Actions annotations, so
results are readable on the run page without opening the logs.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET


def failures(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        for tag in ("failure", "error"):
            el = case.find(tag)
            if el is not None:
                name = f"{case.get('classname')}::{case.get('name')}"
                out[name] = " ".join((el.get("message") or el.text or "").split())[:300]
    return out


def main(baseline_path: str, patched_path: str) -> int:
    baseline, patched = failures(baseline_path), failures(patched_path)
    regressions = {name: msg for name, msg in patched.items() if name not in baseline}
    fixed = sorted(set(baseline) - set(patched))
    for name, msg in sorted(baseline.items()):
        print(f"::notice title=fails without the patch too::{name}: {msg}")
    for name, msg in sorted(regressions.items()):
        print(f"::error title=regression introduced by the patch::{name}: {msg}")
    summary = (f"aamt tests: {len(baseline)} failing without the patch, {len(patched)} with it, "
               f"{len(regressions)} introduced by the patch, {len(fixed)} fixed by it")
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Integration patch check\n\n{summary}\n")
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
