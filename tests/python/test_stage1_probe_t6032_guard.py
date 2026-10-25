#!/usr/bin/env python3
"""Board gate for stage1_probe_t6032: a fake J313 board makes zero risky calls,
and a transport failure on the right board still writes the report and sums."""

import importlib.util
import os
import sys
import types

import pytest

ROOT = os.environ.get("PROBE_REPO_ROOT",
                      os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class FakeDT:
    def __init__(self, model="Mac15,14", target="J575d",
                 compat=("J575dAP", "Mac15,14", "AppleARM"), with_gfx=True):
        import struct
        self.model = model
        self.target_type = target
        self.compatible = list(compat)
        segs = struct.pack("<4Q", 0x1000, 0xFFFFFC0000000000, 0x1000, 0x100060000)
        self._gfx = types.SimpleNamespace(_properties={"segment-ranges": segs} if with_gfx else {})
        sgx = types.SimpleNamespace()
        sgx.get_reg = lambda i: (0x408000000, 0x3fdc000)
        self._sgx = sgx

    def __getitem__(self, key):
        return {"/arm-io/gfx-asc": self._gfx, "/arm-io/sgx": self._sgx}[key]


@pytest.fixture()
def tool(monkeypatch):
    if not ROOT or not os.path.isdir(os.path.join(ROOT, "proxyclient")):
        pytest.skip("PROBE_REPO_ROOT not set")
    sys.path.insert(0, os.path.join(ROOT, "proxyclient"))
    from m1n1 import proxy as real_proxy
    from m1n1 import proxyutils as real_proxyutils

    calls = {"readmem": 0, "read32": 0, "power": 0, "guard": 0}

    class FakeIface:
        def readmem(self, addr, size):
            calls["readmem"] += 1
            raise RuntimeError("simulated UartRemoteError")

    class FakeProxy:
        def __init__(self, iface, debug=False):
            self.iface = iface

        def set_exc_guard(self, g):
            calls["guard"] += 1

        def get_exc_count(self):
            return 0

        def read32(self, addr):
            calls["read32"] += 1
            return 0x07040000

        def pmgr_adt_power_enable(self, path):
            calls["power"] += 1
            return 0

    monkeypatch.setattr(real_proxy, "GUARD", types.SimpleNamespace(MARK=2, SILENT=0x100, OFF=0))
    monkeypatch.setattr(real_proxy, "UartInterface", FakeIface)
    monkeypatch.setattr(real_proxy, "M1N1Proxy", FakeProxy)
    monkeypatch.setattr(real_proxyutils, "bootstrap_port", lambda iface, p: None)

    spec = importlib.util.spec_from_file_location(
        "stage1_probe_t6032_under_test",
        os.path.join(ROOT, "proxyclient", "tools", "stage1_probe_t6032.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ctx = types.SimpleNamespace(calls=calls, mod=mod, real_proxyutils=real_proxyutils)
    yield ctx


def run_tool(tool, monkeypatch, tmp_path, proxyutils_cls):
    tool.real_proxyutils.ProxyUtils = proxyutils_cls
    monkeypatch.setattr(sys, "argv", ["stage1_probe_t6032.py", str(tmp_path)])
    rc = tool.mod.main()
    report = open(os.path.join(str(tmp_path), "probe.txt")).read()
    sums = os.path.exists(os.path.join(str(tmp_path), "SHA256SUMS"))
    return rc, report, sums


def test_wrong_board_makes_no_risky_calls(tool, monkeypatch, tmp_path):
    class PU:
        def __init__(self, p):
            self.adt = FakeDT(model="Mac14,2", target="J313",
                              compat=("J313AP", "Mac14,2", "AppleARM"), with_gfx=False)
            self.base = 0x100000000

        def get_adt(self):
            return b"fake-adt" 

    rc, report, sums = run_tool(tool, monkeypatch, tmp_path, PU)
    assert rc == 1
    assert "refusing" in report
    assert sums
    assert tool.calls == {"readmem": 0, "read32": 0, "power": 0, "guard": 0}


def test_right_board_readmem_failure_still_writes_report(tool, monkeypatch, tmp_path):
    class PU:
        def __init__(self, p):
            self.adt = FakeDT()
            self.base = 0x100000000

        def get_adt(self):
            return b"fake-adt" 

    rc, report, sums = run_tool(tool, monkeypatch, tmp_path, PU)
    assert rc in (0, None)
    assert "not readable" in report
    assert "first16" not in report and "last16" not in report
    assert sums
    assert tool.calls["readmem"] == 1
    assert tool.calls["read32"] == 8
    assert tool.calls["power"] == 0
