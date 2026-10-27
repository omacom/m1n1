# SPDX-License-Identifier: MIT
"""M3 graphics on by default: dt_set_m3_defaults() against real libfdt root compatibles."""
from pathlib import Path

from test_siliconforest_handoff import definition, run_c

ROOT = Path(__file__).resolve().parents[2]


def between(source, start, end_marker):
    a = source.index(start)
    b = source.index(end_marker, a) + len(end_marker)
    return source[a:b]


def harness():
    source = (ROOT / "src/kboot.c").read_text()
    tables = between(source, "struct m3_default_board {", '#define M3_DEFAULTS_SWITCH "asahi,m3-defaults"\n')
    lookup = between(source, "static const char *kboot_chosen_param(const char *name)\n{", "\n}\n")
    drop = between(source, "static void dt_drop_m3_defaults(void)\n{", "\n}\n")
    setter = between(source, "int kboot_set_chosen(const char *name, const char *value)\n{", "\n}\n")
    deleter = between(source, "void kboot_delete_chosen(const char *name)\n{", "\n}\n")
    return (r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
#define MAX_CHOSEN_PARAMS 16
static char *chosen_params[MAX_CHOSEN_PARAMS][2];
static bool chosen_is_default[MAX_CHOSEN_PARAMS];
static bool chosen_lost;
static unsigned char blob[16384];
static void *dt = blob;
/* allocation fault injection: the n-th calloc from here on fails */
static int calloc_fail_in = -1;
static void *test_calloc(size_t n, size_t size)
{
    if (calloc_fail_in == 0) { calloc_fail_in = -1; return NULL; }
    if (calloc_fail_in > 0) calloc_fail_in--;
    return calloc(n, size);
}
#define calloc test_calloc
void kboot_delete_chosen(const char *name);
''' + setter + "\n" + deleter + "\n" + tables + lookup + "\n" + drop + "\n" +
        definition(source, "dt_set_m3_defaults") + r'''
/* the same write as dt_set_chosen(): value plus its NUL */
static void write_chosen(void)
{
    int node = fdt_path_offset(dt, "/chosen");
    assert(node >= 0);
    for (int i = 0; i < MAX_CHOSEN_PARAMS && chosen_params[i][0]; i++)
        assert(!fdt_setprop(dt, node, chosen_params[i][0], chosen_params[i][1],
                            (int)strlen(chosen_params[i][1]) + 1));
}
/* a fresh device tree, as each kboot_prepare_dt() opens one; chosen_params persist */
static void tree(const char *compat, int compat_len)
{
    assert(!fdt_create_empty_tree(dt, sizeof(blob)));
    assert(!fdt_setprop(dt, 0, "compatible", compat, compat_len));
    assert(fdt_add_subnode(dt, 0, "chosen") >= 0);
}
static void reset(const char *compat, int compat_len)
{
    dt_drop_m3_defaults();
    chosen_lost = false;     /* a new boot */
    for (int i = 0; i < MAX_CHOSEN_PARAMS; i++) {
        free(chosen_params[i][0]); free(chosen_params[i][1]);
        chosen_params[i][0] = chosen_params[i][1] = NULL;
    }
    tree(compat, compat_len);
}
#define RESET(c) reset(c, sizeof(c))
#define TREE(c) tree(c, sizeof(c))
static int entries(void)
{
    int n = 0;
    while (n < MAX_CHOSEN_PARAMS && chosen_params[n][0]) n++;
    return n;
}
/* what dcp6_armed()/g15_optin() accept: exactly the two bytes "1\0" */
static int armed(const char *name)
{
    int len = 0, node = fdt_path_offset(dt, "/chosen");
    const char *v = node >= 0 ? fdt_getprop(dt, node, name, &len) : NULL;
    return v && len == 2 && !strcmp(v, "1");
}
static int present(const char *name)
{
    int node = fdt_path_offset(dt, "/chosen");
    return fdt_getprop(dt, node, name, NULL) != NULL;
}
static int run(void) { int r = dt_set_m3_defaults(); write_chosen(); return r; }
static const char *air[] = {"asahi,t8122-dcp", "asahi,t8122-gpu", "asahi,t8122-gpu-power-standin",
                            "asahi,t8122-gpu-fuse-leakage"};
static const char *pro[] = {"asahi,t6030-dcp", "asahi,t6030-gpu"};
static const char *never[] = {"asahi,t8122-dcpext", "asahi,t6030-dcpext", "asahi,t6030-dispext",
                              "asahi,t6031-dcp", "asahi,t6031-gpu", "asahi,t8122-gpu-asc-preflight",
                              "asahi,t8122-gpu-handoff-diag", "asahi,m3-defaults"};
static void expect_set(const char **want, int n_want, const char **other, int n_other)
{
    for (int i = 0; i < n_want; i++) assert(armed(want[i]));
    for (int i = 0; i < n_other; i++) assert(!present(other[i]));
    for (int i = 0; i < 8; i++) assert(!present(never[i]));
}
static void expect_none(void)
{
    for (int i = 0; i < 4; i++) assert(!present(air[i]));
    for (int i = 0; i < 2; i++) assert(!present(pro[i]));
    for (int i = 0; i < 8; i++) assert(!present(never[i]));
}
int main(void)
{
    /* the four boards get their own recipe and nothing else */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform"); assert(!run()); expect_set(air, 4, pro, 2);
    RESET("apple,j615\0apple,t8122\0apple,arm-platform"); assert(!run()); expect_set(air, 4, pro, 2);
    RESET("apple,j516s\0apple,t6030\0apple,arm-platform"); assert(!run()); expect_set(pro, 2, air, 4);
    RESET("apple,j514s\0apple,t6030\0apple,arm-platform"); assert(!run()); expect_set(pro, 2, air, 4);

    /* M3 Max, unknown M3 boards, a board without its SoC, M1/M2: untouched */
    RESET("apple,j516c\0apple,t6031\0apple,arm-platform"); assert(!run()); expect_none();
    RESET("apple,j514c\0apple,t6031\0apple,arm-platform"); assert(!run()); expect_none();
    RESET("apple,j999\0apple,t6030\0apple,arm-platform"); assert(!run()); expect_none();
    RESET("apple,j999\0apple,t8122\0apple,arm-platform"); assert(!run()); expect_none();
    RESET("apple,j613\0apple,arm-platform"); assert(!run()); expect_none();
    RESET("apple,j516s\0apple,t8122\0apple,arm-platform"); assert(!run()); expect_none();
    RESET("apple,j293\0apple,t8103\0apple,arm-platform"); assert(!run()); expect_none();
    RESET("apple,j413\0apple,t8112\0apple,arm-platform"); assert(!run()); expect_none();

    /* an explicit config value wins: =0 keeps that switch off, the others still default on */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    assert(kboot_set_chosen("asahi,t8122-gpu", "0") >= 0);
    assert(!run());
    assert(armed("asahi,t8122-dcp") && !armed("asahi,t8122-gpu") && present("asahi,t8122-gpu"));
    assert(armed("asahi,t8122-gpu-power-standin") && armed("asahi,t8122-gpu-fuse-leakage"));
    RESET("apple,j516s\0apple,t6030\0apple,arm-platform");
    assert(kboot_set_chosen("asahi,t6030-dcp", "0") >= 0);
    assert(kboot_set_chosen("asahi,t6030-dcpext", "1") >= 0);   /* its own line, kept as is */
    assert(!run());
    assert(!armed("asahi,t6030-dcp") && armed("asahi,t6030-gpu") && armed("asahi,t6030-dcpext"));
    /* an explicit =1 stays one entry */
    RESET("apple,j516s\0apple,t6030\0apple,arm-platform");
    assert(kboot_set_chosen("asahi,t6030-gpu", "1") >= 0);
    assert(!run());
    assert(entries() == 2 && armed("asahi,t6030-dcp") && armed("asahi,t6030-gpu"));

    /* chosen.asahi,m3-defaults=0 turns every default off and keeps explicit lines */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    assert(kboot_set_chosen("asahi,m3-defaults", "0") >= 0);
    assert(kboot_set_chosen("asahi,t8122-dcp", "1") >= 0);
    assert(!run());
    assert(armed("asahi,t8122-dcp") && !present("asahi,t8122-gpu"));
    assert(!present("asahi,t8122-gpu-power-standin") && !present("asahi,t8122-gpu-fuse-leakage"));
    RESET("apple,j516s\0apple,t6030\0apple,arm-platform");
    assert(kboot_set_chosen("asahi,m3-defaults", "1") >= 0);
    assert(!run());
    assert(armed("asahi,t6030-dcp") && armed("asahi,t6030-gpu"));

    /* a short chosen table: all of the recipe or none of it, nothing overwritten */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    for (int i = 0; i < MAX_CHOSEN_PARAMS - 3; i++) {
        char name[32]; snprintf(name, sizeof(name), "test,filler-%d", i);
        assert(kboot_set_chosen(name, "x") >= 0);
    }
    assert(!run());
    for (int i = 0; i < 4; i++) assert(!present(air[i]));
    assert(entries() == MAX_CHOSEN_PARAMS - 3 && !strcmp(chosen_params[0][1], "x"));
    RESET("apple,j516s\0apple,t6030\0apple,arm-platform");      /* the Pro's two still fit */
    for (int i = 0; i < MAX_CHOSEN_PARAMS - 2; i++) {
        char name[32]; snprintf(name, sizeof(name), "test,filler-%d", i);
        assert(kboot_set_chosen(name, "x") >= 0);
    }
    assert(!run()); assert(armed("asahi,t6030-dcp") && armed("asahi,t6030-gpu"));

    /* a second preparation (proxy re-prepare) changes nothing */
    RESET("apple,j615\0apple,t8122\0apple,arm-platform");
    assert(!run()); TREE("apple,j615\0apple,t8122\0apple,arm-platform"); assert(!run());
    assert(entries() == 4);
    for (int i = 0; i < 4; i++) assert(armed(air[i]));

    /* the master switch set after a first preparation: the earlier defaults go */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    assert(!run());
    assert(kboot_set_chosen("asahi,m3-defaults", "0") >= 0);
    TREE("apple,j613\0apple,t8122\0apple,arm-platform"); assert(!run());
    for (int i = 0; i < 4; i++) assert(!present(air[i]));
    assert(entries() == 1);

    /* another device tree in the same process: no Air default carries over to a Pro */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    assert(!run());
    TREE("apple,j516s\0apple,t6030\0apple,arm-platform"); assert(!run());
    for (int i = 0; i < 4; i++) assert(!present(air[i]));
    assert(armed("asahi,t6030-dcp") && armed("asahi,t6030-gpu") && entries() == 2);
    TREE("apple,j516c\0apple,t6031\0apple,arm-platform"); assert(!run());
    expect_none(); assert(entries() == 0);

    /* an owner's later value for a defaulted switch is theirs and survives */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    assert(!run());
    assert(kboot_set_chosen("asahi,t8122-gpu", "0") >= 0);
    TREE("apple,j613\0apple,t8122\0apple,arm-platform"); assert(!run());
    assert(present("asahi,t8122-gpu") && !armed("asahi,t8122-gpu"));
    assert(armed("asahi,t8122-dcp") && armed("asahi,t8122-gpu-power-standin") &&
           armed("asahi,t8122-gpu-fuse-leakage") && entries() == 4);

    /* allocation failure part way through: no partial recipe, no crash */
    for (int fail = 0; fail < 8; fail++) {
        RESET("apple,j613\0apple,t8122\0apple,arm-platform");
        calloc_fail_in = fail;
        assert(!run());
        calloc_fail_in = -1;
        int on = 0;
        for (int i = 0; i < 4; i++) on += present(air[i]);
        assert(on == 0 || on == 4);
        assert(entries() == on);
        for (int i = 0; i < entries(); i++) assert(chosen_params[i][0] && chosen_params[i][1]);
    }
    /* kboot_set_chosen itself: a failed allocation leaves the table as it was */
    RESET("apple,j293\0apple,t8103\0apple,arm-platform");
    calloc_fail_in = 1;                                  /* the name, after the value */
    assert(kboot_set_chosen("test,x", "1") < 0);
    assert(entries() == 0 && chosen_lost);
    RESET("apple,j293\0apple,t8103\0apple,arm-platform");
    calloc_fail_in = 0;                                  /* the value */
    assert(kboot_set_chosen("test,x", "1") < 0);
    assert(entries() == 0 && chosen_lost);

    /* a failed overwrite keeps an owner's =0 (name, value and ownership), and the defaults stay off */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    assert(kboot_set_chosen("asahi,t8122-gpu", "0") >= 0);
    assert(!run());
    calloc_fail_in = 0;
    assert(kboot_set_chosen("asahi,t8122-gpu", "0") < 0);
    assert(entries() == 4 && !strcmp(chosen_params[0][0], "asahi,t8122-gpu") &&
           !strcmp(chosen_params[0][1], "0") && !chosen_is_default[0]);
    TREE("apple,j613\0apple,t8122\0apple,arm-platform"); assert(!run());
    assert(present("asahi,t8122-gpu") && !armed("asahi,t8122-gpu"));
    assert(!present("asahi,t8122-dcp") && entries() == 1);      /* fail closed: no defaults */

    /* an owner's =0 that could not be stored at all: no default goes in */
    for (int b = 0; b < 2; b++) {
        if (b) RESET("apple,j516s\0apple,t6030\0apple,arm-platform");
        else RESET("apple,j613\0apple,t8122\0apple,arm-platform");
        calloc_fail_in = 0;
        assert(kboot_set_chosen(b ? "asahi,t6030-gpu" : "asahi,t8122-gpu", "0") < 0);
        assert(!run());
        expect_none(); assert(entries() == 0);
    }
    /* nor when a config line was lost to a full table */
    RESET("apple,j516s\0apple,t6030\0apple,arm-platform");
    for (int i = 0; i < MAX_CHOSEN_PARAMS; i++) {
        char name[32]; snprintf(name, sizeof(name), "test,filler-%d", i);
        assert(kboot_set_chosen(name, "x") >= 0);
    }
    assert(kboot_set_chosen("asahi,t6030-gpu", "0") < 0 && chosen_lost);
    assert(!run());
    expect_none();

    /* the Air's display line at anything but 1 keeps the default GPU switches off */
    for (int b = 0; b < 2; b++) {
        if (b) RESET("apple,j615\0apple,t8122\0apple,arm-platform");
        else RESET("apple,j613\0apple,t8122\0apple,arm-platform");
        assert(kboot_set_chosen("asahi,t8122-dcp", "0") >= 0);
        assert(!run());
        assert(present("asahi,t8122-dcp") && !armed("asahi,t8122-dcp"));
        for (int i = 1; i < 4; i++) assert(!present(air[i]));
        assert(entries() == 1);
    }
    /* an owner's own GPU line beside it stays, with no default power switches added */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    assert(kboot_set_chosen("asahi,t8122-dcp", "0") >= 0);
    assert(kboot_set_chosen("asahi,t8122-gpu", "1") >= 0);
    assert(!run());
    assert(armed("asahi,t8122-gpu") && !present("asahi,t8122-gpu-power-standin"));
    assert(!present("asahi,t8122-gpu-fuse-leakage") && entries() == 2);
    /* the display line changed after a first preparation: =0 takes the GPU defaults away, =1 restores them */
    RESET("apple,j613\0apple,t8122\0apple,arm-platform");
    assert(!run()); assert(entries() == 4);
    assert(kboot_set_chosen("asahi,t8122-dcp", "0") >= 0);
    TREE("apple,j613\0apple,t8122\0apple,arm-platform"); assert(!run());
    for (int i = 1; i < 4; i++) assert(!present(air[i]));
    assert(entries() == 1);
    assert(kboot_set_chosen("asahi,t8122-dcp", "1") >= 0);
    TREE("apple,j613\0apple,t8122\0apple,arm-platform"); assert(!run());
    for (int i = 0; i < 4; i++) assert(armed(air[i]));
    assert(entries() == 4);
    /* the Pro keeps its GPU default beside a display line at 0 (tested above): no such rule there */
    puts("ok");
    return 0;
}
''')


def test_m3_graphics_defaults(tmp_path):
    run_c(tmp_path, "m3-graphics-defaults", harness())


def test_defaults_run_before_chosen_is_written():
    source = (ROOT / "src/kboot.c").read_text()
    prep = source[source.index("int kboot_prepare_dt(void *fdt)\n{"):]
    assert prep.index("dt_set_m3_defaults()") < prep.index("dt_set_chosen()")
    # and before every reader of the switches
    for reader in ("dt_set_display()", "dt_set_gpu(dt)", "dt_set_pmp_t6030()", "dt_set_pmp_t8122()"):
        assert prep.index("dt_set_m3_defaults()") < prep.index(reader)
