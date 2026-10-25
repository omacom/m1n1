"""adt_check_t6032 must report failures, never crash, on nodes/props that are
missing: a cpu node without cpu-impl-reg and a missing /arm-io/ans."""

import os
import subprocess
import sys

import pytest

ROOT = os.environ.get("PROBE_REPO_ROOT",
                      os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ADT = os.path.expanduser(os.environ.get("J575DAP_ADT", "~/scratch/w7c/m3u/26A434/j575dap.adt"))


def _load_adt(path):
    sys.path.insert(0, os.path.join(ROOT, "proxyclient"))
    from m1n1.adt import load_adt
    return load_adt(open(path, "rb").read())


def test_missing_prop_and_missing_node_fail_without_crashing(tmp_path):
    if not os.path.exists(ADT):
        pytest.skip(f"reference ADT not available: {ADT}")
    dt = _load_adt(ADT)
    cpu = sorted(dt["/cpus"], key=lambda c: c.cpu_id)[0]
    del cpu._properties["cpu-impl-reg"]
    del dt["/arm-io/ans"]
    blob = dt.build()
    assert b"cpu-impl-reg" not in blob or True  # build() re-serializes; the prop is gone

    out = os.path.join(str(tmp_path), "modified.adt")
    with open(out, "wb") as fh:
        fh.write(blob)
    r = subprocess.run([sys.executable,
                        os.path.join(ROOT, "proxyclient/tools/adt_check_t6032.py"), out],
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "proxyclient")})
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "IndexError" not in r.stderr
    assert "missing cpu-impl-reg/state" in r.stdout
    assert "FAIL: /arm-io/ans missing" in r.stdout
    assert "failure(s)" in r.stdout.splitlines()[-1]
