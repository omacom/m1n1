#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Stage 1 probe for the Mac Studio (M3 Ultra, T6032): read values that only a running loader can read.

Usage: stage1_probe_t6032.py OUTDIR [--power-gpu]
       stage1_probe_t6032.py --selftest

It writes no Mac register and no Mac RAM, apart from the loader's own bookkeeping and the UART baud
rate on a UART link. One option does write hardware: --power-gpu turns on the GPU power domain through
the loader's pmgr call so the hardware-id words can be read; that setting is lost at the next restart.
Reads of the hardware-id words and of the GPU firmware image are guarded: a bus fault is counted and
reported, it does not stop the probe. It never writes a GPU register and never starts firmware.

The live board is validated first: model Mac15,14, target J575d, root compatible containing
J575dAP. Anything else is refused before the firmware read, the SGX reads and --power-gpu.

Output, under OUTDIR:
  probe.txt, SHA256SUMS   the report and its hash: SEND THESE TWO ONLY. probe.txt carries no raw
                          firmware bytes: sizes, counts and digests only.
  private/                adt.bin, adt.txt (the whole device tree: serial, hardware UUID, chip IDs,
                          MACs), gpu-text.bin (the GPU firmware image), gpu-text-meta.txt (first16 /
                          last16 of that image) and their hashes. DO NOT SEND.
"""
import hashlib
import os
import struct
import sys

# sgx offsets. 0xd04000/10/14 and the core mask 0xe01500 (3 words) are the ones src/kboot_gpu.c reads
# (G15_SGX_ID_VERSION, ID_COUNTS_1, ID_COUNTS_2, CORE_MASK). 0xd04018/0xd0401c are NOT read by m1n1: they
# come from the macOS kext reading in the layout notes (section 7); the probe reads them to test that reading.
HWID = (0xD04000, 0xD04010, 0xD04014, 0xD04018, 0xD0401C, 0xE01500, 0xE01504, 0xE01508)
TAG = b"GKTS"


def decode_hwid(w):
    """Words at sgx+0xd04000 and +0xd04010 -> readable facts.
    ID_COUNTS_1 fields as src/kboot_gpu.c documents them: [7:0] cores per cluster, [15:8] clusters per die,
    [19:16] dies. The family/variant split of the first word is the kext reading (INFERENCE until measured)."""
    v, c = w[0], w[1]
    return {
        "family": v >> 24,
        "variant": (v >> 16) & 0xFF,
        "cores_per_cluster": c & 0xFF,
        "clusters_per_die": (c >> 8) & 0xFF,
        "dies": (c >> 16) & 0xF,
    }


def text_digests(text):
    """sha256 of the image, and of the image with the 8 bytes after each 'GKTS' tag zeroed (one line per tag)."""
    out = [("raw", hashlib.sha256(text).hexdigest())]
    pos = text.find(TAG)
    while pos >= 0:
        b = bytearray(text)
        b[pos + len(TAG):pos + len(TAG) + 8] = bytes(8)
        out.append((f"tag@{pos:#x} zeroed", hashlib.sha256(bytes(b)).hexdigest()))
        pos = text.find(TAG, pos + 1)
    return out


def segments(raw):
    """gfx-asc 'segment-ranges': 32-byte entries (phys, iova, phys, flags<<32 | size)."""
    return [struct.unpack_from("<4Q", raw, o) for o in range(0, len(raw) - 31, 32)]


def selftest():
    assert decode_hwid([0x07040000, 0x0002040A]) == {
        "family": 7, "variant": 4, "cores_per_cluster": 10, "clusters_per_die": 4, "dies": 2}
    raw = struct.pack("<4Q", 0x1000, 0xFFFFFC0000000000, 0x1000, 0x100060000)
    assert segments(raw)[0][3] & 0xFFFFFFFF == 0x60000
    t = b"a" * 10 + TAG + bytes(range(1, 9)) + b"z"
    d = text_digests(t)
    assert len(d) == 2 and d[0][1] != d[1][1]
    assert d[1][1] == hashlib.sha256(b"a" * 10 + TAG + bytes(8) + b"z").hexdigest()
    print("selftest ok")


def write_sums(directory, names):
    """SHA256SUMS in `directory` for exactly `names` (no shell, no stale file)."""
    with open(os.path.join(directory, "SHA256SUMS"), "w") as fh:
        for n in names:
            with open(os.path.join(directory, n), "rb") as src:
                fh.write(f"{hashlib.sha256(src.read()).hexdigest()}  {n}\n")


def validate_board(dt):
    """The live board must be the Mac Studio (M3 Ultra): (ok, reason)."""
    model = str(getattr(dt, "model", ""))
    target = str(getattr(dt, "target_type", ""))
    compat = [str(c) for c in getattr(dt, "compatible", [])]
    if model != "Mac15,14":
        return False, f"model {model!r} != 'Mac15,14'"
    if target != "J575d":
        return False, f"target-type {target!r} != 'J575d'"
    if "J575dAP" not in compat:
        return False, f"root compatible {compat} lacks 'J575dAP'"
    return True, ""


def main():
    if "--selftest" in sys.argv:
        return selftest()
    out = sys.argv[1]
    priv = os.path.join(out, "private")
    os.makedirs(priv, exist_ok=True)
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from m1n1.proxy import GUARD, M1N1Proxy, UartInterface
    from m1n1.proxyutils import ProxyUtils, bootstrap_port

    # Not m1n1.setup: that module writes the PMU panic counter at import time.
    iface = UartInterface()
    p = M1N1Proxy(iface, debug=False)
    bootstrap_port(iface, p)
    u = ProxyUtils(p)
    rep = open(os.path.join(out, "probe.txt"), "w")

    def log(s):
        print(s)
        rep.write(s + "\n")
        rep.flush()

    def guarded(fn):
        """Run fn with exceptions guarded; return (value, faulted)."""
        p.set_exc_guard(GUARD.MARK | GUARD.SILENT)
        before = p.get_exc_count()
        try:
            v = fn()
        finally:
            faulted = p.get_exc_count() != before
            p.set_exc_guard(GUARD.OFF)
        return v, faulted

    log("probe: stage1_probe_t6032 v3")
    log(f"loader base: {u.base:#x}")
    adt_raw = u.get_adt()
    with open(os.path.join(priv, "adt.bin"), "wb") as fh:
        fh.write(adt_raw)
    dt = u.adt
    with open(os.path.join(priv, "adt.txt"), "w") as fh:
        fh.write(str(dt))
    log(f"model: {dt.model}  target: {dt.target_type}  adt bytes: {len(adt_raw)}")

    ok, why = validate_board(dt)
    if not ok:
        log(f"refusing: not a j575dap Mac Studio ({why}); no firmware or SGX reads were made")
        log("send probe.txt and SHA256SUMS only; never send the private folder")
        rep.close()
        write_sums(out, ["probe.txt"])
        return 1

    # 1. GPU firmware image iBoot loaded (a memory read, no GPU register involved)
    asc = dt["/arm-io/gfx-asc"]
    raw = asc._properties.get("segment-ranges")  # set by iBoot at run time; not in the IPSW tree
    if raw is None:
        log("gpu text: not read (gfx-asc has no segment-ranges property)")
    else:
        segs = segments(raw)
        log("gfx-asc segments (phys, iova, size): " + "; ".join(f"{s[0]:#x} {s[1]:#x} {s[3] & 0xFFFFFFFF:#x}" for s in segs))
        try:
            text, faulted = guarded(lambda: iface.readmem(segs[0][0], segs[0][3] & 0xFFFFFFFF))
        except Exception as e:  # UartRemoteError and any other transport failure
            log(f"gpu text: not readable ({type(e).__name__})")
            text, faulted = None, False
        if faulted or not text:
            log("gpu text: not readable (bus fault or empty read)")
        else:
            with open(os.path.join(priv, "gpu-text.bin"), "wb") as fh:
                fh.write(text)
            with open(os.path.join(priv, "gpu-text-meta.txt"), "w") as fh:
                fh.write(f"first16 {text[:16].hex()}\nlast16 {text[-16:].hex()}\n")
            log(f"gpu text: {len(text)} bytes, {sum(1 for b in text if b)} non-zero")
            for name, dg in text_digests(text):
                log(f"gpu text {name}: {dg}")

    # 2. hardware-id words: a bus fault here is reported, not fatal
    base = dt["/arm-io/sgx"].get_reg(0)[0]
    log(f"sgx base: {base:#x}")
    if "--power-gpu" in sys.argv:
        log("power-gpu: pmgr_adt_power_enable('/arm-io/sgx') (lost at restart)")
        try:
            log(f"  returned {p.pmgr_adt_power_enable('/arm-io/sgx')}")
        except Exception as e:
            log(f"  failed: {e}")
    words = []
    for off in HWID:
        v, faulted = guarded(lambda off=off: p.read32(base + off))
        words.append(None if faulted else v)
        log(f"sgx+{off:#x}: " + ("BUS FAULT" if faulted else f"{v:#010x}"))
    if None not in words[:2]:
        log(f"decoded (inference): {decode_hwid(words)}")
    else:
        log("hardware-id words not read; rerun with --power-gpu if that was not used")

    log("send probe.txt and SHA256SUMS only; never send the private folder")
    rep.close()
    private = [n for n in ("adt.bin", "adt.txt", "gpu-text.bin", "gpu-text-meta.txt")
               if os.path.exists(os.path.join(priv, n))]
    write_sums(priv, private)
    write_sums(out, ["probe.txt"])


if __name__ == "__main__":
    main()
