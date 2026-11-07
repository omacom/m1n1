/* SPDX-License-Identifier: MIT */
/*
 * T6050 N1 (Wi-Fi/Bluetooth, PCI 106b:1900-1903) on apcie0 port 0: the device-tree part of the
 * hand-off to Linux's adopting T6050 host (pcie-apple, apple,t6050-pcie) and the protocol-27
 * centauri drivers.
 *
 * For the apple,t6050-pcie host node, if the DT has one:
 *  - its N1 control function (pci106b,1900 below a root port) gets
 *      firmware-name              = "apple/centauri/<ADT target-type, lower case>-ftab.bin"
 *      apple,centauri-platform-id = (ADT /chosen board-id << 16) | ADT /chosen chip-id
 *      apple,centauri-protocol    = <27> unless the DT already sets it;
 *  - each root port marked apple,link-trained-by-bootloader gets the tunables Linux replays in its
 *    root-port reset (the N1's post-ROM port cycle) from the ADT pci-bridge<n> node, as
 *    (offset, 4, mask, value) groups: apple,apcie-config-tunables, apple,pcie-rc-tunables and
 *    apple,pcie-rc-gen{3,4,5}-shadow-tunables;
 *  - each such root port, and the DART its endpoint's RID maps to, is enabled only if the port
 *    passes the kernel's adoption preconditions: root complex running, port ready, link up and not
 *    retraining, LTSSM on, an Apple endpoint behind it, no RID route yet, the endpoint's command
 *    bits clear and the port's DART untouched. The bus numbers are written if the root port has
 *    none yet.
 *    Otherwise both stay disabled: Linux probes every enabled DART, and the DART of a port that is
 *    not up takes an SError.
 * The MAC addresses travel through the wifi0/bluetooth0 aliases (dt_set_mac_addresses()).
 * Nothing here trains a link; with no trained port the DT stays as it came, less the disabled
 * nodes it may have had enabled.
 */

#include "kboot_n1.h"
#include "adt.h"
#include "kboot.h"
#include "string.h"
#include "utils.h"

#include "libfdt/libfdt.h"

#define T6050_GP_PORTS 4

/* apcie0 "reg": shared windows, then six per port (see pcie_t6050.c) */
#define REG_ECAM         0
#define REG_RC           1
#define REG_PORT(p)      (16 + 6 * (p))
#define REG_PORT_LANE(p) (18 + 6 * (p))

#define CORE_RC_CTL        0x50
#define CORE_RC_CTL_RUN    BIT(0)
#define CORE_RC_STAT       0x58
#define CORE_RC_STAT_READY BIT(0)

#define PORT_LTSSMCTL     0x080
#define PORT_LINKSTS      0x208
#define PORT_LINKSTS_UP   BIT(0)
#define PORT_LINKSTS_BUSY BIT(2)
#define PORT_STATUS       0x804
#define PORT_STATUS_READY BIT(0)

#define PORT_RID2SID     0x3000
#define PORT_RID2SID_EN  BIT(31)
#define PORT_RID2SID_NUM 64

#define PCI_COMMAND     0x04
#define PCI_PRIMARY_BUS 0x18

/* DART registers that must still read 0 before Linux adopts a port (as Aura's stage G checks) */
#define DART_PROTECT_0 0x200
#define DART_PROTECT_1 0x208
#define DART_STREAMS   0xc00

static const char apcie_path[] = "/arm-io/apcie0";

struct adt_tunable_local {
    u32 offset;
    u32 size;
    u64 mask;
    u64 value;
} PACKED;

static int n1_set_control(void *dt, int host)
{
    int adt_chosen = adt_path_offset(adt, "/chosen");
    const char *target = adt_getprop(adt, 0, "target-type", NULL);
    u32 board_id, chip_id;
    char name[64], lower[16];

    if (adt_chosen < 0 || !target || ADT_GETPROP(adt, adt_chosen, "board-id", &board_id) < 0 ||
        ADT_GETPROP(adt, adt_chosen, "chip-id", &chip_id) < 0) {
        printf("n1: no target-type, board-id or chip-id in the ADT\n");
        return -1;
    }
    size_t i;
    for (i = 0; target[i] && i < sizeof(lower) - 1; i++)
        lower[i] = (target[i] >= 'A' && target[i] <= 'Z') ? target[i] + 32 : target[i];
    lower[i] = 0;
    snprintf(name, sizeof(name), "apple/centauri/%s-ftab.bin", lower);
    u32 platform_id = (board_id << 16) | (chip_id & 0xffff);

    int found = 0;
    for (int node = fdt_node_offset_by_compatible(dt, host, "pci106b,1900"); node >= 0;
         node = fdt_node_offset_by_compatible(dt, node, "pci106b,1900")) {
        /* only below this host */
        int parent = fdt_parent_offset(dt, node);
        if (parent < 0 || fdt_parent_offset(dt, parent) != host)
            continue;
        /* Fill only what the DT leaves unset: a DT value wins */
        bool have_name = fdt_getprop(dt, node, "firmware-name", NULL);
        bool have_id = fdt_getprop(dt, node, "apple,centauri-platform-id", NULL);
        if ((!have_name && fdt_setprop_string(dt, node, "firmware-name", name)) ||
            (!have_id && fdt_setprop_u32(dt, node, "apple,centauri-platform-id", platform_id)))
            return -1;
        if (!fdt_getprop(dt, node, "apple,centauri-protocol", NULL) &&
            fdt_setprop_u32(dt, node, "apple,centauri-protocol", 27))
            return -1;
        printf("n1: control function: firmware-name %s, platform-id 0x%08x%s\n",
               have_name ? "(DT value kept)" : name, platform_id,
               have_id ? " (DT value kept)" : "");
        found++;
    }
    if (!found)
        printf("n1: no pci106b,1900 control function below the T6050 host\n");
    return 0;
}

/* ADT pci-bridge<port> 24-byte tunable records -> (offset, 4, mask, value) cells on the DT port */
static int n1_copy_tunables(void *dt, int port_node, int port)
{
    static const char *const props[] = {
        "apcie-config-tunables",        "pcie-rc-tunables",
        "pcie-rc-gen3-shadow-tunables", "pcie-rc-gen4-shadow-tunables",
        "pcie-rc-gen5-shadow-tunables",
    };
    char bridge[48], dtname[48];

    snprintf(bridge, sizeof(bridge), "%s/pci-bridge%d", apcie_path, port);
    int anode = adt_path_offset(adt, bridge);
    if (anode < 0) {
        printf("n1: no %s\n", bridge);
        return 0;
    }
    for (size_t i = 0; i < ARRAY_SIZE(props); i++) {
        u32 len;
        const struct adt_tunable_local *t = adt_getprop(adt, anode, props[i], &len);
        if (!t || !len || len % sizeof(*t) || len / sizeof(*t) > 16) {
            printf("n1: %s %s missing or malformed, not copied\n", bridge, props[i]);
            continue;
        }
        u32 n = len / sizeof(*t);
        fdt32_t cells[16 * 4];
        bool ok = true;
        for (u32 k = 0; k < n; k++) {
            if (t[k].size != 4 || (t[k].offset & 3) || (t[k].mask >> 32) || (t[k].value >> 32))
                ok = false;
            cells[4 * k + 0] = cpu_to_fdt32(t[k].offset);
            cells[4 * k + 1] = cpu_to_fdt32(4);
            cells[4 * k + 2] = cpu_to_fdt32((u32)t[k].mask);
            cells[4 * k + 3] = cpu_to_fdt32((u32)t[k].value);
        }
        if (!ok) {
            printf("n1: %s %s has a record the kernel does not take, not copied\n", bridge,
                   props[i]);
            continue;
        }
        snprintf(dtname, sizeof(dtname), "apple,%s", props[i]);
        if (fdt_setprop(dt, port_node, dtname, cells, n * 4 * sizeof(fdt32_t)))
            return -1;
    }
    return 0;
}

/* The DART node apcie's iommu-map names for RID `rid`, or a negative libfdt error */
static int n1_iommu_for_rid(void *dt, int host, u32 rid)
{
    int len;
    const fdt32_t *map = fdt_getprop(dt, host, "iommu-map", &len);

    if (!map || len % (4 * sizeof(*map)))
        return -FDT_ERR_BADVALUE;
    for (int pos = 0; pos + 4 <= len / (int)sizeof(*map); pos += 4) {
        u32 base = fdt32_ld(&map[pos]), count = fdt32_ld(&map[pos + 3]);
        if (rid >= base && rid - base < count)
            return fdt_node_offset_by_phandle(dt, fdt32_ld(&map[pos + 1]));
    }
    return -FDT_ERR_NOTFOUND;
}

/*
 * 1 if port `port` is up as pcie-apple's adoption needs it, with its root port's buses numbered
 * (written here as primary 0, secondary = subordinate = `bus` if they were 0); 0 if not up.
 * Reads only, but for the bus numbers.
 */
static int n1_port_up(int port, u32 bus)
{
    int path[8];
    u64 ecam, rc, ctl, span;

    if (adt_path_offset_trace(adt, apcie_path, path) < 0 ||
        adt_get_reg(adt, path, "reg", REG_ECAM, &ecam, &span) || span < ((u64)(bus + 1) << 20) ||
        adt_get_reg(adt, path, "reg", REG_RC, &rc, &span) || span < CORE_RC_STAT + 4 ||
        adt_get_reg(adt, path, "reg", REG_PORT(port), &ctl, &span) ||
        span < PORT_RID2SID + 4 * PORT_RID2SID_NUM) {
        printf("n1: %s reg windows for port %d missing\n", apcie_path, port);
        return 0;
    }
    u32 rc_ctl = read32(rc + CORE_RC_CTL), rc_stat = read32(rc + CORE_RC_STAT);
    u32 status = read32(ctl + PORT_STATUS), linksts = read32(ctl + PORT_LINKSTS);
    u32 ltssm = read32(ctl + PORT_LTSSMCTL);
    printf("n1: port %d: rc ctl 0x%x stat 0x%x, port status 0x%x linksts 0x%x ltssm 0x%x\n", port,
           rc_ctl, rc_stat, status, linksts, ltssm);
    if (!(rc_ctl & CORE_RC_CTL_RUN) || !(rc_stat & CORE_RC_STAT_READY) ||
        !(status & PORT_STATUS_READY) ||
        (linksts & (PORT_LINKSTS_UP | PORT_LINKSTS_BUSY)) != PORT_LINKSTS_UP || !(ltssm & BIT(0)))
        return 0;

    u64 rp = ecam + ((u64)port << 15);
    u32 buses = read32(rp + PCI_PRIMARY_BUS);
    if (!((buses >> 8) & 0xff)) {
        u32 want = (buses & 0xff000000) | (bus << 16) | (bus << 8);
        write32(rp + PCI_PRIMARY_BUS, want);
        buses = read32(rp + PCI_PRIMARY_BUS);
        printf("n1: port %d: numbered the root port's buses: 0x%08x\n", port, buses);
    }
    if ((buses & 0xff) || ((buses >> 8) & 0xff) != bus || ((buses >> 16) & 0xff) != bus) {
        printf("n1: port %d: root port buses 0x%08x, not 0/%u/%u\n", port, buses, bus, bus);
        return 0;
    }
    u32 id = read32(ecam + ((u64)bus << 20));
    printf("n1: port %d: endpoint %02x:00.0 id 0x%08x\n", port, bus, id);
    if ((id & 0xffff) != 0x106b)
        return 0;

    /*
     * The rest of the adoption preconditions: no RID route yet, the endpoint's I/O, memory and
     * bus mastering off, and the port's DART untouched (no protection, no stream enabled).
     */
    for (int i = 0; i < PORT_RID2SID_NUM; i++) {
        if (read32(ctl + PORT_RID2SID + 4 * i) & PORT_RID2SID_EN) {
            printf("n1: port %d: RID route %d already valid\n", port, i);
            return 0;
        }
    }
    if (read32(ecam + ((u64)bus << 20) + PCI_COMMAND) & 7) {
        printf("n1: port %d: endpoint I/O, memory or bus mastering enabled\n", port);
        return 0;
    }
    char dart_name[32];
    int dart_path[8];
    u64 dart;
    snprintf(dart_name, sizeof(dart_name), "/arm-io/dart-apcie%d", port);
    if (adt_path_offset_trace(adt, dart_name, dart_path) < 0 ||
        adt_get_reg(adt, dart_path, "reg", 0, &dart, &span) || span < DART_STREAMS + 4) {
        printf("n1: port %d: no %s in the ADT\n", port, dart_name);
        return 0;
    }
    if (read32(dart + DART_PROTECT_0) || read32(dart + DART_PROTECT_1) ||
        read32(dart + DART_STREAMS)) {
        printf("n1: port %d: %s protection or streams not 0\n", port, dart_name);
        return 0;
    }
    return 1;
}

int kboot_setup_n1(void *dt)
{
    int host = fdt_node_offset_by_compatible(dt, -1, "apple,t6050-pcie");

    if (host < 0)
        return 0;
    if (adt_path_offset(adt, apcie_path) < 0) {
        printf("n1: no %s in the ADT\n", apcie_path);
        return 0;
    }
    if (n1_set_control(dt, host))
        return -1;

    /* Pass 1: the adopted root ports. Pass 2 re-finds every node, as each write moves offsets. */
    struct {
        char path[96];
        int port;
        u32 bus;
    } ports[T6050_GP_PORTS];
    int nports = 0, port_node;

    fdt_for_each_subnode(port_node, dt, host)
    {
        int len;
        const fdt32_t *reg = fdt_getprop(dt, port_node, "reg", &len);
        const fdt32_t *busr = fdt_getprop(dt, port_node, "bus-range", NULL);
        if (!reg || len < 4 ||
            !fdt_getprop(dt, port_node, "apple,link-trained-by-bootloader", NULL))
            continue;
        int port = (fdt32_ld(&reg[0]) >> 11) & 0x1f;
        if (port >= T6050_GP_PORTS || !busr || nports == T6050_GP_PORTS ||
            fdt_get_path(dt, port_node, ports[nports].path, sizeof(ports[nports].path))) {
            printf("n1: root port node for port %d unusable, skipped\n", port);
            continue;
        }
        ports[nports].port = port;
        ports[nports].bus = fdt32_ld(&busr[0]);
        nports++;
    }

    for (int i = 0; i < nports; i++) {
        int port = ports[i].port;
        u32 bus = ports[i].bus;
        bool up = n1_port_up(port, bus) == 1;
        const char *status = up ? "okay" : "disabled";

        port_node = fdt_path_offset(dt, ports[i].path);
        if (port_node < 0 || n1_copy_tunables(dt, port_node, port))
            return -1;
        port_node = fdt_path_offset(dt, ports[i].path);
        if (port_node < 0 || fdt_setprop_string(dt, port_node, "status", status))
            return -1;
        host = fdt_node_offset_by_compatible(dt, -1, "apple,t6050-pcie");
        int dart = host < 0 ? host : n1_iommu_for_rid(dt, host, bus << 8);
        if (dart >= 0 && fdt_setprop_string(dt, dart, "status", status))
            return -1;
        printf("n1: port %d %s: root port%s %s\n", port, up ? "up" : "not up",
               dart >= 0 ? " and DART" : " (no DART in iommu-map)", status);
    }
    return 0;
}
