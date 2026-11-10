/* SPDX-License-Identifier: MIT */

#include "wdt.h"
#include "adt.h"
#include "boot_options.h"
#include "types.h"
#include "utils.h"

#define WDT_COUNT 0x10
#define WDT_ALARM 0x14
#define WDT_CTL   0x1c

static u64 wdt_base = 0;

void wdt_disable(void)
{
    int path[8];
    int node = adt_path_offset_trace(adt, "/arm-io/wdt", path);

    if (node < 0) {
        printf("WDT node not found!\n");
        return;
    }

    if (adt_get_reg(adt, path, "reg", 0, &wdt_base, NULL)) {
        printf("Failed to get WDT reg property!\n");
        return;
    }

    printf("Primary WDT register @ 0x%lx\n", wdt_base);
    /*
     * A chainloading stage that armed the watchdog for this one passes m1n1.wdt=keep in the
     * boot-args command line: leave both watchdogs as they are, so a hang before the OS takes the
     * watchdog over still resets the machine into that stage's fallback. wdt_reboot() works as
     * usual, since wdt_base is known.
     */
    if (m1n1_wdt_keep_enabled()) {
        printf("Primary WDT left armed (m1n1.wdt=keep)\n");
        return;
    }
    write32(wdt_base + WDT_CTL, 0);
    printf("Primary WDT disabled\n");

    // disable secondary watchdog if wdt-version is 2 or 3
    u32 wdt_version;
    if (ADT_GETPROP(adt, node, "wdt-version", &wdt_version) < 0)
        return;

    if (wdt_version == 2 || wdt_version == 3) {
        u64 wdt_2nd = 0;
        if (adt_get_reg(adt, path, "reg", 2, &wdt_2nd, NULL)) {
            printf("Failed to get WDT reg[2] property!\n");
            return;
        }

        printf("Secondary WDT register @ 0x%lx\n", wdt_2nd);
        write32(wdt_2nd, 0);
        printf("Secondary WDT disabled\n");
    }
}

void wdt_reboot(void)
{
    if (!wdt_base)
        return;

    write32(wdt_base + WDT_ALARM, 0x100000);
    write32(wdt_base + WDT_COUNT, 0);
    write32(wdt_base + WDT_CTL, 4);
}

int wdt_arm_seconds(unsigned seconds)
{
    int path[8];
    int node = adt_path_offset_trace(adt, "/arm-io/wdt", path);
    u64 primary, primary_size, secondary, secondary_size;
    u32 version;
    if (chip_id != T8140 || seconds < 1 || seconds > 178 || node < 0 ||
        ADT_GETPROP(adt, node, "wdt-version", &version) < 0 || version != 2 ||
        adt_get_reg(adt, path, "reg", 0, &primary, &primary_size) ||
        adt_get_reg(adt, path, "reg", 2, &secondary, &secondary_size) || (primary & 3) ||
        primary_size < 0x20 || secondary_size != 4 || read32(secondary))
        return -1;

    u32 alarm = seconds * 24000000u;
    write32(primary + WDT_CTL, 0);
    write32(primary + WDT_COUNT, 0);
    write32(primary + WDT_ALARM, alarm);
    write32(primary + WDT_CTL, 4);
    sysop("dsb sy");
    if (read32(primary + WDT_ALARM) != alarm || !(read32(primary + WDT_CTL) & 4))
        return -1;
    wdt_base = primary;
    return 0;
}
