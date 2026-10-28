#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Decode the T6032 on-screen probe's base32 backup block.

Usage:
    decode_probe_t6032.py <file-with-the-base32-lines>   # or '-' for stdin
    decode_probe_t6032.py --selftest

The block is the last 3 small lines on the probe screen: RFC 4648 base32
(no padding, 'A' filler), whitespace/newlines between chunks are ignored.
The decoded record is verified (magic + CRC-32) and printed.

Record layout v1: see src/probe_record.h in m1n1. No raw firmware bytes are
carried: only sizes, counts, addresses and SHA-256 digests.
"""

import struct
import sys
import zlib

MAGIC = b"T6032PR1"
SGX_OFFSETS = (0xD04000, 0xD04010, 0xD04014, 0xD04018, 0xD0401C, 0xE01500, 0xE01504, 0xE01508)
PAY_LEN = 328  # per src/probe_record.h: 328 payload bytes, 344-byte record
ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
PAY_STATIC = struct.Struct("<IIQ24s16s32sBBBBIII32s")


def decode_base32(text):
    """RFC 4648 base32 (padding optional); whitespace ignored.

    The on-screen block has no padding and a possibly short final group (the
    C encoder emits only the characters that carry data), so decode chunk by
    chunk with a partial-group tail."""
    s = "".join(text.split()).upper().rstrip("=")
    if not s or any(c not in ALPHA for c in s):
        raise ValueError("not a base32 block")
    out = bytearray()
    for i in range(0, len(s), 8):
        chunk = s[i : i + 8]
        acc = 0
        for c in chunk:
            acc = (acc << 5) | ALPHA.index(c)
        nc = len(chunk)
        nb = (nc * 5) // 8
        if nb:
            val = acc << (40 - nc * 5)  # top-align to the full 40-bit group
            out += (val >> (40 - nb * 8)).to_bytes(nb, "big")
    return bytes(out)


def decode_record(rec):
    """Verify and unpack a record; returns a dict. Raises ValueError on any mismatch."""
    if len(rec) < 16 or rec[:8] != MAGIC:
        raise ValueError("bad magic")
    payload = rec[16:]
    (declared,) = struct.unpack_from("<I", rec, 12)
    if declared != len(payload) or len(payload) != PAY_LEN:
        raise ValueError(f"payload length mismatch ({declared} != {len(payload)})")
    (crc,) = struct.unpack_from("<I", rec, 8)
    if crc != zlib.crc32(payload):
        raise ValueError("CRC-32 mismatch")
    (version, flags, chip_id, model, target, sgx, cores, clusters, dies, faults, text_size,
     nonzero, tag_count, text_sha) = PAY_STATIC.unpack_from(payload, 0)
    if version != 1:
        raise ValueError(f"unsupported version {version}")
    tags = []
    for i in range(4):
        off, sha = struct.unpack_from("<I32s", payload, 136 + 36 * i)
        if i < tag_count:
            tags.append({"offset": off, "sha256": sha.hex()})
    segs = []
    for i in range(3):
        phys, size = struct.unpack_from("<QQ", payload, 280 + 16 * i)
        if phys or size:
            segs.append({"phys": phys, "size": size})
    return {
        "version": version,
        "powered_gpu": bool(flags & 1),
        "chip_id": chip_id,
        "model": model.rstrip(b"\0").decode("ascii"),
        "target": target.rstrip(b"\0").decode("ascii"),
        "sgx_words": {f"{SGX_OFFSETS[i]:06x}": None if v == 0xFFFFFFFF else v
                      for i, v in enumerate(struct.unpack("<8I", sgx))},
        "cores_per_cluster": cores,
        "clusters_per_die": clusters,
        "dies": dies,
        "fault_count": faults,
        "text_size": text_size,
        "nonzero_count": nonzero,
        "tag_count": tag_count,
        "text_sha256": text_sha.hex() if any(text_sha) else None,
        "tags": tags,
        "segments": segs,
    }


def decode_text(text):
    return decode_record(decode_base32(text))


def _dump(rec):
    d = decode_record(rec)
    for k, v in d.items():
        if k == "tags":
            for t in v:
                print(f"tag@{t['offset']:#x} zeroed sha256 {t['sha256']}")
        elif k == "segments":
            for i, s in enumerate(v):
                print(f"seg{i} phys={s['phys']:#x} size={s['size']:#x}")
        elif k == "sgx_words":
            for off, val in v.items():
                print(f"sgx+{off} = {'BUS FAULT' if val is None else f'{val:#010x}'}")
        else:
            print(f"{k}: {v}")


def main():
    if "--selftest" in sys.argv:
        # Corrupted-input handling: bad magic and bad CRC must both raise.
        rec = bytearray(16 + PAY_LEN)
        rec[:8] = MAGIC
        for mut in ((0, b"T6032PRX"), (8, b"\0\0\0\0")):
            bad = bytearray(rec)
            bad[mut[0] : mut[0] + len(mut[1])] = mut[1]
            try:
                decode_record(bytes(bad))
            except ValueError:
                pass
            else:
                raise AssertionError(f"corruption at {mut[0]} not detected")
        # stdlib round trip: padded and unpadded base32 decode identically
        import base64

        blob = bytes(range(256)) * 3
        std = base64.b32encode(blob).decode()
        assert decode_base32(std) == blob
        assert decode_base32(std.rstrip("=")) == blob
        print("selftest ok")
        return 0
    src = open(sys.argv[1]).read() if sys.argv[1] != "-" else sys.stdin.read()
    _dump(decode_base32(src))
    return 0


if __name__ == "__main__":
    sys.exit(main())
