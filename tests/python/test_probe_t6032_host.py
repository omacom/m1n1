"""Runs the C-probe host harness (board gate, record round trip, leakage,
previews) through pytest so the suite covers it."""

import os
import subprocess
import sys

ROOT = os.environ.get("PROBE_REPO_ROOT",
                      os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_probe_t6032_host_harness():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tests/run_probe_t6032_host.py")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "ALL PASS" in r.stdout
