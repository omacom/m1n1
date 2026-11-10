/* SPDX-License-Identifier: MIT */

#include "dapf.h"
#include "adt.h"
#include "assert.h"
#include "malloc.h"
#include "memory.h"
#include "pmgr.h"
#include "string.h"
#include "utils.h"

#include "libfdt/libfdt.h"

struct dapf_t8020_config {
    u64 start;
    u64 end;
    u8 unk1;
    u8 r0_hi;
    u8 r0_lo;
    u8 unk2;
    u32 r4;
} PACKED;

static int dapf_init_t8020(const char *path, u64 base, int node)
{
    u32 length;
    const char *prop = "filter-data-instance-0";
    const struct dapf_t8020_config *config = adt_getprop(adt, node, prop, &length);

    if (!config || !length || (length % sizeof(*config)) != 0) {
        printf("dapf: Error getting ADT node %s property %s.\n", path, prop);
        return -1;
    }

    int count = length / sizeof(*config);

    for (int i = 0; i < count; i++) {
        write32(base + 0x04, config[i].r4);
        write64(base + 0x08, config[i].start);
        write64(base + 0x10, config[i].end);
        write32(base + 0x00, (config[i].r0_hi << 4) | config[i].r0_lo);
        base += 0x40;
    }
    return 0;
}

struct dapf_t8110_config {
    u64 start;
    u64 end;
    u32 r20;
    u32 unk1;
    u32 r4;
    u32 unk2[5];
    u8 unk3;
    u8 r0_hi;
    u8 r0_lo;
    u8 unk4;
} PACKED;

struct dapf_t8110b_config {
    u64 start;
    u64 end;
    u32 r20;
    u32 unk1;
    u32 r4;
    u32 unk2[5];
    u8 unk3;
    u8 r0_hi;
    u8 r0_lo;
    u8 unk4;
    u32 pad;
} PACKED;

static int dapf_init_t8110a(u64 base, struct dapf_t8110_config *config, u32 length)
{
    int count = length / sizeof(*config);

    for (int i = 0; i < count; i++) {
        write32(base + 0x04, config[i].r4);
        write64(base + 0x08, config[i].start);
        write64(base + 0x10, config[i].end);
        write32(base + 0x00, (config[i].r0_hi << 4) | config[i].r0_lo);
        write32(base + 0x20, config[i].r20);
        base += 0x40;
    }
    return 0;
}

static int dapf_init_t8110b(u64 base, struct dapf_t8110b_config *config, u32 length)
{
    int count = length / sizeof(*config);

    for (int i = 0; i < count; i++) {
        write32(base + 0x04, config[i].r4);
        write64(base + 0x08, config[i].start);
        write64(base + 0x10, config[i].end);
        write32(base + 0x00, (config[i].r0_hi << 4) | config[i].r0_lo);
        write32(base + 0x20, config[i].r20);
        base += 0x40;
    }
    return 0;
}

static int dapf_init_t8110(const char *path, u64 base, int node)
{
    u32 length = 0;
    char prop[32];
    const void *config = NULL;
    int instance;

    for (instance = 0; instance < 8; instance++) {
        snprintf(prop, sizeof(prop), "dapf-instance-%d", instance);
        config = adt_getprop(adt, node, prop, &length);
        if (config && length)
            break;
    }

    if (instance == 8) {
        printf("dapf: no non-empty dapf-instance property on %s\n", path);
        return -1;
    }
    printf("dapf: %s uses dapf-instance-%d\n", path, instance);

    // The least common multiple of 52 and 56 is 728 which is in the range of
    // the observed lengthe for "dapf-instance-0". The 52 byte variant is more
    // common and prefering that works so far.
    if (length % sizeof(struct dapf_t8110_config) == 0) {
        return dapf_init_t8110a(base, (struct dapf_t8110_config *)config, length);
    } else if (length % sizeof(struct dapf_t8110b_config) == 0) {
        return dapf_init_t8110b(base, (struct dapf_t8110b_config *)config, length);
    } else {
        printf("dapf: Invalid length for %s property %s\n", path, prop);
        return -1;
    }
}

int dapf_init(const char *path, int index)
{
    int ret = -1;
    int dart_path[8];
    int node = adt_path_offset_trace(adt, path, dart_path);
    if (node < 0) {
        printf("dapf: Error getting DAPF %s node.\n", path);
        return -1;
    }

    u32 pwr;
    if (!adt_getprop(adt, node, "clock-gates", &pwr))
        pwr = 0;
    if (pwr && (pmgr_adt_power_enable(path) < 0))
        return -1;

    u64 base;
    if (adt_get_reg(adt, dart_path, "reg", index, &base, NULL) < 0) {
        printf("dapf: Error getting DAPF %s base address.\n", path);
        goto out;
    }

    if (adt_is_compatible(adt, node, "dart,t8020")) {
        ret = dapf_init_t8020(path, base, node);
    } else if (adt_is_compatible(adt, node, "dart,t6000")) {
        ret = dapf_init_t8020(path, base, node);
    } else if (adt_is_compatible(adt, node, "dart,t8110")) {
        ret = dapf_init_t8110(path, base, node);
    } else {
        printf("dapf: DAPF %s at 0x%lx is of an unknown type\n", path, base);
        goto out;
    }

out:
    if (pwr && pmgr_adt_power_disable(path)) {
        printf("dapf: failed to power down %s\n", path);
        ret = -1;
    }

    if (!ret)
        printf("dapf: Initialized %s\n", path);

    return ret;
}

struct entry {
    const char *path;
    int index;
    const char *alias;
};

struct entry dapf_entries[] = {
    /* Keep dart-ane out: its Linux driver owns that DAPF on T8140. */
    {"/arm-io/dart-aop", 1, "aop"},
    {"/arm-io/dart-mtp", 1, "mtp"},
    {"/arm-io/dart-pmp", 1, "pmp"},
    /* T6050: the PMP's DART is dart-pmp0 (dart,t8110; DAPF aperture reg[1], live-verified). */
    {"/arm-io/dart-pmp0", 1, "pmp0"},
    {"/arm-io/dart-isp", 5, "isp"},
    {"/arm-io/dart-isp0", 5, "isp"},
    {NULL, -1, NULL},
};

int dapf_init_fdt(const void *fdt)
{
    int ret = 0;
    int count = 0;
    struct entry *entry = dapf_entries;

    while (entry->path != NULL) {
        if (chip_id == T8140 && strcmp(entry->path, "/arm-io/dart-mtp") &&
            strcmp(entry->path, "/arm-io/dart-pmp")) {
            printf("dapf: preserving inherited %s state on T8140\n", entry->path);
            entry++;
            continue;
        }
        /* Only prepare DMA for devices handed to the next stage. In
         * particular, an initial J813 FDT has no AOP, MTP, PMP or ISP.
         * Their firmware-owned protection registers must remain intact.
         */
        if (fdt) {
            int node = fdt_path_offset(fdt, entry->alias);
            if (node < 0) {
                entry++;
                continue;
            }
            const char *status = fdt_getprop(fdt, node, "status", NULL);
            if (status && strcmp(status, "okay") && strcmp(status, "ok")) {
                entry++;
                continue;
            }
        }
        if (adt_path_offset(adt, entry->path) < 0) {
            entry++;
            continue;
        }
        if (dapf_init(entry->path, entry->index) < 0) {
            ret = -1;
        }
        entry++;
        count += 1;
    }
    return ret ? ret : count;
}

int dapf_init_all(void)
{
    return dapf_init_fdt(NULL);
}
