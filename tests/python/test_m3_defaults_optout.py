# SPDX-License-Identifier: MIT
"""A config line m1n1 loses must not leave an earlier graphics value armed on the M3 default boards.

The whole payload_run() runs on the host, with the real payload scan and loaders (gzip through
tinf, device tree ranking, the kernel loader), kboot_set_chosen(), dt_set_m3_defaults() and the
T8122 GPU selector. Stage 1 hands its lines on with chainload_image()'s own loop, and stage 2
reads them after its boot.bin as on a Mac. Only hardware is a stub: CPU start, mitigations, NVMe,
the device tree handoff to the kernel and the GPU setups behind the selector. When an
update-m1n1 is available (M1N1_TEST_UPDATE_M1N1, or one on PATH), boot.bin comes from running it,
with only its two fixed system paths moved into the test directory.
"""
import gzip
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
MAX_CHOSEN_VARS = 16

M1N1 = b"M" * 64                           # the stage 2 image itself; its payload follows it
UBOOT = (bytes(16) + (0x10000).to_bytes(8, "little") + bytes(32) + b"ARM\x64" + bytes(4)
         + bytes(4096 - 64))               # an arm64 Image header, image_size 64 KiB
BOARDS = {
    "j613": ("J613", "apple,j613;apple,t8122;apple,arm-platform"),
    "j615": ("J615", "apple,j615;apple,t8122;apple,arm-platform"),
    "j516s": ("J516S", "apple,j516s;apple,t6030;apple,arm-platform"),
    "j516c": ("J516C", "apple,j516c;apple,t6031;apple,arm-platform"),
    "j293": ("J293", "apple,j293;apple,t8103;apple,arm-platform"),
}
FAMILY = {"j613": ("asahi,t8122-", "asahi,j613-", "asahi,j615-"),
          "j615": ("asahi,t8122-", "asahi,j613-", "asahi,j615-"),
          "j516s": ("asahi,t6030-",)}
AIR = ("asahi,t8122-dcp", "asahi,t8122-gpu", "asahi,t8122-gpu-power-standin",
       "asahi,t8122-gpu-fuse-leakage")
GPU_ON = b"chosen.asahi,t8122-gpu=1\n"
GPU_OFF = b"chosen.asahi,t8122-gpu=0\n"
LOST = b"m1n1.config-lost=1\n"
STAGE2 = ["plain", "stage2"]               # stage2: CHAINLOADING + ESP_STAGE2, as packaged


def between(source, start, end):
    a = source.index(start)
    return source[a:source.index(end, a)]


def function(source, signature):
    a = source.index(signature)
    return source[a:source.index("\n}\n", a) + 3]


def optional(source, signature):
    return function(source, signature) if signature in source else ""


def harness():
    payload = (SRC / "payload.c").read_text()
    kboot = (SRC / "kboot.c").read_text()
    gpu = (SRC / "kboot_gpu.c").read_text()
    chainload = (SRC / "chainload.c").read_text()
    header = (SRC / "kboot.h").read_text()
    return r'''
#include <assert.h>
#include <ctype.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
#include "tinf/tinf.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define BUILD_TAG "host-test"
#define ARRAY_SIZE(s) (sizeof(s) / sizeof((s)[0]))
#define ALIGN_UP(x, a) (((x) + (a) - 1) & ~((a) - 1))
''' + between(header, "struct kernel_header {", "void kboot_set_initrd(") + r'''
enum { V26_6_2 = 1 };
static struct { int version; } os_firmware = {V26_6_2};
#include "payload_profile.h"
static int XzDecode(void *in, uint32_t *in_len, void *out, uint32_t *out_len) { return 0; }

/* heapblock: one bump allocator */
static u8 heap[16 << 20] __attribute__((aligned(2 << 20)));
static size_t heap_used;
void *heapblock_alloc_aligned(size_t size, size_t align)
{
    heap_used = ALIGN_UP(heap_used, align);
    void *r = heap + heap_used;
    heap_used += size;
    assert(heap_used <= sizeof(heap));
    return r;
}

/* kboot.c, with allocation failure on demand: the n-th calloc from the start fails */
static int calloc_fail_in = -1;
static void *test_calloc(size_t n, size_t size)
{
    if (calloc_fail_in == 0) { calloc_fail_in = -1; return NULL; }
    if (calloc_fail_in > 0) calloc_fail_in--;
    return calloc(n, size);
}
#define calloc test_calloc
#define MAX_CHOSEN_PARAMS 16
static char *chosen_params[MAX_CHOSEN_PARAMS][2];
static bool chosen_is_default[MAX_CHOSEN_PARAMS];
static bool chosen_lost;
static unsigned char blob[1 << 16];
static void *dt = blob;
void kboot_delete_chosen(const char *name);
''' + function(kboot, "int kboot_set_chosen(const char *name, const char *value)\n{") + \
        function(kboot, "void kboot_delete_chosen(const char *name)\n{") + \
        optional(kboot, "void kboot_set_config_lost(void)\n{") + \
        between(kboot, "struct m3_default_board {", "/* The config value of chosen.") + \
        function(kboot, "static const char *kboot_chosen_param(const char *name)\n{") + \
        function(kboot, "static void dt_drop_m3_defaults(void)\n{") + \
        optional(kboot, "static void dt_drop_m3_graphics(") + \
        function(kboot, "static int dt_set_m3_defaults(void)\n{") + r'''
/* kboot_gpu.c: the T8122 selector, with its GPU setups recorded instead of run */
struct g15_soc { const char *optin; };
static const struct g15_soc g15_soc_t8122 = {"asahi,t8122-gpu"};
static const struct g15_soc g15_soc_t8122_standin = {"asahi,t8122-gpu"};
static const char *selected = "none";
static void t8122_leak_fuse_export(void *d) {}
static int dt_set_gpu_t8122(void *d) { selected = "reservation-only"; return 0; }
static int dt_set_gpu_g15(void *d, const struct g15_soc *soc)
{
    selected = soc == &g15_soc_t8122_standin ? "g15-standin" : "g15";
    return 0;
}
''' + function(gpu, "static bool g15_optin(") + \
        function(gpu, "static int dt_set_gpu_t8122_select(void *dt)\n{") + r'''
/* kboot's handoff: /chosen as dt_set_chosen() writes it, then what the GPU code would see */
static void kboot_set_initrd(void *start, size_t size) {}
static int kboot_prepare_dt(void *fdt)
{
    assert(!fdt_open_into(fdt, dt, sizeof(blob)));
    int node = fdt_path_offset(dt, "/chosen");
    if (node < 0)
        node = fdt_add_subnode(dt, 0, "chosen");
    assert(node >= 0);
    assert(!dt_set_m3_defaults());
    node = fdt_path_offset(dt, "/chosen");
    for (int i = 0; i < MAX_CHOSEN_PARAMS && chosen_params[i][0]; i++) {
        const char *v = chosen_params[i][1] ? chosen_params[i][1] : "";
        assert(!fdt_setprop(dt, node, chosen_params[i][0], v, (int)strlen(v) + 1));
    }
    if (!fdt_node_check_compatible(dt, 0, "apple,t8122"))
        assert(!dt_set_gpu_t8122_select(dt));
    int prop;
    fdt_for_each_property_offset(prop, dt, node) {
        const char *name;
        int len;
        const char *v = fdt_getprop_by_offset(dt, prop, &name, &len);
        char shown[160]; /* a name that took in the next line stays on one output line */
        size_t k = 0;
        for (const char *c = name; *c && k < sizeof(shown) - 3; c++) {
            if (*c == '\n')
                shown[k++] = '\\';
            shown[k++] = *c == '\n' ? 'n' : *c;
        }
        shown[k] = 0;
        printf("CHOSEN %s=%.*s armed=%d\n", shown, len, v, len == 2 && !strcmp(v, "1"));
    }
    printf("SELECT %s\n", selected);
    return 0;
}
static int kboot_boot(void *kernel) { printf("RESULT booted=1\n"); return 0; }

/* The rest of the machine payload_run() talks to */
static const char *target_type;
static void *adt;
static const char *adt_getprop(void *a, int node, const char *name, u32 *len) { return target_type; }
static void cpufreq_payload_boost(bool on) {}
static void cpufreq_init(void) {}
#define T8140 0x8140
#define SMP_TIMEOUT -2
#define MAX_CPUS 4
static int chip_id = 0x8122, boot_cpu_idx;
static int smp_start_secondaries(void) { return 0; }
static int mitigations_perform(void) { return 0; }
static bool smp_is_alive(int cpu) { return false; }
static int smp_call_sync0(int cpu, void (*fn)(void)) { return 0; }
static void do_enable_tso(void) {}
static bool nvme_shutdown(void) { return true; }
static bool nvme_keep_running_for_linux;
static bool nvme_adopt_live_session;
static int display_configure(const char *s) { printf("DISPLAY %s\n", s); return 0; }
static void mitigations_configure(const char *s) {}
static int boot_storage_load(const char *spec, struct kernel_header **k, void **f) { return -1; }
/* A stage 1 configured with its target (stage1_config.c), or not (NULL) */
static const char *stage1_target;
static __attribute__((unused)) const char *stage1_config_target(void) { return stage1_target; }
static __attribute__((unused)) const char *stage1_config_esp_uuid(void) { return "ESP-UUID"; }
struct { void *entry; } next_stage;
static u8 *payload_at;
static void *payload_start(void) { return payload_at; }

/* chainload_image()'s own loop writes the lines after the next stage's image */
static const char *forward_path;
static u8 new_image[1 << 16];
static int chainload_load(const char *spec, char **vars, size_t *var_cnt, size_t var_capacity)
{
    size_t size = 0, var_cnt_ = *var_cnt;
    {
        size_t var_cnt = var_cnt_;
''' + between(chainload, "    u8 *p = new_image + size;\n",
              "\n\n    u8 *fw_data") + r'''
        FILE *out = fopen(forward_path, "wb");
        assert(out && fwrite(new_image, 1, p - new_image, out) == (size_t)(p - new_image));
        fclose(out);
    }
    printf("RESULT chainloaded=1\n");
    return 0;
}

''' + between(payload, "// Kernels must be 2MB aligned", "static void finalize_uncompression(") + \
        between(payload, "static void finalize_uncompression(", "#define MAX_VAR_NAME 64") + \
        between(payload, "#define MAX_VAR_NAME 64", "void do_enable_tso(void)") + \
        function(payload, "int payload_run(void)\n{") + r'''
static u8 image[1 << 20] __attribute__((aligned(16)));

static int mkdtb(const char *compat_list, const char *path)
{
    static unsigned char tree[4096];
    char compat[128];
    int len = snprintf(compat, sizeof(compat), "%s", compat_list) + 1;
    for (char *c = compat; *c; c++)
        if (*c == ';')
            *c = 0;
    assert(!fdt_create_empty_tree(tree, sizeof(tree)));
    assert(!fdt_setprop(tree, 0, "compatible", compat, len));
    assert(fdt_add_subnode(tree, 0, "chosen") >= 0);
    assert(!fdt_pack(tree));
    FILE *out = fopen(path, "wb");
    assert(out && fwrite(tree, 1, fdt_totalsize(tree), out) == fdt_totalsize(tree));
    fclose(out);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc == 4 && !strcmp(argv[1], "mkdtb"))
        return mkdtb(argv[2], argv[3]);
    /* run IMAGE PAYLOAD-OFFSET TARGET-TYPE FORWARD-OUT CALLOC-FAIL */
    assert(argc == 7 && !strcmp(argv[1], "run"));
    FILE *f = fopen(argv[2], "rb");
    assert(f);
    size_t n = fread(image, 1, sizeof(image) - 4096, f);
    assert(!ferror(f) && n < sizeof(image) - 4096);
    fclose(f);
    payload_at = image + atoi(argv[3]);
    target_type = argv[4];
    forward_path = argv[5];
    calloc_fail_in = atoi(argv[6]);
    stage1_target = getenv("M1N1_TEST_STAGE1_TARGET");
    int rc = payload_run();
    printf("RESULT payload_run=%d\n", rc);
    return 0;
}
'''


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    out = tmp_path_factory.mktemp("m3-optout")
    lib = SRC / "libfdt"
    sources = [lib / f for f in ("fdt.c", "fdt_ro.c", "fdt_rw.c", "fdt_sw.c", "fdt_wip.c",
                                 "fdt_empty_tree.c", "fdt_strerror.c")]
    sources += [SRC / "tinf" / f for f in ("tinflate.c", "tinfgzip.c", "crc32.c")]
    source = harness()
    binaries = {}
    for name, flags in (("plain", []), ("stage2", ["-DCHAINLOADING", "-DESP_STAGE2"]),
                        ("stage1", ["-DCHAINLOADING"])):
        binary = out / name
        subprocess.run(
            ["cc", "-std=gnu11", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror",
             "-Wno-unused-parameter", *flags, "-I", str(SRC), "-I", str(lib), "-x", "c", "-",
             "-x", "none", *map(str, sources), "-o", str(binary)],
            input=source, text=True, check=True,
        )
        binaries[name] = binary
    dtbs = {}
    for board, (_, compat) in BOARDS.items():
        dtbs[board] = out / (board + ".dtb")
        subprocess.run([str(binaries["plain"]), "mkdtb", compat, str(dtbs[board])], check=True)
    return out, binaries, dtbs


class Run:
    def __init__(self, stdout, forwarded):
        self.stdout = stdout
        self.forwarded = forwarded
        self.chosen = {}
        self.armed = set()
        self.select = None
        self.rc = None
        self.booted = self.chainloaded = False
        for line in stdout.splitlines():
            if line.startswith("CHOSEN "):
                head, armed = line[7:].rsplit(" armed=", 1)
                name, value = head.split("=", 1)
                self.chosen[name] = value.rstrip("\0")
                if armed == "1":
                    self.armed.add(name)
            elif line.startswith("SELECT "):
                self.select = line[7:]
            elif line.startswith("RESULT payload_run="):
                self.rc = int(line[19:])
            elif line == "RESULT booted=1":
                self.booted = True
            elif line == "RESULT chainloaded=1":
                self.chainloaded = True


def run(tools, build, image, board="j613", calloc_fail=-1, target=None):
    out, binaries, _ = tools
    path = out / "boot.bin"
    path.write_bytes(image)
    forwarded = out / "forwarded.txt"
    forwarded.write_bytes(b"")
    env = {k: v for k, v in os.environ.items() if k != "M1N1_TEST_STAGE1_TARGET"}
    if target:
        env["M1N1_TEST_STAGE1_TARGET"] = target
    r = subprocess.run([str(binaries[build]), "run", str(path), str(len(M1N1)), BOARDS[board][0],
                        str(forwarded), str(calloc_fail)], env=env,
                       capture_output=True, text=True, errors="backslashreplace", check=True)
    return Run(r.stdout, forwarded.read_bytes())


def boot_bin(tools, config, board="j613"):
    """boot.bin laid out as update-m1n1 writes it: m1n1, device trees, gzipped U-Boot, config"""
    _, _, dtbs = tools
    other = "j516s" if board != "j516s" else "j613"
    return (M1N1 + dtbs[board].read_bytes() + dtbs[other].read_bytes() +
            gzip.compress(UBOOT, mtime=0) + config)


def boot(tools, build, config, board="j613", trailer=b"", calloc_fail=-1):
    return run(tools, build, boot_bin(tools, config, board) + trailer, board, calloc_fail)


def stage1(tools, config, configured=False):
    """A chainloading stage 1 and its own config; returns what it hands to stage 2. A configured
    one takes its target from stage1_config and adds chosen.asahi,efi-system-partition itself."""
    if configured:
        r = run(tools, "stage1", M1N1 + config, target="ESP-UUID;m1n1/boot.bin")
    else:
        r = run(tools, "stage1", M1N1 + b"chainload=ESP-UUID;m1n1/boot.bin\n" + config)
    assert r.rc == 0 and r.chainloaded, r.stdout
    return r.forwarded


def first_chosen(build):
    return 0 if build == "plain" else 1


def fill(build, keep=0):
    """The same filler line until chosen[] is full, keeping keep slots for lines before it"""
    return b"chosen.test,filler=1\n" * (MAX_CHOSEN_VARS - first_chosen(build) - keep)


def graphics_off(r, board="j613"):
    assert r.rc == 0 and r.booted, r.stdout
    for name in r.chosen:
        assert not name.startswith(FAMILY[board]), (name, r.stdout)
    if board in ("j613", "j615"):
        assert r.select == "reservation-only", r.stdout


def booted(r):
    assert r.rc == 0 and r.booted, r.stdout


# ---- a complete config: unchanged ----

@pytest.mark.parametrize("build", STAGE2)
def test_no_config_gets_the_whole_air_recipe(tools, build):
    b = boot(tools, build, b"")
    booted(b)
    assert set(AIR) <= b.armed and b.select == "g15-standin"


@pytest.mark.parametrize("build", STAGE2)
def test_the_pro_gets_its_two_defaults(tools, build):
    b = boot(tools, build, b"", board="j516s")
    booted(b)
    assert {"asahi,t6030-dcp", "asahi,t6030-gpu"} <= b.armed


@pytest.mark.parametrize("config,armed,select", [
    (GPU_OFF, set(AIR) - {"asahi,t8122-gpu"}, "reservation-only"),
    (GPU_ON, set(AIR), "g15-standin"),
    (GPU_ON + GPU_OFF, set(AIR) - {"asahi,t8122-gpu"}, "reservation-only"),
    (b"chosen.asahi,t8122-dcp=0\n" + GPU_ON, {"asahi,t8122-gpu"}, "g15"),
    (b"chosen.asahi,t8122-dcp=0\n", set(), "reservation-only"),
    (b"chosen.asahi,m3-defaults=0\n", set(), "reservation-only"),
], ids=["gpu-off", "gpu-on", "gpu-on-then-off", "dcp-off-gpu-on", "dcp-off", "master-off"])
@pytest.mark.parametrize("build", STAGE2)
def test_owner_lines_are_kept(tools, build, config, armed, select):
    b = boot(tools, build, config)
    booted(b)
    assert b.armed & set(AIR) == armed and b.select == select


@pytest.mark.parametrize("build", STAGE2)
def test_a_full_but_not_overflowing_chosen_buffer_is_no_loss(tools, build):
    b = boot(tools, build, GPU_ON + fill(build, keep=1))
    booted(b)
    assert "asahi,t8122-gpu" in b.armed


@pytest.mark.parametrize("header", [b"\x30\x83\x01\x02\x00", b"\x30\x83\x3d\x0a\x12"],
                         ids=["plain-header", "header-reading-as-a-line"])
@pytest.mark.parametrize("build", STAGE2)
def test_an_img4_end_is_no_loss(tools, build, header):
    b = boot(tools, build, GPU_ON, trailer=header + b"\x16\x04IMG4" + bytes(8))
    booted(b)
    assert "asahi,t8122-gpu" in b.armed


@pytest.mark.parametrize("config", [b"foo=bar\n", b"junk\nfoo=bar\n"],
                         ids=["unknown-variable", "line-without-equals-before-unknown"])
@pytest.mark.parametrize("build", STAGE2)
def test_lines_m1n1_does_not_know_are_no_loss(tools, build, config):
    b = boot(tools, build, GPU_ON + config)
    booted(b)
    assert "asahi,t8122-gpu" in b.armed


@pytest.mark.parametrize("build", STAGE2)
def test_literal_display_zero_is_the_framebuffer_setting(tools, build):
    b = boot(tools, build, b"display=0\n")
    booted(b)
    assert set(AIR) <= b.armed


# ---- a lost line: no graphics on the four boards, and the Mac still boots ----

LOSSES = {
    "gpu-on-fillers-lost-gpu-off": lambda build: GPU_ON + fill(build, keep=1) + GPU_OFF,
    "missing-newline": lambda build: GPU_ON + b"chosen.asahi,t8122-gpu=0",
    "overlong-value": lambda build: GPU_ON + b"chosen.asahi,t8122-gpu=0" + b"x" * 1025 + b"\n",
    "overlong-name": lambda build: GPU_ON + b"chosen." + b"n" * 70 + b"=0\n",
    "overlong-earlier-line": lambda build: GPU_ON + b"display=" + b"x" * 1100 + b"\n" + GPU_OFF,
    "swallowed-line": lambda build: GPU_ON + b"junk\n" + GPU_OFF,
    "unknown-after-config": lambda build: GPU_ON + b"\x01\x02\x03\x04unknown\x00" + GPU_OFF,
    "unknown-after-kernel": lambda build: b"\x01\x02\x03\x04unknown\x00" + GPU_OFF,
    "marker-line": lambda build: GPU_ON + LOST,
}


@pytest.mark.parametrize("loss", sorted(LOSSES))
@pytest.mark.parametrize("build", STAGE2)
def test_a_lost_line_keeps_air_graphics_off_and_boots(tools, build, loss):
    graphics_off(boot(tools, build, LOSSES[loss](build)))


@pytest.mark.parametrize("build", STAGE2)
def test_a_lost_line_keeps_pro_graphics_off_and_boots(tools, build):
    config = (b"chosen.asahi,t6030-gpu=1\nchosen.asahi,t6030-dcpext=1\n" +
              fill(build, keep=2) + b"chosen.asahi,t6030-gpu=0\n")
    graphics_off(boot(tools, build, config, board="j516s"), board="j516s")


@pytest.mark.parametrize("build", STAGE2)
def test_a_failed_setter_replacing_gpu_on_with_gpu_off(tools, build):
    """kboot_set_chosen() keeps the old value when it cannot store the new one"""
    nth = 2 * (first_chosen(build) + 1)   # two allocations per stored line; the tag goes first
    graphics_off(boot(tools, build, GPU_ON + GPU_OFF, calloc_fail=nth))


@pytest.mark.parametrize("build", STAGE2)
def test_a_kboot_table_too_small_for_the_config(tools, build):
    """chosen[] holds every line, but apple,tso takes the kboot entry the last one needed"""
    lines = b"".join(b"chosen.test,line-%d=1\n" % i
                     for i in range(MAX_CHOSEN_VARS - first_chosen(build) - 1))
    graphics_off(boot(tools, build, b"tso=1\n" + GPU_ON + lines))


@pytest.mark.parametrize("board,line", [("j516c", b"chosen.asahi,t6031-gpu="),
                                        ("j293", b"chosen.test,switch=")])
@pytest.mark.parametrize("build", STAGE2)
def test_other_boards_keep_their_lines_after_a_loss(tools, build, board, line):
    b = boot(tools, build, line + b"1\n" + fill(build, keep=1) + line + b"0\n", board=board)
    booted(b)
    assert b.chosen[line[7:-1].decode()] == "1"   # as before the M3 defaults: no rule there


# ---- the chainload route ----

def test_stage_1_hands_its_loss_on_after_every_owner_line(tools):
    forwarded = stage1(tools, fill("stage1") + GPU_OFF)
    lines = forwarded.splitlines(keepends=True)
    assert lines[-1] == LOST and lines.count(LOST) == 1
    assert len(lines) == MAX_CHOSEN_VARS + 1           # all of chosen[], then the loss
    b = boot(tools, "stage2", GPU_ON + b"chosen.asahi,m3-defaults=1\n", trailer=forwarded)
    graphics_off(b)


def test_stage_1_loss_survives_a_stage_2_owner_marker_line(tools):
    forwarded = stage1(tools, b"chosen.asahi,t8122-gpu=0" + b"x" * 1025 + b"\n")
    assert forwarded.endswith(LOST)
    graphics_off(boot(tools, "stage2", GPU_ON + b"m1n1.config-lost=0\n", trailer=forwarded))


def test_a_clean_stage_1_hands_over_its_lines_only(tools):
    forwarded = stage1(tools, b"chosen.asahi,efi-system-partition=X\n")
    assert b"config-lost" not in forwarded
    b = boot(tools, "stage2", GPU_ON, trailer=forwarded)
    booted(b)
    assert "asahi,t8122-gpu" in b.armed and b.chosen["asahi,efi-system-partition"] == "X"


def test_stage_2_overflowing_with_stage_1_lines_keeps_graphics_off(tools):
    forwarded = stage1(tools, b"".join(b"chosen.test,s1-%d=1\n" % i for i in range(3)))
    owner = GPU_ON + b"".join(b"chosen.test,owner-%d=1\n" % i
                              for i in range(MAX_CHOSEN_VARS - 1 - 1 - 2))
    graphics_off(boot(tools, "stage2", owner, trailer=forwarded))


def test_a_configured_stage_1_hands_on_a_loss_within_its_capacity(tools):
    forwarded = stage1(tools, GPU_ON + b"junk\n" + GPU_OFF, configured=True)
    lines = forwarded.splitlines(keepends=True)
    assert lines[-2:] == [b"chosen.asahi,efi-system-partition=ESP-UUID\n", LOST]
    graphics_off(boot(tools, "stage2", GPU_ON, trailer=forwarded))


def test_a_configured_stage_1_with_a_full_chosen_buffer_still_refuses_as_before(tools):
    """Not new: a configured stage 1 has always refused once chosen[] leaves no room for its
    efi-system-partition line, before #18 too. No loss marker is claimed through this path."""
    r = run(tools, "stage1", M1N1 + GPU_ON + fill("stage1", keep=1) + GPU_OFF,
            target="ESP-UUID;m1n1/boot.bin")
    assert r.rc == -1 and not r.chainloaded and r.forwarded == b""
    r = run(tools, "stage1", M1N1 + fill("stage1"), target="ESP-UUID;m1n1/boot.bin")
    assert r.rc == -1 and not r.chainloaded


@pytest.mark.parametrize("board", ["j613", "j516s", "j293"])
def test_fourteen_owner_lines_overflow_stage_2_after_a_configured_stage_1(tools, board):
    """Stage 2 keeps its own tag, then 14 owner lines, then stage 1's tag and ESP line: the last
    one does not fit. The four M3 boards drop graphics and boot; other Macs boot as before."""
    gpu = {"j613": b"chosen.asahi,t8122-gpu=1\n", "j516s": b"chosen.asahi,t6030-gpu=1\n",
           "j293": b"chosen.test,switch=1\n"}[board]
    owner = gpu + b"".join(b"chosen.test,owner-%d=1\n" % i for i in range(13))
    forwarded = stage1(tools, b"", configured=True)
    assert forwarded.count(b"\n") == 2 and b"config-lost" not in forwarded
    b = boot(tools, "stage2", owner, board=board, trailer=forwarded)
    if board == "j293":
        booted(b)
        assert b.chosen["test,switch"] == "1" and "asahi,efi-system-partition" not in b.chosen
    else:
        graphics_off(b, board)


# ---- from /etc/m1n1.conf through update-m1n1 ----

def updater():
    path = os.environ.get("M1N1_TEST_UPDATE_M1N1") or shutil.which("update-m1n1")
    if not path or not Path(path).is_file():
        pytest.skip("no update-m1n1 (set M1N1_TEST_UPDATE_M1N1 to asahi-scripts' update-m1n1)")
    return Path(path)


def run_updater(tools, conf, board="j613", fix_last_line=False):
    """update-m1n1 itself; only /etc/default/update-m1n1 and /run/m1n1.conf move into the test"""
    out, _, dtbs = tools
    script = updater()
    work = out / "updater"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    text = script.read_text()
    for fixed in ("/etc/default/update-m1n1", "/run/m1n1.conf"):
        assert fixed in text
        text = text.replace(fixed, str(work / fixed.strip("/").replace("/", "_")))
    if fix_last_line:
        assert text.count("    while read line; do\n") == 1
        text = text.replace("    while read line; do\n", '    while read line || [ -n "$line" ]; do\n')
    (work / "update-m1n1").write_text(text)
    functions = script.parent / "functions.sh"
    shutil.copy(functions if functions.exists() else "/usr/share/asahi-scripts/functions.sh",
                work / "functions.sh")
    (work / "m1n1.bin").write_bytes(M1N1)
    (work / "u-boot.bin").write_bytes(UBOOT)
    (work / "m1n1.conf").write_bytes(conf)
    other = "j516s" if board != "j516s" else "j613"
    env = {"PATH": os.environ["PATH"], "M1N1": str(work / "m1n1.bin"),
           "U_BOOT": str(work / "u-boot.bin"), "CONFIG": str(work / "m1n1.conf"),
           "DTBS": f"{dtbs[board]} {dtbs[other]}"}
    subprocess.run(["sh", str(work / "update-m1n1"), str(work / "boot.bin")], env=env,
                   capture_output=True, check=True)
    return (work / "boot.bin").read_bytes()


def test_updater_gpu_on_then_off_keeps_the_gpu_off(tools):
    image = run_updater(tools, b"chosen.asahi,t8122-gpu=1\n# a comment\nchosen.asahi,t8122-gpu=0\n")
    forwarded = stage1(tools, b"chosen.asahi,efi-system-partition=X\n")
    b = run(tools, "stage2", image + forwarded)
    booted(b)
    assert b.chosen["asahi,t8122-gpu"] == "0" and b.select == "reservation-only"


def test_updater_many_owner_lines_overflow_stage_2_and_keep_graphics_off(tools):
    """About 14 owner lines already fill stage 2 once stage 1's own lines come after them"""
    conf = GPU_ON + b"".join(b"chosen.test,owner-%d=1\n" % i for i in range(13)) + GPU_OFF
    image = run_updater(tools, conf)
    assert image.endswith(GPU_OFF)
    forwarded = stage1(tools, b"", configured=True)
    graphics_off(run(tools, "stage2", image + forwarded))


@pytest.mark.xfail(strict=True, reason="upstream update-m1n1's `while read line` drops a last "
                   "line without its newline before m1n1 sees it")
def test_updater_last_line_without_newline_reaches_m1n1(tools):
    image = run_updater(tools, GPU_ON + b"chosen.asahi,t8122-gpu=0")
    assert image.endswith(GPU_OFF)


def test_updater_read_loop_fix_keeps_a_last_line_without_newline(tools):
    image = run_updater(tools, GPU_ON + b"chosen.asahi,t8122-gpu=0", fix_last_line=True)
    assert image.endswith(GPU_OFF)
    forwarded = stage1(tools, b"chosen.asahi,efi-system-partition=X\n")
    b = run(tools, "stage2", image + forwarded)
    booted(b)
    assert b.chosen["asahi,t8122-gpu"] == "0"
