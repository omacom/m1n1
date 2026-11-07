/* SPDX-License-Identifier: MIT */
/*
 * T6050 apcie0 port 0 (the N1 Wi-Fi/Bluetooth) cold bring-up, for the J716s.
 *
 * Ported from Aura's src/pcie_t6050.c (aurora-silicon/aura PR #60/#61, Jeff Queisser), the
 * sequence measured on J775D's port 2 (RTL8127A) from the proxy, stages A, S and B-G. Changes:
 * the only port is 0, whose endpoint is the N1 in its ROM personality (106b:1900, function 0
 * only, links at Gen1); its PERST# pad, refclk-to-PERST# time, windows and tunables come from the
 * J716s ADT (apcie0 reg[16..21], pci-bridge0); optionally the N1's reset is released first
 * through the SMC (pci-bridge0 function-reset, pKW4 "gP13" template 0x00800000); the DT side is
 * kboot_n1.c, which enables the root port and its DART only if the link is up.
 *
 * iBoot leaves the GP side of apcie0 powered down on J775D and storage (ANS) live on the same
 * controller. This raises only the GP power closure, starts the shared GP PHY only while no GP
 * port has a link (A, S), then brings port 0 up (B-G). Writes meant to stick are read back; the
 * first unexpected value stops with no cleanup (a reboot is the cleanup). Storage (ANS, NVMe, its
 * SART and power) is snapshotted before and compared after each stage that touches shared state.
 *
 * Only run when the payload sets chosen.asahi,t6050-n1. On the J716s, =4 (ANS started, SMC pulse)
 * brings the link up at Gen1 x1 with the N1 at 01:00.0; without the SMC pulse the link did not
 * come up within 500 ms (the N1 stays in reset).
 *
 * =3 (=4 with the SMC release) is for a boot chain whose chainloading stage-1 reads the ESP and
 * then stops ANS, so this stage finds it stopped and the check above refuses.
 * pcie_t6050_n1_train_ans() starts ANS with nvme_init() and reads a block, trains as =1/=2 with
 * storage live, then requires the same register state and the same block, and stops ANS again
 * with nvme_stop() (also on M4 and later), so Linux gets ANS as that stage-1 left it and no RTKit
 * buffer of this session stays live. If storage is in doubt, Linux could not mount its root, so
 * this resets the SoC through the watchdog instead and iBoot counts a failed boot. With ANS
 * already running, =3/=4 train as =1/=2.
 *
 * With the SMC release (=2, =4) the N1's reset is pulsed through the SMC as Aura's J813 code does
 * (aurora-silicon/aura 21c3f707e0, src/j813_n1.c): pci-bridge0 function-reset is
 * pKW4("gP13", 0x00800000) on the J716s as on J813; write template | 0, 50 ms, template | 1,
 * 50 ms, then 100 ms before the port reset, and allow 1 s for link up.
 */

#include "pcie_t6050.h"
#include "adt.h"
#include "malloc.h"
#include "nvme.h"
#include "pmgr.h"
#include "rtkit.h"
#include "smc.h"
#include "string.h"
#include "tunables.h"
#include "utils.h"
#include "wdt.h"

#define T6050_GP_PORTS 4

static const struct t6050_port {
    int port;
    int bus; /* its root port's secondary bus */
    const char *endpoint;
} n1_port = {0, 1, "N1"};

/* apcie0 "reg": shared windows, then six per port */
#define REG_ECAM          0
#define REG_RC            1
#define REG_PHY           2
#define REG_PHY_CTL       3
#define REG_AXI2AF        5
#define REG_AUS5_TOP      10
#define REG_AUS5_AON      11
#define REG_PORT(p)       (16 + 6 * (p))
#define REG_PORT_LANE(p)  (18 + 6 * (p))
#define REG_PORT_I2AXI(p) (19 + 6 * (p))
#define REG_COUNT         (16 + 6 * T6050_GP_PORTS)

/* Port registers */
#define PORT_LTSSMCTL   0x080
#define PORT_CLKRST     0x0a8
#define PORT_MSICFG     0x124
#define PORT_LINKSTS    0x208
#define PORT_APPCLK     0x800
#define PORT_STATUS     0x804
#define PORT_INT_RESET  0x82c
#define PORT_RID2SID    0x3000
#define PORT_RID2SID_EN BIT(31)

/* Root port DBI */
#define DBI_GEN3_EQ    0x194
#define DBI_LINK_WIDTH 0x710
#define DBI_GEN2_CTRL  0x80c
#define DBI_SHADOW_SEL 0x890
#define DBI_RO_WR_EN   0x8bc

/* PCI config space */
#define PCI_COMMAND               0x04
#define PCI_PRIMARY_BUS           0x18
#define PCI_CAPABILITY_LIST       0x34
#define PCI_CAP_ID_EXP            0x10
#define PCI_EXP_LNKCAP            0x0c
#define PCI_EXP_LNKCTL            0x10
#define PCI_EXP_LNKSTA            0x12
#define PCI_EXP_LNKCAP2           0x2c
#define PCI_EXP_LNKCAP2_SLS_8_0GB BIT(3)
#define PCI_EXP_LNKCTL2           0x30
#define PCI_CFG_SIZE              0x1000

#define GPIO_PERST_MASK  0x27f
#define GPIO_PERST_LOW   0x202
#define GPIO_PERST_LOCK  BIT(21)
#define GPIO_FOURCC_GPIO 0x4750494f /* "OIPG" in ADT byte order */
#define SMC_FOURCC_PKW4  0x704b5734 /* "4WKp" in ADT byte order: pKW4 */

static const char apcie_path[] = "/arm-io/apcie0";

static struct {
    u64 ecam, rc, phy, phy_ctl, axi2af, aus5_top, aus5_aon;
    u64 port[T6050_GP_PORTS]; /* every GP port's control block, for the link checks in S */
    u64 phy_span, axi2af_span;
} r;

static struct port_regs {
    const struct t6050_port *p;
    char bridge[40];
    u64 ctl, ctl_span, lane, i2axi;
    u64 rp; /* root port 00:0<port>.0 config space */
    u64 perst;
    u32 refclk_to_perst_us;
    u64 dart;
    u32 reset_key, reset_template; /* SMC pKW4 key and template of pci-bridge0 function-reset */
} pr;

/* AXI2AF records whose written bit reads back 0, identical in every J775D proxy run: bit 31 at
 * 0x0-0x3c and 0x410-0x440, bit 23 at 0x310-0x340. Built at run time. */
static struct tunable_excuse axi2af_excuses[16 + 13 + 13 + 1];

static void axi2af_excuses_init(void)
{
    int n = 0;
    for (u32 o = 0x000; o <= 0x03c; o += 4)
        axi2af_excuses[n++] = (struct tunable_excuse){o, BIT(31)};
    for (u32 o = 0x310; o <= 0x340; o += 4)
        axi2af_excuses[n++] = (struct tunable_excuse){o, BIT(23)};
    for (u32 o = 0x410; o <= 0x440; o += 4)
        axi2af_excuses[n++] = (struct tunable_excuse){o, BIT(31)};
    axi2af_excuses[n] = (struct tunable_excuse){0, 0};
}

/* One copy of the prefix, shared by every stop message */
static const char stop_prefix[] = "pcie-t6050: STOP: ";
#define FAIL(...)                                                                                  \
    do {                                                                                           \
        printf("%s", stop_prefix);                                                                 \
        printf(__VA_ARGS__);                                                                       \
        return -1;                                                                                 \
    } while (0)

#define TRY(x)                                                                                     \
    do {                                                                                           \
        int _ret = (x);                                                                            \
        if (_ret < 0)                                                                              \
            return _ret == N1_STORAGE_LOST ? _ret : -1;                                            \
    } while (0)

static int wait32(const char *what, u64 addr, u32 mask, u32 want, bool equal, u32 ms)
{
    u64 timeout = timeout_calculate(ms * 1000);
    u32 v;

    while (true) {
        v = read32(addr);
        if (((v & mask) == want) == equal)
            return 0;
        if (timeout_expired(timeout))
            FAIL("%s: 0x%lx = 0x%08x after %u ms\n", what, addr, v, ms);
        udelay(10);
    }
}

#define poll_set(what, addr, bits, ms)   wait32(what, addr, bits, bits, true, ms)
#define poll_clear(what, addr, bits, ms) wait32(what, addr, bits, 0, true, ms)

/* Clear `clear`, set `set`, and require exactly that under both masks */
static int rmw(const char *what, u64 addr, u32 clear, u32 set)
{
    u32 old = read32(addr);
    write32(addr, (old & ~clear) | set);
    u32 v = read32(addr);
    if ((v & (clear | set)) != set)
        FAIL("%s: 0x%lx 0x%08x -> 0x%08x\n", what, addr, old, v);
    return 0;
}

/* The Gen3/4/5 shadows behind DBI_SHADOW_SEL do not read back through the same offsets */
#define NO_CHECK ((const struct tunable_excuse *)-1)

static int apply_tunables(const char *path, const char *prop, u64 base, u64 span,
                          const struct tunable_excuse *excuses)
{
    if (tunables_validate_local(path, prop, span))
        FAIL("%s: missing or out of its window\n", prop);
    if (tunables_apply_local_addr(path, prop, base))
        FAIL("%s: not applied\n", prop);
    if (excuses != NO_CHECK && tunables_check_local_addr(path, prop, base, excuses))
        FAIL("%s: readback differs\n", prop);
    return 0;
}

static int find_regs(void)
{
    int path_nodes[8];

    int node = adt_path_offset_trace(adt, apcie_path, path_nodes);
    u32 reg_len = 0;
    if (node < 0 || !adt_is_compatible(adt, node, "apcie,t6050"))
        FAIL("no apcie,t6050 at %s\n", apcie_path);
    if (!adt_getprop(adt, node, "reg", &reg_len) || reg_len != 16 * REG_COUNT)
        FAIL("%s has %u reg bytes, expected %u\n", apcie_path, reg_len, 16 * REG_COUNT);
    u32 lane_cfg;
    if (ADT_GETPROP(adt, node, "lane-cfg", &lane_cfg) < 0 || lane_cfg)
        FAIL("lane-cfg is not 0\n");

    u64 unused;
    struct {
        u64 *dst, *span;
        int idx;
        u64 min_span;
    } regs[] = {
        {&r.ecam, &unused, REG_ECAM, (u64)(n1_port.bus + 1) << 20},
        {&r.rc, &unused, REG_RC, 0x60},
        {&r.phy, &r.phy_span, REG_PHY, 0x4004},
        {&r.phy_ctl, &unused, REG_PHY_CTL, 0xc},
        {&r.axi2af, &r.axi2af_span, REG_AXI2AF, 0x444},
        {&r.aus5_top, &unused, REG_AUS5_TOP, 0x98},
        {&r.aus5_aon, &unused, REG_AUS5_AON, 0x60},
        {&r.port[0], &unused, REG_PORT(0), PORT_LINKSTS + 4},
        {&r.port[1], &unused, REG_PORT(1), PORT_LINKSTS + 4},
        {&r.port[2], &unused, REG_PORT(2), PORT_LINKSTS + 4},
        {&r.port[3], &unused, REG_PORT(3), PORT_LINKSTS + 4},
    };
    for (size_t i = 0; i < ARRAY_SIZE(regs); i++)
        if (adt_get_reg(adt, path_nodes, "reg", regs[i].idx, regs[i].dst, regs[i].span) ||
            !*regs[i].dst || *regs[i].span < regs[i].min_span)
            FAIL("apcie0 reg[%d] missing or short\n", regs[i].idx);
    return 0;
}

/* The port's own windows, PERST# pad, refclk-to-PERST# time, DART and SMC reset, from the ADT */
static int find_port_regs(struct port_regs *pp)
{
    int path_nodes[8], gpio_path[8], dart_path[8];
    u64 unused, span;
    int n = pp->p->port;

    if (adt_path_offset_trace(adt, apcie_path, path_nodes) < 0)
        FAIL("no %s\n", apcie_path);
    struct {
        u64 *dst, *span;
        int idx;
        u64 min_span;
    } regs[] = {
        {&pp->ctl, &pp->ctl_span, REG_PORT(n), PORT_RID2SID + 64 * 4},
        {&pp->lane, &unused, REG_PORT_LANE(n), 0x4},
        {&pp->i2axi, &unused, REG_PORT_I2AXI(n), 0x84},
    };
    for (size_t i = 0; i < ARRAY_SIZE(regs); i++)
        if (adt_get_reg(adt, path_nodes, "reg", regs[i].idx, regs[i].dst, regs[i].span) ||
            !*regs[i].dst || *regs[i].span < regs[i].min_span)
            FAIL("apcie0 reg[%d] missing or short\n", regs[i].idx);
    pp->rp = r.ecam + ((u64)n << 15);

    /* PERST#: pci-bridge<n> function-perst = GPIO(pin) of /arm-io/gpio0 */
    snprintf(pp->bridge, sizeof(pp->bridge), "%s/pci-bridge%d", apcie_path, n);
    struct {
        u32 phandle, fourcc, pin, flags;
    } perst, reset;
    int bridge = adt_path_offset(adt, pp->bridge);
    int gpio = adt_path_offset_trace(adt, "/arm-io/gpio0", gpio_path);
    u32 gpio_phandle, apcie_port;
    u64 gpio_base;
    if (bridge < 0 || ADT_GETPROP(adt, bridge, "apcie-port", &apcie_port) < 0 ||
        apcie_port != (u32)n)
        FAIL("%s is not apcie port %d\n", pp->bridge, n);
    if (!adt_getprop(adt, bridge, "centauri-builtin", NULL))
        FAIL("%s has no centauri-builtin: not the N1's port\n", pp->bridge);
    if (gpio < 0 || adt_getprop_copy(adt, bridge, "function-perst", &perst, sizeof(perst)) < 0 ||
        perst.fourcc != GPIO_FOURCC_GPIO ||
        ADT_GETPROP(adt, gpio, "AAPL,phandle", &gpio_phandle) < 0 ||
        gpio_phandle != perst.phandle || adt_get_reg(adt, gpio_path, "reg", 0, &gpio_base, &span) ||
        span < 4 * (perst.pin + 1))
        FAIL("cannot resolve %s function-perst\n", pp->bridge);
    pp->perst = gpio_base + 4 * perst.pin;
    if (ADT_GETPROP(adt, bridge, "t-refclk-to-perst", &pp->refclk_to_perst_us) < 0)
        FAIL("%s has no t-refclk-to-perst\n", pp->bridge);
    /* function-reset = pKW4(key, template) on the SMC PMU nub (J716s: "gP13", 0x00800000);
     * adt_getprop_copy() returns the length on success */
    int reset_len = adt_getprop_copy(adt, bridge, "function-reset", &reset, sizeof(reset));
    if (reset_len == (int)sizeof(reset) && reset.fourcc == SMC_FOURCC_PKW4) {
        pp->reset_key = reset.pin;
        pp->reset_template = reset.flags;
    }

    char dart_name[32];
    snprintf(dart_name, sizeof(dart_name), "/arm-io/dart-apcie%d", n);
    if (adt_path_offset_trace(adt, dart_name, dart_path) < 0 ||
        adt_get_reg(adt, dart_path, "reg", 0, &pp->dart, &span) || span < 0xc04)
        FAIL("no %s\n", dart_name);
    return 0;
}

/* Storage as iBoot left it, read only (never the mailbox payloads) */
#define SART_SLOTS 16
enum {
    ST_ANS_CTRL,
    ST_ANS_STATUS,
    ST_BOOT_STATUS,
    ST_CC,
    ST_CSTS,
    ST_A2I,
    ST_I2A,
    ST_PS,
    ST_SART = ST_PS + 6,
    ST_COUNT = ST_SART + 3 * SART_SLOTS,
};
static const char *const storage_ps[] = {"APCIE_ST0", "APCIE_ST1",     "FAB6_SOC",
                                         "ANS",       "APCIE_SYS_ST0", "APCIE_SYS_ST1"};
static u64 storage_addr[ST_COUNT];
static u32 storage_ref[ST_COUNT];
static bool storage_ref_valid; /* the reference passed its own checks */
static bool storage_have_ref;
#define NVME_BOOT_STATUS_OK 0xde71ce55

static int storage_find(void)
{
    int path_nodes[8];
    u64 ans, nvme, sart, span;

    if (adt_path_offset_trace(adt, "/arm-io/ans", path_nodes) < 0 ||
        adt_get_reg(adt, path_nodes, "reg", 0, &ans, &span) || span < 0x8118 ||
        adt_get_reg(adt, path_nodes, "reg", 9, &nvme, &span) || span < 0x1304)
        FAIL("no /arm-io/ans\n");
    if (adt_path_offset_trace(adt, "/arm-io/sart-ans", path_nodes) < 0 ||
        adt_get_reg(adt, path_nodes, "reg", 0, &sart, &span) || span < 0xc0 + 4 * SART_SLOTS)
        FAIL("no /arm-io/sart-ans\n");
    storage_addr[ST_ANS_CTRL] = ans + 0x44;
    storage_addr[ST_ANS_STATUS] = ans + 0x48;
    storage_addr[ST_A2I] = ans + 0x8110;
    storage_addr[ST_I2A] = ans + 0x8114;
    storage_addr[ST_BOOT_STATUS] = nvme + 0x1300;
    storage_addr[ST_CC] = nvme + 0x14;
    storage_addr[ST_CSTS] = nvme + 0x1c;
    for (int i = 0; i < (int)ARRAY_SIZE(storage_ps); i++)
        if (!(storage_addr[ST_PS + i] = pmgr_psreg_by_name(0, storage_ps[i])))
            FAIL("no PMGR device %s\n", storage_ps[i]);
    for (int i = 0; i < SART_SLOTS; i++)
        for (int k = 0; k < 3; k++)
            storage_addr[ST_SART + 3 * i + k] = sart + 0x60 * k + 4 * i;
    return 0;
}

/* Before the training a failed check only refuses it; after a stage it means storage is in doubt */
#define STORAGE_FAIL(...)                                                                          \
    do {                                                                                           \
        printf("%s", stop_prefix);                                                                 \
        printf(__VA_ARGS__);                                                                       \
        return reference ? -1 : N1_STORAGE_LOST;                                                   \
    } while (0)

static int storage_check(const char *tag, bool reference)
{
    u32 cur[ST_COUNT];
    int changed = 0;

    for (int i = 0; i < ST_COUNT; i++)
        cur[i] = read32(storage_addr[i]);
    if (reference) {
        memcpy(storage_ref, cur, sizeof(cur));
        storage_have_ref = true;
    }
    for (int i = 0; i < ST_COUNT && !reference; i++) {
        if (cur[i] == storage_ref[i])
            continue;
        printf("pcie-t6050: storage %s: 0x%lx 0x%08x -> 0x%08x\n", tag, storage_addr[i],
               storage_ref[i], cur[i]);
        changed++;
        /* PS actual state and was-gated flags may move; target and auto must not */
        if (i >= ST_PS && i < ST_SART && !((cur[i] ^ storage_ref[i]) & ~0x3f0))
            continue;
        if (i >= ST_PS || i == ST_BOOT_STATUS || i == ST_CC)
            STORAGE_FAIL("storage changed %s\n", tag);
    }
    if (!(cur[ST_ANS_CTRL] & BIT(4)))
        STORAGE_FAIL("ANS is not running (%s)\n", tag);
    if (cur[ST_BOOT_STATUS] != NVME_BOOT_STATUS_OK)
        STORAGE_FAIL("NVMe BOOT_STATUS 0x%x (%s)\n", cur[ST_BOOT_STATUS], tag);
    if (cur[ST_CSTS] & BIT(1))
        STORAGE_FAIL("NVMe CSTS fatal (%s)\n", tag);
    if (reference)
        storage_ref_valid = true;
    printf("pcie-t6050: storage %s: %s\n", tag,
           reference ? "reference"
           : changed ? "allowed"
                     : "unchanged");
    return 0;
#undef STORAGE_FAIL
}

/* A: the GP power closure; the rest of it must already be active */
static int stage_a(void)
{
    static const char *const keep[] = {"GPIO", "APCIE_GP", "FAB6_SOC", "APHY_GP_AUS5_A"};
    static const char *const raise[] = {"APCIE_SYS_GP", "APCIE_PHY_SW", "APHY_GP_AUS5"};

    /* The port still to be brought up is held in reset as iBoot leaves it */
    if (!(read32(pr.ctl + PORT_LINKSTS) & BIT(0)) &&
        (read32(pr.perst) & GPIO_PERST_MASK) != GPIO_PERST_LOW)
        FAIL("port %d PERST# is not output-low: 0x%x\n", pr.p->port, read32(pr.perst));
    for (size_t i = 0; i < ARRAY_SIZE(keep); i++)
        if (pmgr_power_is_on(0, keep[i]) != 1)
            FAIL("%s is not active, and it is not ours to raise\n", keep[i]);
    for (size_t i = 0; i < ARRAY_SIZE(raise); i++) {
        uintptr_t ps = pmgr_psreg_by_name(0, raise[i]);
        if (!ps || pmgr_set_mode(ps, 0xf) || read32(ps) & BIT(28))
            FAIL("%s did not come up with auto-PM off\n", raise[i]);
    }
    /* macOS waits here */
    return poll_clear("AUS5 top +0x94 bit 1", r.aus5_top + 0x94, BIT(1), 100);
}

/* S: the shared GP init, Apple's post-iBoot GP order; only with no GP port linked */
static int stage_s(void)
{
    for (int p = 0; p < T6050_GP_PORTS; p++)
        if (read32(r.port[p] + PORT_LINKSTS) & BIT(0))
            FAIL("GP port %d has a link\n", p);
    if (read32(r.phy_ctl + 0x08))
        FAIL("shared PHY status 0x%x: not cold\n", read32(r.phy_ctl + 0x08));

    TRY(rmw("AUS5 aon +0x5c bit 2", r.aus5_aon + 0x5c, 0, BIT(2)));
    TRY(apply_tunables(apcie_path, "apcie-axi2af-tunables", r.axi2af, r.axi2af_span,
                       axi2af_excuses));
    write32(r.rc + 0x04, 0); /* lane-cfg 0 */
    if (read32(r.rc + 0x04))
        FAIL("lane cfg did not take\n");
    TRY(apply_tunables(apcie_path, "apcie-phy-tunables", r.phy, r.phy_span, NULL));
    TRY(poll_set("refclk", r.phy + 0x4000, BIT(31), 250));
    /* Gen5 PHY start */
    TRY(rmw("H+0x04 bit 4", r.phy_ctl + 0x04, 0, BIT(4)));
    TRY(poll_set("H+0x08 bit 4", r.phy_ctl + 0x08, BIT(4), 250));
    TRY(rmw("H+0x04 bit 5 clear", r.phy_ctl + 0x04, BIT(5), 0));
    TRY(poll_set("H+0x08 bit 0", r.phy_ctl + 0x08, BIT(0), 250));
    TRY(rmw("refclk enable", r.phy + 0x4000, 0, BIT(0)));
    TRY(rmw("H+0x00 bit 27", r.phy_ctl + 0x00, 0, BIT(27)));
    write32(r.rc + 0x54, 0x140);
    TRY(rmw("RC run", r.rc + 0x50, 0, BIT(0)));
    return poll_set("RC ready", r.rc + 0x58, BIT(0), 250);
}

/* B: the port-local reset table; port interrupts masked, RID and MSI tables untouched */
static int stage_b(const struct port_regs *pp)
{
    static const u32 table[][2] = {
        {0x088, 0x110},   {0x100, 0xffffffff}, {0x148, 0xffffffff}, {0x210, 0xffffffff},
        {0x080, 0},       {0x084, 0},          {0x104, 0xffffffff}, {0x124, 0x100},
        {0x16c, 0},       {0x13c, 0},          {0x800, 0x100100},   {0x808, 0x1001ff},
        {0x82c, 0x10000}, {0x130, 0x03020000}, {0x140, 0x10},       {0x144, 0x253770},
        {0x21c, 0},       {0x834, 0},          {0x83c, 0},
    };
    u64 p2 = pp->ctl;

    if (read32(p2 + PORT_LINKSTS) & BIT(0) || read32(p2 + PORT_LTSSMCTL) & BIT(0))
        FAIL("port %d has a link or LTSSM running\n", pp->p->port);
    if (!(read32(r.phy_ctl + 0x08) & BIT(4)) || !(read32(r.rc + 0x58) & BIT(0)))
        FAIL("shared GP init not in place\n");
    for (size_t i = 0; i < ARRAY_SIZE(table); i++)
        write32(p2 + table[i][0], table[i][1]);
    if (read32(p2 + PORT_LTSSMCTL) & BIT(0) || read32(p2 + PORT_MSICFG) & BIT(0) ||
        read32(p2 + PORT_APPCLK) & BIT(0) || !(read32(p2 + PORT_INT_RESET) & BIT(16)) ||
        read32(p2 + PORT_INT_RESET) & BIT(0))
        FAIL("port reset table did not take\n");
    return 0;
}

/* C: port config tunables, app clock, internal reset handshake */
static int stage_c(const struct port_regs *pp)
{
    u64 p2 = pp->ctl;

    TRY(apply_tunables(pp->bridge, "apcie-config-tunables", p2, pp->ctl_span, NULL));
    TRY(rmw("APPCLK", p2 + PORT_APPCLK, 0, BIT(0)));
    TRY(wait32("clk/rst bit 0 or 16 clear", p2 + PORT_CLKRST, BIT(0) | BIT(16), BIT(0) | BIT(16),
               false, 10));
    TRY(rmw("internal reset bit 0", p2 + PORT_INT_RESET, 0, BIT(0)));
    TRY(poll_set("clk/rst bit 0", p2 + PORT_CLKRST, BIT(0), 20));
    TRY(rmw("internal reset bit 16 clear", p2 + PORT_INT_RESET, BIT(16), 0));
    TRY(poll_set("port ready", p2 + PORT_STATUS, BIT(0), 250));
    TRY(rmw("APPCLK bit 8", p2 + PORT_APPCLK, 0, BIT(8)));
    return poll_clear("port not busy", p2 + PORT_LINKSTS, BIT(2), 250);
}

static int find_pcie_cap(u64 rp)
{
    u32 cap = read32(rp + PCI_CAPABILITY_LIST) & 0xfc;

    for (int i = 0; i < 48 && cap >= 0x40 && !(cap & 3); i++) {
        u32 hdr = read32(rp + cap);
        if ((hdr & 0xff) == PCI_CAP_ID_EXP)
            return cap;
        cap = (hdr >> 8) & 0xfc;
    }
    FAIL("no PCI Express capability on the root port\n");
}

/* D: root-port DBI tunables and the Gen3 x1 policy, in the order Apple's driver uses */
static int stage_d(const struct port_regs *pp)
{
    static const struct {
        u32 sel;
        const char *prop;
    } shadows[] = {
        {0, "pcie-rc-gen3-shadow-tunables"},
        {1 << 24, "pcie-rc-gen4-shadow-tunables"},
        {0, "pcie-rc-gen5-shadow-tunables"},
    };
    u64 rp = pp->rp;
    int cap = find_pcie_cap(rp);
    TRY(cap);

    TRY(rmw("DBI write enable", rp + DBI_RO_WR_EN, 0, BIT(0)));
    TRY(apply_tunables(pp->bridge, "pcie-rc-tunables", rp, PCI_CFG_SIZE, NULL));
    for (size_t i = 0; i < ARRAY_SIZE(shadows); i++) {
        TRY(rmw("shadow select", rp + DBI_SHADOW_SEL, 3 << 24, shadows[i].sel));
        TRY(apply_tunables(pp->bridge, shadows[i].prop, rp, PCI_CFG_SIZE, NO_CHECK));
    }
    TRY(rmw("Gen3 EQ", rp + DBI_GEN3_EQ, 0, BIT(0)));
    if (!(read32(rp + cap + PCI_EXP_LNKCAP2) & PCI_EXP_LNKCAP2_SLS_8_0GB))
        FAIL("Gen3 not advertised\n");
    u16 lnkctl2 = read16(rp + cap + PCI_EXP_LNKCTL2);
    write16(rp + cap + PCI_EXP_LNKCTL2, (lnkctl2 & ~0xf) | 3);
    if ((read16(rp + cap + PCI_EXP_LNKCTL2) & 0xf) != 3)
        FAIL("target link speed did not take\n");
    TRY(rmw("link width 1", rp + DBI_LINK_WIDTH, 0x3f << 16, 1 << 16));
    TRY(rmw("lanes 1", rp + DBI_GEN2_CTRL, 0x1f << 8, 1 << 8));
    TRY(rmw("link cap x1", rp + cap + PCI_EXP_LNKCAP, 0x3f << 4, 1 << 4));
    TRY(rmw("DBI write lock", rp + DBI_RO_WR_EN, BIT(0), 0));
    write16(rp + cap + PCI_EXP_LNKCTL, read16(rp + cap + PCI_EXP_LNKCTL) | 0x400);
    TRY(rmw("speed change", rp + DBI_GEN2_CTRL, 0, BIT(17)));
    /* Interrupt translation; reads back 0 (not needed for config reads) */
    write32(pp->i2axi + 0x80, 1);
    return 0;
}

/* E: lane PHY, refclk, PERST# release, LTSSM, link up within link_ms */
static int stage_e(const struct port_regs *pp, u32 link_ms)
{
    u64 p2 = pp->ctl;

    if (read32(pp->lane) & BIT(28))
        FAIL("lane PHY bit 28 set\n");
    TRY(rmw("lane clk0 req", pp->lane, 0, BIT(0)));
    TRY(poll_set("lane clk0 ack", pp->lane, BIT(2), 50));
    TRY(rmw("lane clk1 req", pp->lane, 0, BIT(1)));
    TRY(poll_set("lane clk1 ack", pp->lane, BIT(3), 50));
    TRY(rmw("lane bit 4 clear", pp->lane, BIT(4), 0));
    mdelay(1);
    TRY(rmw("lane bit 9", pp->lane, 0, BIT(9)));
    TRY(rmw("lane bit 10", pp->lane, 0, BIT(10)));
    /* refclk to PERST#: the bridge's t-refclk-to-perst (J716s pci-bridge0: 100 us), >= 1 ms */
    udelay(max(pp->refclk_to_perst_us, 1000u));

    u32 old = read32(pp->perst);
    if (old & GPIO_PERST_LOCK)
        FAIL("PERST# pad locked\n");
    u32 want = (old & ~GPIO_PERST_MASK) | GPIO_PERST_LOW | 1;
    write32(pp->perst, want);
    if ((read32(pp->perst) ^ want) & GPIO_PERST_MASK)
        FAIL("PERST# readback 0x%x\n", read32(pp->perst));

    TRY(rmw("LTSSM start", p2 + PORT_LTSSMCTL, 0, BIT(0)));
    TRY(poll_set("link up", p2 + PORT_LINKSTS, BIT(0), link_ms));
    mdelay(100);
    return 0;
}

/* F: bus numbers and the endpoint's ID (the N1's ROM personality, function 0) */
static int stage_f(const struct port_regs *pp)
{
    u64 p2 = pp->ctl, rp = pp->rp;
    u32 bus = pp->p->bus;

    if (!(read32(p2 + PORT_STATUS) & BIT(0)) || !(read32(p2 + PORT_LINKSTS) & BIT(0)))
        FAIL("port not ready or link down\n");
    int cap = find_pcie_cap(rp);
    TRY(cap);
    u16 lnksta = read16(rp + cap + PCI_EXP_LNKSTA);
    printf("pcie-t6050: port %d link Gen%d x%d\n", pp->p->port, lnksta & 0xf, (lnksta >> 4) & 0x3f);
    u32 buses = read32(rp + PCI_PRIMARY_BUS);
    if (!((buses >> 8) & 0xff)) {
        u32 want = (buses & 0xff000000) | (bus << 16) | (bus << 8);
        write32(rp + PCI_PRIMARY_BUS, want);
        if (read32(rp + PCI_PRIMARY_BUS) != want)
            FAIL("bus numbers did not take\n");
    } else if (((buses >> 8) & 0xff) != bus) {
        FAIL("secondary bus is %d, not %d\n", (buses >> 8) & 0xff, bus);
    }
    u32 id = read32(r.ecam + ((u64)bus << 20));
    if (id != 0x1900106b && id != 0x1901106b)
        FAIL("endpoint %02x:00.0 is 0x%08x, not the N1\n", bus, id);
    printf("pcie-t6050: endpoint %02x:00.0 is 0x%08x\n", bus, id);
    return 0;
}

/* G: what Linux's adoption requires, read only */
static int stage_g(const struct port_regs *pp)
{
    u64 p2 = pp->ctl;

    for (int i = 0; i < 64; i++)
        if (read32(p2 + PORT_RID2SID + 4 * i) & PORT_RID2SID_EN)
            FAIL("RID route %d is already valid\n", i);
    if (read32(r.ecam + ((u64)pp->p->bus << 20) + PCI_COMMAND) & 7)
        FAIL("endpoint I/O, memory or bus mastering enabled\n");
    if (read32(pp->dart + 0x200) || read32(pp->dart + 0x208) || read32(pp->dart + 0xc00))
        FAIL("DART%d protection or streams no longer 0\n", pp->p->port);
    return 0;
}

/* Release the N1's reset through the SMC: pKW4(key, template | 1), as dcp.c raises its rails */
static int n1_smc_release(const struct port_regs *pp)
{
    int ret = 0;

    if (!pp->reset_key)
        FAIL("%s function-reset is not an SMC pKW4\n", pp->bridge);
    rtkit_set_boot_timeout(10 * USEC_PER_SEC);
    smc_dev_t *smc = smc_init();
    rtkit_set_boot_timeout(0);
    if (!smc)
        FAIL("SMC not available\n");
    /* as J813: assert (template | 0), 50 ms, release (template | 1), 50 ms */
    for (u32 level = 0; level < 2 && !ret; level++) {
        u32 value = pp->reset_template | level;
        ret = smc_write_u32(smc, pp->reset_key, value);
        printf("pcie-t6050: SMC key %c%c%c%c <- 0x%08x: %d\n", (pp->reset_key >> 24) & 0xff,
               (pp->reset_key >> 16) & 0xff, (pp->reset_key >> 8) & 0xff, pp->reset_key & 0xff,
               value, ret);
        mdelay(50);
    }
    smc_shutdown(smc);
    if (ret)
        return -1;
    mdelay(100); /* J813: before the port reset */
    return 0;
}

static int n1_train(bool smc_release)
{
    pr.p = &n1_port;
    axi2af_excuses_init();
    TRY(find_regs());
    TRY(find_port_regs(&pr));
    TRY(storage_find());
    TRY(storage_check("before", true));

    u64 p2 = pr.ctl;
    if (read32(p2 + PORT_LINKSTS) & BIT(0)) {
        printf("pcie-t6050: port 0 already has a link: adopting it\n");
    } else {
        if (!(read32(r.rc + 0x58) & BIT(0)) || !(read32(r.phy_ctl + 0x08) & BIT(4))) {
            TRY(stage_a());
            TRY(storage_check("after A", false));
            if (read32(r.phy_ctl + 0x08) & BIT(4) && read32(r.rc + 0x58) & BIT(0)) {
                printf("pcie-t6050: shared GP init already in place\n");
            } else {
                TRY(stage_s());
                TRY(storage_check("after S", false));
            }
        } else {
            printf("pcie-t6050: shared GP init already in place\n");
        }
        if (smc_release)
            TRY(n1_smc_release(&pr));
        TRY(stage_b(&pr));
        TRY(stage_c(&pr));
        TRY(stage_d(&pr));
        TRY(stage_e(&pr, smc_release ? 1000 : 500));
    }
    TRY(stage_f(&pr));
    TRY(storage_check("after F", false));
    TRY(stage_g(&pr));
    printf("pcie-t6050: port 0 up, %s at %02x:00.0\n", pr.p->endpoint, pr.p->bus);
    return 0;
}

int pcie_t6050_n1_train(bool smc_release)
{
    static int state = 1;

    if (state <= 0)
        return state;
    storage_ref_valid = false;
    state = n1_train(smc_release);
    /* A stage that stopped partway may already have written shared power or the GP PHY */
    if (state < 0 && state != N1_STORAGE_LOST && storage_ref_valid) {
        int st = storage_check("after the stop", false);
        if (st < 0)
            state = st;
    }
    return state;
}

void pcie_t6050_storage_lost(const char *why)
{
    printf("%sstorage lost (%s): watchdog reset in 15 s\n", stop_prefix, why);
    mdelay(15000);
    wdt_reboot();
    while (true)
        ;
}

#define ANS_RUNNING() (read32(storage_addr[ST_ANS_CTRL]) & BIT(4))

/* Stop the ANS this file started; it must really stop, as it was when we came */
static void ans_stop(void)
{
    if (!nvme_stop())
        pcie_t6050_storage_lost("nvme_stop failed");
    if (ANS_RUNNING())
        pcie_t6050_storage_lost("ANS still running after nvme_stop");
    printf("pcie-t6050: ANS stopped again for Linux\n");
}

static int n1_train_ans(bool smc_release)
{
    const u32 nsid = 1, lba = 1; /* the GPT header of the 4 KiB-block namespace */

    TRY(storage_find());
    if (ANS_RUNNING()) {
        printf("pcie-t6050: ANS already running: training as =1/=2\n");
        return pcie_t6050_n1_train(smc_release);
    }

    printf("pcie-t6050: ANS stopped (0x%x): starting it\n", read32(storage_addr[ST_ANS_CTRL]));
    rtkit_set_boot_timeout(10 * USEC_PER_SEC); /* ANS can take ~3 s to answer after a stop */
    bool up = nvme_init();
    rtkit_set_boot_timeout(0);
    if (!up) {
        if (ANS_RUNNING())
            pcie_t6050_storage_lost("nvme_init failed, ANS left running");
        FAIL("nvme_init failed, ANS stopped as before: not training\n");
    }

    u8 *blk = memalign(SZ_16K, 2 * SZ_4K);
    if (!blk || !nvme_read(nsid, lba, blk)) {
        free(blk);
        ans_stop();
        FAIL("NVMe read before the training failed: not training\n");
    }
    printf("pcie-t6050: NVMe block %u read (%s)\n", lba,
           memcmp(blk, "EFI PART", 8) ? "no GPT signature" : "GPT header");

    int ret = pcie_t6050_n1_train(smc_release);

    /* Whatever the outcome: the same storage registers, the same block, read through ANS */
    if (ret == N1_STORAGE_LOST)
        pcie_t6050_storage_lost("during the port 0 training");
    if (storage_ref_valid && storage_check("after training", false))
        pcie_t6050_storage_lost("registers");
    if (!nvme_read(nsid, lba, blk + SZ_4K))
        pcie_t6050_storage_lost("read failed");
    if (memcmp(blk, blk + SZ_4K, SZ_4K))
        pcie_t6050_storage_lost("block differs");
    free(blk);
    printf("pcie-t6050: storage after training: NVMe read identical\n");

    ans_stop();
    return ret;
}

/*
 * Once per boot, like pcie_t6050_n1_train(): a second kboot preparation must not start a second
 * ANS session and compare it with the first one's reference. It gets the first result.
 */
int pcie_t6050_n1_train_ans(bool smc_release)
{
    static int state = 1;

    if (state <= 0)
        return state;
    state = n1_train_ans(smc_release);
    return state;
}
