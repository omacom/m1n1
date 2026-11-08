/* SPDX-License-Identifier: MIT */
/*
 * Host test: smp_init_t8152() (the real function, extracted from src/smp.c by
 * tests/run-t8152-smp-bounds.sh) on a fake J873g ADT with 12 CPUs, built with the MAX_CPUS given
 * by -DMAX_CPUS. Below 12 it must refuse without touching the per-CPU arrays (ASan checks the
 * bounds); at 12 or more it must accept all 12 CPUs.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uint64_t u64;
typedef uint32_t u32;
typedef uint16_t u16;
typedef uint8_t u8;

#define BIT(n)  (1ULL << (n))
#define T8152   0x8152
#define MPIDR_EL1 0

struct spin_table {
    u64 mpidr;
    u64 flag;
};
struct cpu_info {
    bool valid;
    u8 die;
    u8 cluster;
    u8 core;
    u64 impl_reg;
};

static const void *adt = (void *)1;
static u32 board_id = 0x24;
static int cpu_nodes[MAX_CPUS];
static struct spin_table spin_table[MAX_CPUS];
static struct cpu_info cpu_info[MAX_CPUS];
static bool smp_initialized, wfe_mode;
static u64 cpu_start_base, boot_cpu_mpidr;
static int boot_cpu_idx = -1;

static bool in_el2(void)
{
    return true;
}
#define mrs(r)    ((u64)0) /* boot CPU 0 */
#define msr(r, v) ((void)(v))

enum { PMGR = 5, CPUS = 10, CPU0 = 100 };
static int adt_path_offset_trace(const void *a, const char *path, int *offsets)
{
    offsets[0] = PMGR;
    return strcmp(path, "/arm-io/pmgr-child") ? -1 : PMGR;
}
static int adt_path_offset(const void *a, const char *path)
{
    return strcmp(path, "/cpus") ? -1 : CPUS;
}
static bool adt_is_compatible(const void *a, int node, const char *c)
{
    return (node == 0 && !strcmp(c, "J873gAP")) || (node == PMGR && !strcmp(c, "pmgr2,t8152"));
}
static const void *adt_getprop(const void *a, int node, const char *name, u32 *len)
{
    static const u16 maps[] = {0, 0x128};
    if (node != PMGR || strcmp(name, "reg-maps"))
        return NULL;
    *len = sizeof(maps);
    return maps;
}
static int adt_get_reg(const void *a, int *path, const char *prop, int idx, u64 *base, u64 *size)
{
    *base = 0x300730000ULL;
    *size = 0x8000;
    return 0;
}
static int adt_get_child_count(const void *a, int node)
{
    return node == CPUS ? 12 : 0;
}
static int adt_first_child_offset(const void *a, int node)
{
    return CPU0;
}
static int adt_next_sibling_offset(const void *a, int node)
{
    return node + 1;
}
static int adt_getprop_copy(const void *a, int node, const char *name, void *val, size_t len)
{
    u32 id = node - CPU0;
    if (!strcmp(name, "cpu-id") && len == 4) {
        memcpy(val, &id, 4);
    } else if (!strcmp(name, "reg") && len == 4) {
        u32 reg = ((id / 6) << 8) | (id % 6);
        memcpy(val, &reg, 4);
    } else if (!strcmp(name, "cpu-impl-reg") && len == 16) {
        u64 impl[2] = {0x210000000ULL + id * 0x100000ULL, 0x10000};
        memcpy(val, impl, 16);
    } else {
        return -1;
    }
    return len;
}
#define ADT_GETPROP(a, n, name, val)       adt_getprop_copy(a, n, name, (val), sizeof(*(val)))
#define ADT_GETPROP_ARRAY(a, n, name, arr) adt_getprop_copy(a, n, name, (arr), sizeof(arr))
#define ADT_FOREACH_CHILD(a, node)                                                                 \
    for (int _child_count = adt_get_child_count(a, node); _child_count; _child_count = 0)          \
        for (node = adt_first_child_offset(a, node); _child_count--;                               \
             node = adt_next_sibling_offset(a, node))

#include "t8152_smp.inc"

int main(void)
{
    int ret = smp_init_t8152();
    bool ok = MAX_CPUS < 12 ? ret == -1 && !smp_initialized && boot_cpu_idx == -1
                            : ret == 0 && smp_initialized && boot_cpu_idx == 0;
    printf("MAX_CPUS=%d: smp_init_t8152() = %d (%s)\n", MAX_CPUS, ret,
           ok ? "ok" : "FAIL");
    return !ok;
}
