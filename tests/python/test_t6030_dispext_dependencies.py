# SPDX-License-Identifier: MIT
"""A display-DART request must first publish its firmware-memory prerequisite."""

from pathlib import Path
from test_siliconforest_handoff import run_c

ROOT = Path(__file__).resolve().parents[2]


def test_dispext_request_includes_firmware_memory(tmp_path):
    source = (ROOT / "src/kboot_display_handoff.c").read_text()

    def function(signature):
        start = source.index(signature)
        return source[start:source.index("\n}", start) + 2] + "\n"

    def region(first, last):
        """The production declarations from first up to (not including) last."""
        start = source.index(first)
        return source[start:source.index(last, start)]

    # The real per-SoC processor tables: T6030's two and T6031's four.
    tables = region("struct dcpext6_inst {", "/* This boot's entry of dcpext6_socs[]")

    run_c(tmp_path, "t6030-external-dependencies", r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "libfdt.h"
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
typedef uint32_t u32;
typedef uint64_t u64;
enum { T6030 = 0x6030, T6031 = 0x6031, T8122 = 0x8122 };
struct dcp_soc { unsigned int chip; };
static struct { const struct dcp_soc *soc; } dcp6;
static const struct dcp_soc soc_t6030 = {T6030}, soc_t6031 = {T6031}, soc_t8122 = {T8122};
''' + tables + r'''
static const struct dcpext6_soc *dcpext6;
static const char *mem_name = "asahi,t6030-dcpext", *disp_name = "asahi,t6030-dispext";
static unsigned char blob[8192];
static void *dt = blob;
#define MAX_INSTS 4
static int reject_memory, calls[2 * MAX_INSTS], ncalls;

static bool dcpext6_present(void *tree, const struct dcpext6_inst *inst)
{
    return fdt_get_alias(tree, inst->alias) != NULL;
}
/* The processor's index in this boot's table; the handoffs must walk that table. */
static int dcpext6_index(const struct dcpext6_inst *inst)
{
    assert(dcpext6 && inst >= dcpext6->insts && inst < dcpext6->insts + dcpext6->ninsts);
    return inst - dcpext6->insts;
}
/* Reservation/validation boundaries: failed memory publication leaves no marker. */
static void dcpext6_handoff(void *tree, const struct dcpext6_inst *inst)
{
    int index = dcpext6_index(inst);
    assert(ncalls < 2 * MAX_INSTS);
    calls[ncalls++] = 2 * index;
    if (index != reject_memory)
        assert(!fdt_setprop_u32(tree, fdt_path_offset(tree, inst->alias),
                               "memory-ready", 1));
}
static void dispext6_handoff(void *tree, const struct dcpext6_inst *inst)
{
    int index = dcpext6_index(inst);
    assert(ncalls < 2 * MAX_INSTS);
    calls[ncalls++] = 2 * index + 1;
    int node = fdt_path_offset(tree, inst->alias);
    if (fdt_getprop(tree, node, "memory-ready", NULL))
        assert(!fdt_setprop_u32(tree, node, "display-ready", 1));
}
''' + function("static bool dcpext6_flag(") +
        function("static void dcpext6_handoffs(") + r'''
static const char *const names[MAX_INSTS] = {"dcpext0", "dcpext1", "dcpext2", "dcpext3"};

/* A device tree with dcpext0..dcpext<nexts - 1> and their aliases. */
static void world(const char *memory, const char *display, int nexts, int reject)
{
    assert(nexts >= 1 && nexts <= MAX_INSTS);
    assert(!fdt_create_empty_tree(dt, sizeof(blob)));
    int chosen = fdt_add_subnode(dt, 0, "chosen");
    assert(chosen >= 0);
    if (memory) assert(!fdt_setprop_string(dt, chosen, mem_name, memory));
    if (display) assert(!fdt_setprop_string(dt, chosen, disp_name, display));
    for (int i = 0; i < nexts; i++)
        assert(fdt_add_subnode(dt, 0, names[i]) >= 0);
    int aliases = fdt_add_subnode(dt, 0, "aliases");
    assert(aliases >= 0);
    for (int i = 0; i < nexts; i++) {
        char path[16] = "/";
        strcat(path, names[i]);
        assert(!fdt_setprop_string(dt, aliases, names[i], path));
    }
    reject_memory = reject;
    ncalls = 0;
    dcpext6_handoffs(dt);
}
static bool has(const char *alias, const char *property)
{
    int node = fdt_path_offset(dt, alias);
    return node >= 0 && fdt_getprop(dt, node, property, NULL);
}
/* The SoC being booted and the /chosen flags world() writes. */
static void use(const struct dcp_soc *soc, const char *memory, const char *display)
{
    dcp6.soc = soc;
    mem_name = memory;
    disp_name = display;
}
int main(void)
{
    use(&soc_t6030, "asahi,t6030-dcpext", "asahi,t6030-dispext");
    world(NULL, NULL, 2, -1);
    assert(ncalls == 0);
    world("0", "01", 2, -1);
    assert(ncalls == 0);
    world("1", NULL, 2, -1);
    assert(dcpext6 == &dcpext6_socs[0] && dcpext6->insts == dcpext6_insts);
    assert(ncalls == 2 && calls[0] == 0 && calls[1] == 2);
    assert(has("dcpext0", "memory-ready") && has("dcpext1", "memory-ready"));
    assert(!has("dcpext0", "display-ready") && !has("dcpext1", "display-ready"));
    world(NULL, "1", 2, -1); /* J514S DT: only the display flag is present. */
    assert(has("dcpext0", "display-ready") && has("dcpext1", "display-ready"));
    assert(ncalls == 4);
    for (int i = 0; i < 4; i++) assert(calls[i] == i);
    world("0", "1", 1, -1);
    assert(ncalls == 2 && calls[0] == 0 && calls[1] == 1);
    assert(has("dcpext0", "display-ready") && !has("dcpext1", "display-ready"));
    world("1", "1", 2, -1);
    assert(ncalls == 4 && has("dcpext1", "display-ready"));
    world(NULL, "1", 2, 0);
    assert(!has("dcpext0", "memory-ready") && !has("dcpext0", "display-ready"));
    assert(has("dcpext1", "display-ready"));
    world(NULL, "1", 2, 1);
    assert(has("dcpext0", "display-ready") && !has("dcpext1", "display-ready"));

    /*
     * T6031 is armed by its own flags and walks its own four processors, with the same
     * memory-before-DART order for each.
     */
    use(&soc_t6031, "asahi,t6031-dcpext", "asahi,t6031-dispext");
    world(NULL, "1", 4, -1);
    assert(dcpext6 == &dcpext6_socs[1] && dcpext6->insts == dcpext6_insts_t6031);
    assert(dcpext6->ninsts == 4);
    assert(ncalls == 8);
    for (int i = 0; i < 8; i++) assert(calls[i] == i);
    for (int i = 0; i < 4; i++) assert(has(names[i], "display-ready"));
    world("1", NULL, 4, -1);
    assert(ncalls == 4);
    for (int i = 0; i < 4; i++) assert(calls[i] == 2 * i);
    for (int i = 0; i < 4; i++)
        assert(has(names[i], "memory-ready") && !has(names[i], "display-ready"));
    /* dcpext1..3 are optional: only the processors the device tree has are handed off. */
    world(NULL, "1", 2, -1);
    assert(ncalls == 4);
    for (int i = 0; i < 4; i++) assert(calls[i] == i);
    assert(has("dcpext0", "display-ready") && has("dcpext1", "display-ready"));
    /* A refused later processor keeps its own DART and leaves the others' in place. */
    for (int reject = 1; reject < 4; reject++) {
        world(NULL, "1", 4, reject);
        assert(ncalls == 8);
        for (int i = 0; i < 8; i++) assert(calls[i] == i);
        for (int i = 0; i < 4; i++) {
            assert(has(names[i], "memory-ready") == (i != reject));
            assert(has(names[i], "display-ready") == (i != reject));
        }
    }

    /* Each SoC ignores the other's flags. */
    use(&soc_t6031, "asahi,t6030-dcpext", "asahi,t6030-dispext");
    world("1", "1", 4, -1);
    assert(ncalls == 0);
    use(&soc_t6030, "asahi,t6031-dcpext", "asahi,t6031-dispext");
    world("1", "1", 2, -1);
    assert(ncalls == 0);

    /* A SoC without external processors hands nothing off. */
    use(&soc_t8122, "asahi,t6030-dcpext", "asahi,t6030-dispext");
    world("1", "1", 2, -1);
    assert(!dcpext6 && ncalls == 0);
    return 0;
}
''')
