#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Host harness builder/driver for the T6032 on-screen probe.

Compiles the real device sources (src/fb.c, src/probe_t6032.c,
src/probe_record.c, src/sha256.c) against thin shims, links them with the
real tinf/minilzlib, and drives:

  - SHA-256 vectors against hashlib (including the GKTS-zeroed digests)
  - the board gate: a mismatched board makes ZERO hardware-access calls
  - a full probe run on a fake J575dAP with a synthetic firmware image,
    its base32 record decoded back through tools/decode_probe_t6032.py
  - the shareable-output leakage test (no raw firmware bytes anywhere)
  - 1080p/4K previews of the DEMO screen and the refusal screen (PNG)

Run: python3 tests/run_probe_t6032_host.py [--fast]
"""

import hashlib
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

SHIMS = {
    "types.h": r"""
#ifndef HOST_SHIM_TYPES_H
#define HOST_SHIM_TYPES_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32; typedef uint64_t u64;
typedef int8_t s8; typedef int16_t s16; typedef int32_t s32; typedef int64_t s64;
typedef __uint128_t u128;
#ifndef NULL
#define NULL ((void *)0)
#endif
#define ALIGN_UP(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define ALIGN_DOWN(x, a) ((x) & ~((a) - 1))
#endif
""",
    "string.h": r"""
#ifndef HOST_SHIM_STRING_H
#define HOST_SHIM_STRING_H
#include_next <string.h>
#endif
""",
    "memory.h": r"""
#ifndef HOST_SHIM_MEMORY_H
#define HOST_SHIM_MEMORY_H
#include <stdint.h>
#include <stdbool.h>
#define MAIR_IDX_NORMAL_NC 4
#define PERM_RW 2
int mmu_add_mapping(uint64_t from, uint64_t to, size_t size, int idx, int perm);
#endif
""",
    "iodev.h": r"""
#ifndef HOST_SHIM_IODEV_H
#define HOST_SHIM_IODEV_H
#include <stddef.h>
#include <stdbool.h>
typedef int iodev_usage_t;
#define USAGE_CONSOLE (1u << 0)
typedef int iodev_id_t;
#define IODEV_FB 0
typedef struct { int lock; } spinlock_t;
#define SPINLOCK_INIT {0}
void iodev_console_rewind(iodev_id_t id);
struct iodev_ops {
    bool (*can_write)(void *opaque);
    ssize_t (*write)(void *opaque, const void *buf, size_t len);
    ssize_t (*read)(void *opaque, void *buf, size_t len);
    void (*handle_events)(void *opaque);
};
struct iodev {
    const struct iodev_ops *ops;
    spinlock_t lock;
    iodev_usage_t usage;
    void *opaque;
};
void iodev_console_kick(void);
#endif
""",
    "xnuboot.h": r"""
#ifndef HOST_SHIM_XNUBOOT_H
#define HOST_SHIM_XNUBOOT_H
#include <stdint.h>
struct boot_args {
    uint64_t pad[6];
    struct {
        uint64_t base;
        uint32_t stride, width, height, depth;
    } video;
};
extern struct boot_args cur_boot_args;
#endif
""",
    "malloc.h": r"""
#ifndef HOST_SHIM_MALLOC_H
#define HOST_SHIM_MALLOC_H
#include <stdlib.h>
#endif
""",
    "payload.h": r"""
#ifndef HOST_SHIM_PAYLOAD_H
#define HOST_SHIM_PAYLOAD_H
#include <stdint.h>
int payload_logo(void *a, void *b);
#endif
""",
    "soc.h": r"""
#ifndef HOST_SHIM_SOC_H
#define HOST_SHIM_SOC_H
#define T8012 0x8012
#define T8152 0x8152
#define T6032 0x6032
#endif
""",
    "adt.h": r"""
#ifndef HOST_SHIM_ADT_H
#define HOST_SHIM_ADT_H
#include <stdint.h>
extern void *adt;
const void *adt_getprop(const void *adt, int node, const char *name, uint32_t *len);
int adt_path_offset(const void *adt, const char *path);
int adt_path_offset_trace(const void *adt, const char *path, int *offsets);
int adt_get_reg(const void *adt, int *path, const char *prop, int idx, uint64_t *addr,
                uint64_t *size);
#endif
""",
    "exception.h": r"""
#ifndef HOST_SHIM_EXCEPTION_H
#define HOST_SHIM_EXCEPTION_H
#include <stdint.h>
enum exc_guard_t { GUARD_OFF = 0, GUARD_SKIP, GUARD_MARK, GUARD_RETURN, GUARD_SILENT = 0x100 };
extern volatile enum exc_guard_t exc_guard;
extern volatile int exc_count;
#endif
""",
    "pmgr.h": r"""
#ifndef HOST_SHIM_PMGR_H
#define HOST_SHIM_PMGR_H
int pmgr_adt_power_enable(const char *path);
#endif
""",
    "build_cfg.h": "#define BUILTIN_LOGO bootlogo\n",
    "build_tag.h": '#define BUILD_TAG "host-test"\n',
    "utils_host.h": r"""
#ifndef HOST_SHIM_UTILS_H
#define HOST_SHIM_UTILS_H
#include <stddef.h>
#include <stdint.h>
#include "soc.h"
#define min(a, b) (((a) < (b)) ? (a) : (b))
#define max(a, b) (((a) > (b)) ? (a) : (b))
extern uint32_t board_id, chip_id;
int printf(const char *fmt, ...);
int snprintf(char *str, size_t size, const char *fmt, ...);
#endif
""",
    "probe_host_shim.h": r"""
/* host build: route the weak device body's MMIO read to a plain function */
uint32_t host_read32(unsigned long addr);
#define read32(a) host_read32((unsigned long)(a))
""",
}

HARNESS_C = r"""
/* Host harness for the T6032 on-screen probe: mocks every hardware funnel,
 * counts the calls, captures printf, renders into malloc'd framebuffers. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "types.h"
#include "exception.h"
#include "fb.h"
#include "probe_t6032.h"
#include "probe_record.h"
#include "sha256.h"
#include "soc.h"
#include "xnuboot.h"

uint32_t board_id, chip_id;
struct boot_args cur_boot_args;

/* counted hardware funnels (strong: override the weak device bodies) */
long count_mmio, count_mem, count_power;
int fault_first_read;
uint64_t sgx_base_fake = 0x408000000ull;
uint32_t sgx_words_fake[8];
unsigned char *fw_image;
uint32_t fw_size;

const char *fake_compat = "J575dAP\0Mac15,14\0AppleARM\0";
const char *fake_target = "J575d";
const char *fake_model = "Mac15,14";

/* required-by-link device stubs (never called: the strong funnels above win) */
void *adt;
volatile enum exc_guard_t exc_guard = GUARD_OFF;
volatile int exc_count;
const void *adt_getprop(const void *a, int node, const char *name, uint32_t *len) { return NULL; }
int adt_path_offset(const void *a, const char *path) { return -1; }
int adt_path_offset_trace(const void *a, const char *path, int *off) { return -1; }
int adt_get_reg(const void *a, int *path, const char *prop, int idx, uint64_t *addr, uint64_t *sz) {
    return -1;
}
int pmgr_adt_power_enable(const char *path) { return -1; }
int mmu_add_mapping(uint64_t f, uint64_t t, size_t size, int idx, int perm) { return 0; }
void iodev_console_kick(void) {}
void iodev_console_rewind(int id) { (void)id; }
int payload_logo(void *a, void *b) { return 0; }
void *memset64(void *dst, uint64_t value, size_t size) {
    unsigned char *p = dst;
    for (size_t i = 0; i + 8 <= size; i += 8) {
        p[i] = value; p[i+1] = value >> 8; p[i+2] = value >> 16; p[i+3] = value >> 24;
        p[i+4] = value >> 32; p[i+5] = value >> 40; p[i+6] = value >> 48; p[i+7] = value >> 56;
    }
    return dst;
}
uint32_t host_read32(unsigned long addr) { exc_count++; return 0; }
void memcpy128(void *dst, void *src, size_t size) { memcpy(dst, src, size); }
void memcpy32(void *dst, void *src, size_t size) { memcpy(dst, src, size); }
void memset32(void *dst, uint32_t value, size_t size) {
    unsigned char *p = dst;
    for (size_t i = 0; i + 4 <= size; i += 4) {
        p[i] = value; p[i + 1] = value >> 8; p[i + 2] = value >> 16; p[i + 3] = value >> 24;
    }
}

/* printf capture: the real utils.h maps printf to debug_printf */
static char *report;
static size_t report_len, report_cap;
static void report_append(const char *buf, int n) {
    if (report_len + n + 1 > report_cap) {
        report_cap = (report_len + n + 1) * 2;
        report = realloc(report, report_cap);
    }
    memcpy(report + report_len, buf, n + 1);
    report_len += n;
    fputs(buf, stdout);
}
int printf(const char *fmt, ...) {
    char buf[4096];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    report_append(buf, n);
    return n;
}
int snprintf(char *str, size_t size, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(str, size, fmt, ap);
    va_end(ap);
    return n;
}
int debug_printf(const char *fmt, ...) {
    char buf[4096];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    report_append(buf, n);
    return n;
}

/* strong mock accessors: the counted funnels */
static uint64_t seg_phys[3], seg_size[3];

const void *probe_adt_prop(int node, const char *name, uint32_t *len) {
    if (node == 0) {
        if (!strcmp(name, "compatible")) {
            *len = strlen(fake_compat) + 1;
            return fake_compat;
        }
        if (!strcmp(name, "target-type")) {
            *len = strlen(fake_target) + 1;
            return fake_target;
        }
        if (!strcmp(name, "model")) {
            *len = strlen(fake_model) + 1;
            return fake_model;
        }
        return NULL;
    }
    if (node == 10 && !strcmp(name, "segment-ranges")) {
        static uint8_t segs[3 * 32];
        for (int i = 0; i < 3; i++) {
            memcpy(segs + 32 * i, &seg_phys[i], 8);
            memcpy(segs + 32 * i + 24, &seg_size[i], 8);
        }
        *len = sizeof(segs);
        return segs;
    }
    return NULL;
}
int probe_adt_path(const char *path) { return !strcmp(path, "/arm-io/gfx-asc") ? 10 : -1; }
const void *adt_gfx_prop(const char *name, uint32_t *len) { (void)name; (void)len; return NULL; }
int probe_sgx_base(uint64_t *base) { *base = sgx_base_fake; return 0; }
uint32_t probe_mmio_read32(uint64_t addr) {
    count_mmio++;
    if (fault_first_read && addr == sgx_base_fake + 0xd04000) {
        exc_count++;
        return 0xbad0bad0;
    }
    switch (addr - sgx_base_fake) {
    case 0xd04000: return sgx_words_fake[0];
    case 0xd04010: return sgx_words_fake[1];
    case 0xd04014: return sgx_words_fake[2];
    case 0xd04018: return sgx_words_fake[3];
    case 0xd0401c: return sgx_words_fake[4];
    case 0xe01500: return sgx_words_fake[5];
    case 0xe01504: return sgx_words_fake[6];
    case 0xe01508: return sgx_words_fake[7];
    }
    exc_count++;
    return 0;
}
int probe_mem_read(uint64_t addr, void *dst, uint32_t len) {
    count_mem++;
    if (addr != (uint64_t)(uintptr_t)fw_image || len > fw_size)
        return -1;
    memcpy(dst, fw_image, len);
    return 0;
}
int probe_power_enable(const char *path) { count_power++; return 0; }

/* segment-ranges for the fake gfx-asc node are served by probe_adt_prop
 * above (node 10); the driver points entry 0 at the loaded image. */

static void write_ppm(const char *path) {
    FILE *f = fopen(path, "wb");
    fprintf(f, "P6\n%u %u\n255\n", fb.width, fb.height);
    for (uint32_t i = 0; i < (uint32_t)fb.width * fb.height; i++) {
        uint32_t p = ((uint32_t *)fb.ptr)[i];
        fputc(p >> 16, f); fputc(p >> 8, f); fputc(p, f);
    }
    fclose(f);
}

static void fb_setup(uint32_t w, uint32_t h) {
    chip_id = T6032;
    memset(&cur_boot_args, 0, sizeof(cur_boot_args));
    cur_boot_args.video.width = w;
    cur_boot_args.video.height = h;
    cur_boot_args.video.stride = w * 4;
    cur_boot_args.video.depth = 32;
    cur_boot_args.video.base = (uint64_t)(uintptr_t)calloc(h, w * 4);
    fb_init(false);
}

int main(int argc, char **argv) {
    if (argc < 2)
        return 2;
    if (!strcmp(argv[1], "sha256")) {
        FILE *f = fopen(argv[2], "rb");
        static unsigned char buf[16 << 20];
        size_t n = fread(buf, 1, sizeof(buf), f);
        u8 out[32];
        sha256(buf, n, out);
        for (int i = 0; i < 32; i++) printf("%02x", out[i]);
        printf("\n");
        return 0;
    }
    if (!strcmp(argv[1], "tagzero")) {
        uint32_t pos = strtoul(argv[3], NULL, 0);
        FILE *f = fopen(argv[2], "rb");
        static unsigned char buf[16 << 20];
        size_t n = fread(buf, 1, sizeof(buf), f);
        memset(buf + pos + 4, 0, 8);
        u8 out[32];
        sha256(buf, n, out);
        for (int i = 0; i < 32; i++) printf("%02x", out[i]);
        printf("\n");
        return 0;
    }
    if (!strcmp(argv[1], "probe")) {
        /* probe right|wrong fault|nofault <fw-file> <w0>..<w7>
         * One self-contained process: loads the image, points the fake
         * segment-ranges at it, runs the real probe, prints CALLS. */
        int right = !strcmp(argv[2], "right");
        fake_compat = right ? "J575dAP\0Mac15,14\0AppleARM\0" : "J313AP\0Mac14,2\0AppleARM\0";
        fake_target = right ? "J575d" : "J313";
        fake_model = right ? "Mac15,14" : "Mac14,2";
        fault_first_read = !strcmp(argv[3], "fault");
        FILE *f = fopen(argv[4], "rb");
        if (!f)
            return 3;
        fseek(f, 0, SEEK_END);
        fw_size = ftell(f);
        fseek(f, 0, SEEK_SET);
        fw_image = malloc(fw_size);
        if (fread(fw_image, 1, fw_size, f) != fw_size)
            return 3;
        fclose(f);
        seg_phys[0] = (uint64_t)(uintptr_t)fw_image;
        seg_size[0] = fw_size;
        seg_phys[1] = 0x200000abcull;
        seg_size[1] = 0x1000;
        seg_phys[2] = 0x200001abcull;
        seg_size[2] = 0x800;
        for (int i = 0; i < 8; i++)
            sgx_words_fake[i] = strtoul(argv[5 + i], NULL, 0);
        chip_id = T6032;
        probe_t6032_run();
        printf("CALLS mmio=%ld mem=%ld power=%ld\n", count_mmio, count_mem, count_power);
        return 0;
    }
    if (!strcmp(argv[1], "demo")) {
        fb_setup(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0));
        probe_fb_demo_run();
        write_ppm(argv[4]);
        return 0;
    }
    if (!strcmp(argv[1], "refused")) {
        fb_setup(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0));
        fake_compat = "J313AP\0Mac14,2\0AppleARM\0";
        fake_target = "J313";
        fake_model = "Mac14,2";
        probe_t6032_run();
        write_ppm(argv[4]);
        return 0;
    }
    if (!strcmp(argv[1], "retina")) {
        /* 16x32 console font: iBoot flagged the display retina. */
        chip_id = T6032;
        memset(&cur_boot_args, 0, sizeof(cur_boot_args));
        uint32_t w = strtoul(argv[2], NULL, 0), h = strtoul(argv[3], NULL, 0);
        cur_boot_args.video.width = w;
        cur_boot_args.video.height = h;
        cur_boot_args.video.stride = w * 4;
        cur_boot_args.video.depth = 32 | 0x10000;
        cur_boot_args.video.base = (uint64_t)(uintptr_t)calloc(h, w * 4);
        fb_init(false);
        probe_fb_demo_run();
        write_ppm(argv[4]);
        return 0;
    }
    if (!strcmp(argv[1], "console")) {
        /* boot-log lines, the demo render, then trailing console lines:
         * with fb_set_active(false) after the render the tail must not tear
         * the probe block, so the band is identical for any line count. */
        fb_setup(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0));
        fb_set_active(true);
        char l[128];
        int n = atoi(argv[5]);
        for (int i = 0; i < n; i++) {
            int k = snprintf(l, sizeof l, "boot log line %d\n", i);
            fb_console_write(l, k);
        }
        probe_fb_demo_run();
        const char *t = "probe: done\nRunning proxy...\nline3\n";
        fb_console_write(t, strlen(t));
        write_ppm(argv[4]);
        return 0;
    }
    return 2;
}
"""

FONT_C = r"""
/* generated: console font + logo blobs, same bytes the device links */
#include <stdint.h>
"""


def build_harness(work: pathlib.Path) -> pathlib.Path:
    shim = work / "shims"
    shim.mkdir(exist_ok=True)
    for name, body in SHIMS.items():
        (shim / name).write_text(body)
    # fb.c is compiled from work/src-h and includes ../build/build_cfg.h
    (work / "build").mkdir(exist_ok=True)
    (work / "build" / "build_cfg.h").write_text(SHIMS["build_cfg.h"])

    # font + retina font + gz logo blobs, exactly the device symbols
    gen = work / "font_bytes.c"
    gen.write_text(FONT_C)
    blobs = [
        ("_binary_build_font_bin_start", "_binary_build_font_bin_end", ROOT / "font/font.bin"),
        ("_binary_build_font_retina_bin_start", "_binary_build_font_retina_bin_end",
         ROOT / "font/font_retina.bin"),
    ]
    for size in (48, 128, 256):
        raw = (ROOT / f"data/bootlogo_{size}.bin").read_bytes()
        gz = gzip_bytes(raw)
        p = work / f"logo_{size}.bin.gz"
        p.write_bytes(gz)
        blobs.append((f"_binary_build_bootlogo_{size}_bin_gz_start",
                      f"_binary_build_bootlogo_{size}_bin_gz_end", p))
    decls, defs = [], []
    for start, end, path in blobs:
        data = path.read_bytes()
        arr = ",".join(str(b) for b in data)
        decls.append(f'extern const unsigned char {start}[]; extern const unsigned char {end}[];')
        # end = start + N via an assembler .set: a second array's address is
        # not ordered by the linker, which broke gz_len once already.
        defs.append(f'const unsigned char {start}[] = {{{arr}}};\n'
                    f'extern const unsigned char {end}[];\n'
                    f'__asm__(".globl {end}\\n.set {end}, {start} + {len(data)}\\n");')
    gen.write_text(FONT_C + "\n".join(decls) + "\n" + "\n".join(defs))

    (work / "harness.c").write_text(HARNESS_C)

    (work / "preinc.h").write_text(
        "#include <assert.h>\n"
        "#ifndef static_assert\n#define static_assert _Static_assert\n#endif\n")
    # The C sources live in src/ next to the real (aarch64-asm) utils.h; a
    # quote-include would always win from their own directory. Compile patched
    # copies: same bytes, only the utils.h include redirected to the shim.
    hostsrc = work / "src-h"
    hostsrc.mkdir(exist_ok=True)
    srcs = []
    for name in ("fb.c", "probe_t6032.c", "probe_record.c", "sha256.c"):
        text = (ROOT / "src" / name).read_text()
        text = text.replace('#include "utils.h"', '#include "utils_host.h"')
        p = hostsrc / name
        p.write_text(text)
        srcs.append(str(p))
    srcs += [str(work / "harness.c"), str(work / "font_bytes.c")]
    srcs += [str(p) for p in (ROOT / "src/tinf").glob("*.c")]
    srcs += [str(p) for p in (ROOT / "src/minilzlib").glob("*.c")]

    exe = work / "harness"
    cc = subprocess.run(
        ["cc", "-O2", "-g", "-DPROBE_HOST=1", "-D_GNU_SOURCE",
         "-include", str(work / "preinc.h"),
         f"-I{shim}", f"-I{ROOT / 'src'}", "-o", str(exe)] + srcs,
        capture_output=True, text=True)
    if cc.returncode:
        raise RuntimeError("harness build failed:\n" + cc.stdout[-4000:] + cc.stderr[-4000:])
    return exe


def gzip_bytes(raw: bytes) -> bytes:
    import gzip as _gzip
    import io
    buf = io.BytesIO()
    with _gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as f:
        f.write(raw)
    return buf.getvalue()


def run(exe, *args, check=True):
    r = subprocess.run([str(exe)] + [str(a) for a in args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"harness {args} failed rc={r.returncode}\n{r.stderr[-2000:]}")
    return r


def main() -> int:
    import importlib.util
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location(
        "decode_probe_t6032", ROOT / "tools/decode_probe_t6032.py")
    dec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dec)

    work = pathlib.Path(tempfile.mkdtemp(prefix="probe-host-"))
    exe = build_harness(work)
    print("harness built")

    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    # 1. SHA-256 vs hashlib
    vectors = [b"", b"abc", b"a" * 1000, bytes(range(256)) * 4097]
    for i, v in enumerate(vectors):
        p = work / f"v{i}.bin"
        p.write_bytes(v)
        got = run(exe, "sha256", p).stdout.strip()
        check(f"sha256 vector {i}", got == hashlib.sha256(v).hexdigest())

    # synthetic firmware image with distinctive raw bytes + GKTS tags
    fw = bytearray(0x100000)
    for i in range(0, len(fw), 4):
        fw[i:i + 4] = struct.pack("<I", 0x10000000 + i)
    marker = b"RAWWGTSPAYLOAD-BYTES-0123456"
    fw[0x100:0x100 + len(marker)] = marker
    for off in (0x1000, 0x20000, 0x80000):
        fw[off:off + 4] = b"GKTS"
        fw[off + 4:off + 12] = b"TAGDATAX"
    fw_path = work / "fw.bin"
    fw_path.write_bytes(fw)
    fw_sha = hashlib.sha256(fw).hexdigest()

    # 2. tag-zeroed digests
    got = run(exe, "tagzero", fw_path, hex(0x1000)).stdout.strip()
    zeroed = bytearray(fw)
    zeroed[0x1004:0x100c] = bytes(8)
    check("tagzero digest", got == hashlib.sha256(bytes(zeroed)).hexdigest())

    # 3. board gate: wrong board -> zero hardware calls
    words = ["0x07040000", "0x0002040a", "0x0a040014", "0x11111111",
             "0x22222222", "0x33333333", "0x44444444", "0x55555555"]
    g = run(exe, "probe", "wrong", "nofault", fw_path, *words)
    check("wrong board refused", "refusing" in g.stdout)
    check("wrong board zero hw calls", "CALLS mmio=0 mem=0 power=0" in g.stdout)

    # 4. right board, no fault: probe runs, calls happen, power untouched
    g = run(exe, "probe", "right", "nofault", fw_path, *words)
    check("right board calls hw", "CALLS mmio=8 mem=1 power=0" in g.stdout)

    # 5. right board, first read faults: one power enable, 9 mmio reads
    g = run(exe, "probe", "right", "fault", fw_path, *words)
    check("fault path powers sgx once", "CALLS mmio=9 mem=1 power=1" in g.stdout)
    check("fault path announces pmgr", "powering domain via pmgr" in g.stdout)

    # 6. full record round trip through the python decoder
    r = run(exe, "probe", "right", "nofault", fw_path, *words)
    report = r.stdout
    b32 = "".join(l for l in report.splitlines()
                  if l and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in l)
                  and not l.startswith("probe:") and not l.startswith("CALLS")
                  and len(l) > 40)
    rec = dec.decode_text(b32)
    import base64

    check("b32 matches stdlib",
          base64.b32encode(dec.decode_base32(b32)).decode().rstrip("=") == b32)
    check("record magic/crc", rec["version"] == 1)
    check("record fields", rec["model"] == "Mac15,14" and rec["target"] == "J575d"
          and rec["cores_per_cluster"] == 10 and rec["clusters_per_die"] == 4 and rec["dies"] == 2
          and rec["text_size"] == len(fw) and rec["nonzero_count"] == sum(1 for b in fw if b))
    check("record sha256", rec["text_sha256"] == fw_sha)
    check("record tags", rec["tag_count"] == 3 and [t["offset"] for t in rec["tags"]]
          == [0x1000, 0x20000, 0x80000])
    tag_expect = []
    for off in (0x1000, 0x20000, 0x80000):
        z = bytearray(fw)
        z[off + 4:off + 12] = bytes(8)
        tag_expect.append(hashlib.sha256(bytes(z)).hexdigest())
    check("record tag digests", [t["sha256"] for t in rec["tags"]] == tag_expect)
    check("record sgx words", rec["sgx_words"]["d04000"] == 0x07040000
          and rec["sgx_words"]["e01508"] == 0x55555555)
    check("console has facts", "probe: model=Mac15,14 target=J575d" in report
          and "T6032 PROBE v1" in report and "probe: done" in report)

    # 7. leakage: no raw firmware bytes in any shareable output
    shareable = report + b32
    leaks = marker in shareable.encode() or marker.hex() in shareable \
        or "TAGDATAX" in shareable or bytes(fw[0x2000:0x2016]).hex() in shareable
    check("no raw firmware bytes shareable", not leaks)
    check("no first16/last16 in report", fw[:16].hex() not in shareable
          and fw[-16:].hex() not in shareable)

    # 8. decoder negative cases
    try:
        dec.decode_text(b32[:-8] + "B" * 8)
        check("decoder rejects corrupt block", False)
    except ValueError:
        check("decoder rejects corrupt block", True)

    # 9. previews: DEMO + refusal at 1080p and 4K
    out = pathlib.Path(os.environ.get("PROBE_OUT", ROOT.parent))
    for w, h, tag in ((1920, 1080, "1080p"), (3840, 2160, "4k")):
        ppm = work / f"demo-{tag}.ppm"
        run(exe, "demo", w, h, ppm)
        ppm2 = work / f"refused-{tag}.ppm"
        run(exe, "refused", w, h, ppm2)
        try:
            from PIL import Image
            Image.open(ppm).save(out / f"onscreen-preview-{tag}.png")
            Image.open(ppm2).save(out / f"onscreen-preview-refused-{tag}.png")
        except ImportError:
            (out / f"onscreen-preview-{tag}.ppm").write_bytes(ppm.read_bytes())
    check("previews rendered", True)

    # 10. retina console font + console-tail regression (fb_set_active(false))
    def band(path, frac=5):  # bottom fifth: the probe block area
        data = pathlib.Path(path).read_bytes()
        magic, dims, maxv = data.split(b"\n", 2)[:3]
        w, h = map(int, dims.split())
        px = data[data.index(b"255\n") + 4:]
        rows = h - h // frac
        return px[rows * w * 3:]

    ret = work / "retina-1080.ppm"
    run(exe, "retina", 1920, 1080, ret)
    check("retina render works", ret.stat().st_size > 1920 * 1080 * 3 // 4)
    try:
        from PIL import Image
        Image.open(ret).save(out / "onscreen-preview-retina-1080p.png")
    except ImportError:
        pass
    base = work / "demo-1080p.ppm"
    c30 = work / "console-30.ppm"
    c100 = work / "console-100.ppm"
    run(exe, "console", 1920, 1080, c30, 30)
    run(exe, "console", 1920, 1080, c100, 100)
    check("console tail does not tear probe band",
          band(c30) == band(c100) == band(base))

    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
