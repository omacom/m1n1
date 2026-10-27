# SPDX-License-Identifier: MIT
"""An owner's chosen.<switch>=0 must keep the M3 graphics off, even when the payload scan loses it.

The real payload scan (check_var(), load_one_payload()), payload_run()'s chosen handling and its
transfer loop, kboot_set_chosen(), dt_set_m3_defaults() and the T8122 GPU selector run on the host
against libfdt, in a plain and a CHAINLOADING build. Only the loaders and the GPU setups behind the
selector are stubs. Each payload is laid out as update-m1n1 writes boot.bin: kernel, device tree,
then the config lines, then zeros (or a chainloading stage 1's variables, then zeros).
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAX_CHOSEN_VARS = 16

KERNEL = bytes(0x38) + b"ARM\x64" + bytes(4)
FDT = b"\xd0\x0d\xfe\xed" + (64).to_bytes(4, "big") + bytes(56)
AIR = ("asahi,t8122-dcp", "asahi,t8122-gpu", "asahi,t8122-gpu-power-standin",
       "asahi,t8122-gpu-fuse-leakage")
AIR_GPU = AIR[1:]
PRO = ("asahi,t6030-dcp", "asahi,t6030-gpu")


def between(source, start, end):
    a = source.index(start)
    return source[a:source.index(end, a)]


def function(source, signature):
    a = source.index(signature)
    return source[a:source.index("\n}\n", a) + 3]


def harness():
    payload = (ROOT / "src/payload.c").read_text()
    kboot = (ROOT / "src/kboot.c").read_text()
    gpu = (ROOT / "src/kboot_gpu.c").read_text()
    marker = ""
    if "chosen_lost_added" in payload:
        marker = between(payload, "    /* Last, so neither", "    if (chainload_spec && boot_spec) {")
    return r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define BUILD_TAG "host-test"

/* kboot.c */
#define MAX_CHOSEN_PARAMS 16
static char *chosen_params[MAX_CHOSEN_PARAMS][2];
static bool chosen_is_default[MAX_CHOSEN_PARAMS];
static bool chosen_lost;
static unsigned char blob[16384];
static void *dt = blob;
void kboot_delete_chosen(const char *name);
''' + function(kboot, "int kboot_set_chosen(const char *name, const char *value)\n{") + \
        function(kboot, "void kboot_delete_chosen(const char *name)\n{") + \
        between(kboot, "struct m3_default_board {", "/* The config value of chosen.") + \
        function(kboot, "static const char *kboot_chosen_param(const char *name)\n{") + \
        function(kboot, "static void dt_drop_m3_defaults(void)\n{") + \
        function(kboot, "static int dt_set_m3_defaults(void)\n{") + r'''
/* kboot_gpu.c: the T8122 selector, with its GPU setups recorded instead of run */
struct g15_soc { const char *optin; };
static const struct g15_soc g15_soc_t8122 = {"asahi,t8122-gpu"};
static const struct g15_soc g15_soc_t8122_standin = {"asahi,t8122-gpu"};
static const char *selected = "none";
static void t8122_leak_fuse_export(void *d) { (void)d; }
static int dt_set_gpu_t8122(void *d) { selected = "reservation-only"; return 0; }
static int dt_set_gpu_g15(void *d, const struct g15_soc *soc)
{
    selected = soc == &g15_soc_t8122_standin ? "g15-standin" : "g15";
    return 0;
}
''' + function(gpu, "static bool g15_optin(") + \
        function(gpu, "static int dt_set_gpu_t8122_select(void *dt)\n{") + r'''
/* payload.c, with its loaders reduced to finding the kernel and the device tree */
struct kernel_header;
static struct kernel_header *kernel;
static void *fdt;
static char *chainload_spec, *boot_spec;
static bool nvme_adopt_live_session;
static char display_value[64];
static void display_configure(const char *s) { snprintf(display_value, sizeof(display_value), "%s", s); }
static void mitigations_configure(const char *s) {}
static void *decompress_gz(void *p, size_t size) { return NULL; }
static void *decompress_xz(void *p, size_t size) { return NULL; }
static void *load_cpio(void *p, size_t size) { return NULL; }
static void *load_fdt(void *p, size_t size) { fdt = p; return (u8 *)p + fdt_totalsize(p); }
static void *load_kernel(void *p, size_t size) { kernel = p; return (u8 *)p + 64; }
''' + between(payload, "static const u8 gz_magic[]", "static char expect_compatible[256];") + \
        between(payload, "#define MAX_VAR_NAME 64", "static bool check_var(u8 **p)") + \
        function(payload, "static bool check_var(u8 **p)\n{") + \
        function(payload, "static void *load_one_payload(void *start, size_t size)\n{") + r'''
/* payload_run(): its chosen handling (absent before the fix) and its transfer loop */
static int chosen_handling(void)
{
''' + marker + r'''    return 0;
}
static void transfer(void)
{
''' + between(payload, "        for (size_t i = 0; i < chosen_cnt; i++) {",
                  "\n\n        if (kboot_prepare_dt(fdt))") + r'''
}

static u8 image[1 << 16];

int main(int argc, char **argv)
{
    /* argv: stage1|boot, payload file, root compatible list joined by ';', stage-1 output */
    assert(argc >= 4);
    FILE *f = fopen(argv[2], "rb");
    assert(f);
    size_t n = fread(image, 1, sizeof(image) - 4096, f);
    assert(!ferror(f) && n < sizeof(image) - 4096);
    fclose(f);

    void *p = image; /* payload_run()'s scan */
    while (p)
        p = load_one_payload(p, 0);
    int handled = chosen_handling();
    printf("RESULT handled=%d\n", handled);
    if (handled)
        return 0;

    if (!strcmp(argv[1], "stage1")) {
        /* what chainload_image() writes after the next stage's image */
        FILE *out = fopen(argv[4], "wb");
        assert(out);
        for (size_t i = 0; i < chosen_cnt; i++)
            fprintf(out, "%s\n", chosen[i]);
        fclose(out);
        return 0;
    }

    assert(kernel && fdt);
    transfer();
    char compat[128];
    int len = snprintf(compat, sizeof(compat), "%s", argv[3]) + 1;
    for (char *c = compat; *c; c++)
        if (*c == ';')
            *c = 0;
    assert(!fdt_create_empty_tree(dt, sizeof(blob)));
    assert(!fdt_setprop(dt, 0, "compatible", compat, len));
    assert(fdt_add_subnode(dt, 0, "chosen") >= 0);
    assert(!dt_set_m3_defaults());
    int node = fdt_path_offset(dt, "/chosen");
    for (int i = 0; i < MAX_CHOSEN_PARAMS && chosen_params[i][0]; i++) {
        const char *v = chosen_params[i][1] ? chosen_params[i][1] : "";
        assert(!fdt_setprop(dt, node, chosen_params[i][0], v, (int)strlen(v) + 1));
    }
    if (!fdt_node_check_compatible(dt, 0, "apple,t8122"))
        assert(!dt_set_gpu_t8122_select(dt));
    int prop;
    fdt_for_each_property_offset(prop, dt, node) {
        const char *name;
        int vlen;
        const char *v = fdt_getprop_by_offset(dt, prop, &name, &vlen);
        char shown[160]; /* a name that took in the next line keeps it on one output line */
        size_t k = 0;
        for (const char *c = name; *c && k < sizeof(shown) - 3; c++) {
            if (*c == '\n')
                shown[k++] = '\\';
            shown[k++] = *c == '\n' ? 'n' : *c;
        }
        shown[k] = 0;
        printf("CHOSEN %s=%.*s armed=%d\n", shown, vlen, v, vlen == 2 && !strcmp(v, "1"));
    }
    printf("SELECT %s\n", selected);
    printf("DISPLAY %s\n", display_value);
    return 0;
}
'''


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    out = tmp_path_factory.mktemp("m3-optout")
    lib = ROOT / "src/libfdt"
    source = harness()
    binaries = {}
    for chainloading in (False, True):
        binary = out / ("chainloading" if chainloading else "plain")
        subprocess.run(
            ["cc", "-std=gnu11", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror",
             "-Wno-unused-parameter", *(["-DCHAINLOADING"] if chainloading else []),
             "-I", str(lib), "-x", "c", "-",
             *map(str, (lib / f for f in (
                 "fdt.c", "fdt_ro.c", "fdt_rw.c", "fdt_sw.c", "fdt_wip.c",
                 "fdt_empty_tree.c", "fdt_strerror.c"))), "-o", str(binary)],
            input=source, text=True, check=True,
        )
        binaries[chainloading] = binary
    return out, binaries


class Boot:
    def __init__(self, stdout):
        self.stdout = stdout
        self.chosen = {}
        self.armed = set()
        self.select = None
        self.handled = None
        for line in stdout.splitlines():
            if line.startswith("CHOSEN "):
                head, armed = line[7:].rsplit(" armed=", 1)
                name, value = head.split("=", 1)
                self.chosen[name] = value.rstrip("\0")
                if armed == "1":
                    self.armed.add(name)
            elif line.startswith("SELECT "):
                self.select = line[7:]
            elif line.startswith("RESULT handled="):
                self.handled = int(line[15:])


def run(build, chainloading, mode, payload, compat="apple,j613;apple,t8122;apple,arm-platform"):
    out, binaries = build
    path = out / "payload.bin"
    path.write_bytes(payload)
    forwarded = out / "forwarded.txt"
    forwarded.write_bytes(b"")
    r = subprocess.run([str(binaries[chainloading]), mode, str(path), compat, str(forwarded)],
                       capture_output=True, text=True, check=True)
    return Boot(r.stdout), forwarded.read_bytes()


def boot(build, chainloading, config, compat="apple,j613;apple,t8122;apple,arm-platform",
         trailer=b""):
    """One boot.bin: kernel, device tree, config, then trailer (a stage 1's variables) and zeros."""
    return run(build, chainloading, "boot", KERNEL + FDT + config + trailer, compat)[0]


def fill(chainloading):
    """The same filler line until chosen[] is exactly full: kboot keeps one entry for all of them."""
    return b"chosen.test,filler=1\n" * (MAX_CHOSEN_VARS - (1 if chainloading else 0))


def defaults_off(b):
    for name in AIR:
        assert name not in b.armed, (name, b.stdout)


MODES = [False, True]


# ---- controls: a clean config keeps working ----

@pytest.mark.parametrize("chainloading", MODES)
def test_no_config_gets_the_whole_air_recipe(build, chainloading):
    b = boot(build, chainloading, b"")
    assert set(AIR) <= b.armed and b.select == "g15-standin"
    assert "asahi,m3-defaults" not in b.chosen


@pytest.mark.parametrize("chainloading", MODES)
def test_owner_gpu_off_is_kept(build, chainloading):
    b = boot(build, chainloading, b"chosen.asahi,t8122-gpu=0\n")
    assert b.chosen["asahi,t8122-gpu"] == "0" and "asahi,t8122-gpu" not in b.armed
    assert b.select == "reservation-only"


@pytest.mark.parametrize("chainloading", MODES)
def test_owner_master_off_is_kept(build, chainloading):
    b = boot(build, chainloading, b"chosen.asahi,m3-defaults=0\n")
    defaults_off(b)
    assert b.select == "reservation-only"


@pytest.mark.parametrize("chainloading", MODES)
def test_a_full_but_not_overflowing_chosen_buffer_is_no_loss(build, chainloading):
    b = boot(build, chainloading, fill(chainloading))
    assert set(AIR) <= b.armed and "asahi,m3-defaults" not in b.chosen


@pytest.mark.parametrize("chainloading", MODES)
def test_an_img4_end_after_the_config_is_no_loss(build, chainloading):
    b = boot(build, chainloading, b"chosen.asahi,t8122-dcpext=0\n",
             trailer=b"\x30\x83\x01\x02\x00" + b"\x16\x04IMG4" + bytes(8))
    assert set(AIR) <= b.armed and "asahi,m3-defaults" not in b.chosen


@pytest.mark.parametrize("chainloading", MODES)
def test_literal_display_zero_is_the_framebuffer_setting(build, chainloading):
    b = boot(build, chainloading, b"display=0\n")
    assert "DISPLAY 0" in b.stdout
    assert set(AIR) <= b.armed


# ---- defect 1: an owner's =0 the payload scan lost ----

@pytest.mark.parametrize("victim", [b"chosen.asahi,t8122-gpu=0\n", b"chosen.asahi,m3-defaults=0\n"])
@pytest.mark.parametrize("chainloading", MODES)
def test_an_opt_out_past_a_full_chosen_buffer_keeps_defaults_off(build, chainloading, victim):
    b = boot(build, chainloading, fill(chainloading) + victim)
    defaults_off(b)
    assert b.chosen.get("asahi,m3-defaults") == "0"
    assert b.select == "reservation-only"


@pytest.mark.parametrize("line", [
    b"chosen.asahi,m3-defaults=0",                             # no newline before the end
    b"chosen.asahi,m3-defaults=0" + b"x" * 1025 + b"\n",       # value too long
    b"chosen." + b"n" * 70 + b"=0\n",                          # name too long
], ids=["missing-newline", "overlong-value", "overlong-name"])
@pytest.mark.parametrize("chainloading", MODES)
def test_an_unreadable_chosen_line_keeps_defaults_off(build, chainloading, line):
    b = boot(build, chainloading, line)
    defaults_off(b)
    assert b.chosen.get("asahi,m3-defaults") == "0"


@pytest.mark.parametrize("before", [
    b"display=" + b"x" * 1100 + b"\n",                         # an earlier line too long to read
    b"\x01\x02\x03\x04unknown\x00",                            # unknown bytes after the kernel
], ids=["overlong-earlier-line", "unknown-after-kernel"])
@pytest.mark.parametrize("chainloading", MODES)
def test_a_stop_before_an_opt_out_keeps_defaults_off(build, chainloading, before):
    b = boot(build, chainloading, before + b"chosen.asahi,t8122-gpu=0\n")
    defaults_off(b)
    assert b.chosen.get("asahi,m3-defaults") == "0"


@pytest.mark.parametrize("before", [b"garbage\n", b"chosen.asahi,t8122-dcpext\n"],
                         ids=["plain-line", "chosen-line"])
@pytest.mark.parametrize("chainloading", MODES)
def test_a_line_without_equals_swallowing_an_opt_out_keeps_defaults_off(build, chainloading,
                                                                       before):
    b = boot(build, chainloading, before + b"chosen.asahi,t8122-gpu=0\n")
    defaults_off(b)
    assert b.chosen.get("asahi,m3-defaults") == "0"


def test_a_chainloading_stage_1_passes_its_loss_on(build):
    """Stage 1 lost a chosen line; stage 2's own config says =1, and stage 1's word comes last."""
    stage1, forwarded = run(build, True, "stage1",
                            b"chosen.asahi,t8122-gpu=0" + b"x" * 1025 + b"\n")
    assert stage1.handled == 0
    assert forwarded.endswith(b"chosen.asahi,m3-defaults=0\n")
    b = boot(build, True, b"chosen.asahi,m3-defaults=1\n", trailer=forwarded)
    defaults_off(b)
    assert b.chosen.get("asahi,m3-defaults") == "0"


def test_a_chainloading_stage_1_overflow_reaches_stage_2(build):
    stage1, forwarded = run(build, True, "stage1", fill(True) + b"chosen.asahi,t8122-gpu=0\n")
    assert stage1.handled == 0 and forwarded.endswith(b"chosen.asahi,m3-defaults=0\n")
    defaults_off(boot(build, True, b"", trailer=forwarded))


def test_a_clean_chainloading_stage_1_passes_nothing_extra(build):
    stage1, forwarded = run(build, True, "stage1", b"chosen.asahi,efi-system-partition=X\n")
    assert b"m3-defaults" not in forwarded
    b = boot(build, True, b"chosen.asahi,t8122-gpu=0\n", trailer=forwarded)
    assert "asahi,t8122-dcp" in b.armed and "asahi,t8122-gpu" not in b.armed
    assert "asahi,m3-defaults" not in b.chosen


# ---- defect 2: the Air's display line at 0 keeps the default GPU off ----

@pytest.mark.parametrize("board", ["apple,j613", "apple,j615"])
@pytest.mark.parametrize("value", [b"0", b"", b"2"])
@pytest.mark.parametrize("chainloading", MODES)
def test_air_display_off_keeps_the_default_gpu_off(build, chainloading, board, value):
    b = boot(build, chainloading, b"chosen.asahi,t8122-dcp=" + value + b"\n",
             compat=board + ";apple,t8122;apple,arm-platform")
    assert b.chosen["asahi,t8122-dcp"] == value.decode() and "asahi,t8122-dcp" not in b.armed
    for name in AIR_GPU:
        assert name not in b.chosen, (name, b.stdout)
    assert b.select == "reservation-only"


@pytest.mark.parametrize("chainloading", MODES)
def test_air_display_off_keeps_an_owner_gpu_line(build, chainloading):
    b = boot(build, chainloading, b"chosen.asahi,t8122-dcp=0\nchosen.asahi,t8122-gpu=1\n")
    assert "asahi,t8122-gpu" in b.armed and "asahi,t8122-dcp" not in b.armed
    assert "asahi,t8122-gpu-power-standin" not in b.chosen
    assert "asahi,t8122-gpu-fuse-leakage" not in b.chosen
    assert b.select == "g15"


@pytest.mark.parametrize("chainloading", MODES)
def test_air_display_on_by_the_owner_gets_the_gpu_recipe(build, chainloading):
    b = boot(build, chainloading, b"chosen.asahi,t8122-dcp=1\n")
    assert set(AIR) <= b.armed and b.select == "g15-standin"


@pytest.mark.parametrize("chainloading", MODES)
def test_pro_display_off_keeps_its_own_gpu_default(build, chainloading):
    b = boot(build, chainloading, b"chosen.asahi,t6030-dcp=0\n",
             compat="apple,j516s;apple,t6030;apple,arm-platform")
    assert "asahi,t6030-dcp" not in b.armed and "asahi,t6030-gpu" in b.armed
