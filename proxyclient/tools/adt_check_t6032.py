#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify m1n1's T6032 (M3 Ultra, J575D) CPU-side assumptions against a real ADT.

Usage: adt_check_t6032.py <ADT-file>

<ADT-file> is the decoded binary ADT (the IM4P payload of DeviceTree.j575dap.im4p, after
its outer LZSS wrapper and LZFSE payload decoding). Exits 1 if any check fails.

Checked against the sources of this branch:
  src/soc.h        EARLY_UART_BASE for T6032
  src/smp.c        CPU_START_OFF_T6031, CPU_REG_{CORE,CLUSTER,DIE}, MAX_CPUS
  src/pmgr.h       PMGR_DIE_OFFSET
  src/cpufreq.c    t6032_clusters[]
  src/mcc.c        mcc_init_m3() reg_offset for T6032, MAX_MCC_INSTANCES, mcc,t6031 path
  src/chickens.c   MIDR parts (T6031 Everest/Sawtooth cover T6032)
  src/isp.c        isp_init() absence path
  src/kboot.c      dt_set_pmp_t6031 / dt_t6031_fw_uuids / dt_t6031_facts gating,
                   display reserved-memory compat chain
"""
import sys
import pathlib

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from m1n1.adt import load_adt

# Restated m1n1 constants (keep in sync with the sources named above)
PMGR_DIE_OFFSET = 0x2000000000  # src/pmgr.h
CPU_REG_CORE = 0xFF             # GENMASK(7, 0), src/smp.c
CPU_REG_CLUSTER = 0x700         # GENMASK(10, 8)
CPU_REG_DIE = 0x7800            # GENMASK(14, 11)
MAX_CPUS = 32                   # src/smp_asm.h
MAX_MCC_INSTANCES = 16          # src/mcc.c
T6032_EARLY_UART_BASE = 0x391200000  # src/soc.h; = /arm-io/uart0 reg0 TRANSLATED
MCC_DIE0_FIRST = 0x220000000         # translated /arm-io/mcc instance window (bus 0x20000000)

failures = 0


def check(name, ok, detail):
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        failures += 1


def absent(adt, path):
    try:
        adt[path]
        return False
    except Exception:
        return True


def have(adt, path):
    try:
        return adt[path]
    except Exception:
        return None


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    adt = load_adt(open(sys.argv[1], "rb").read())

    # --- Board identity ---
    compat = list(adt.compatible)
    check("root compatible", "J575dAP" in compat and "Mac15,14" in compat, compat)
    check("target-type", adt.target_type == "J575d", adt.target_type)
    armio = adt["/arm-io"]
    check("arm-io compatible (T6031-family SoC side)",
          list(armio.compatible) == ["arm-io,t6031"], list(armio.compatible))
    check("no /arm-io die-count (one tree for two dies, die-1 at +0x2000000000)",
          "die-count" not in armio._properties, "die-count absent")
    r0 = list(armio.ranges)[0]
    check("arm-io ranges[0] maps bus 0 -> 0x200000000 size 0x400000000 (translation is exercised)",
          r0.bus_addr == 0 and r0.parent_addr == 0x200000000 and r0.size == 0x400000000,
          f"bus {hex(r0.bus_addr)} -> parent {hex(r0.parent_addr)} size {hex(r0.size)}")

    # --- SMP: 32 cores, reg field decode, die-1 at +PMGR_DIE_OFFSET ---
    cpus = sorted(adt["/cpus"], key=lambda c: c.cpu_id)
    check("cpu count <= MAX_CPUS(32)", 0 < len(cpus) <= MAX_CPUS, f"{len(cpus)} cpu nodes")
    ok = True
    detail = []
    for c in cpus:
        reg = c.reg
        die, cluster, core = (reg & CPU_REG_DIE) >> 11, (reg & CPU_REG_CLUSTER) >> 8, reg & CPU_REG_CORE
        # "cluster-id" is global (die*8 + die-cluster-id); the reg carries the die-local cluster,
        # which is what smp_start_cpu() writes (start_base is already die-folded).
        if (die, cluster, core) != (c.die_id, c.die_cluster_id, c.cluster_core_id):
            ok = False
            detail.append(f"cpu{c.cpu_id}: decoded ({die},{cluster},{core}) != props "
                          f"({c.die_id},{c.die_cluster_id},{c.cluster_core_id})")
        if c.cluster_id != c.die_id * 8 + c.die_cluster_id:
            ok = False
            detail.append(f"cpu{c.cpu_id}: global cluster-id {c.cluster_id} unexpected")
    check("CPU_REG_{DIE,CLUSTER,CORE} decode all 32 ADT cpu regs (die-local cluster)", ok,
          "; ".join(detail) if detail else "die/cluster/core match for every node")
    types = {}
    for c in cpus:
        types.setdefault((c.die_id, c.cluster_id), set()).add(list(c.compatible)[0])
    exp = {(0, 0): {"apple,sawtooth"}, (0, 1): {"apple,everest"}, (0, 2): {"apple,everest"},
           (1, 8): {"apple,sawtooth"}, (1, 9): {"apple,everest"}, (1, 10): {"apple,everest"}}
    check("cluster topology E/P per die (6 clusters)",
          types == exp, types)
    cores = {list(c.compatible)[0] for c in cpus}
    check("core parts are T6031's Everest/Sawtooth (chickens.c MIDR parts cover them)",
          cores == {"apple,sawtooth", "apple,everest"}, sorted(cores))
    ok, detail = True, []
    for c in cpus:
        if "cpu-impl-reg" not in c._properties or "state" not in c._properties:
            ok = False
            detail.append(f"cpu{c.cpu_id} missing cpu-impl-reg/state")
            continue
        impl = c.translate(list(c.cpu_impl_reg)[0])
        if c.die_id == 1 and (impl & PMGR_DIE_OFFSET) != PMGR_DIE_OFFSET:
            ok = False
            detail.append(f"cpu{c.cpu_id} impl_reg {hex(impl)} lacks die offset")
    check("every core has cpu-impl-reg (RVBAR) and state; die-1 at +0x2000000000 (physical)", ok,
          "; ".join(detail) if detail else "32/32 cores, die-1 folded by PMGR_DIE_OFFSET")

    # --- cpufreq: t6032_clusters die-1 bases are die-0 + PMGR_DIE_OFFSET ---
    # t6032_clusters: die-0 {0x210e00000, 0x211e00000, 0x212e00000}, die-1 {0x2210e00000, ...}
    die0_bases = [0x210E00000, 0x211E00000, 0x212E00000]
    ok, detail = True, []
    for c in cpus:
        idx = c.cluster_id - 8 if c.die_id == 1 else c.cluster_id
        base = die0_bases[idx] + PMGR_DIE_OFFSET * c.die_id
        acc = c.translate(list(c.acc_impl_reg)[0])
        # acc-impl-reg is the cluster's interface block at <cluster complex> + 0x100000
        if acc - base != 0x100000:
            ok = False
            detail.append(f"cpu{c.cpu_id}: acc {hex(acc)} vs cluster base {hex(base)}")
    check("t6032_clusters bases consistent with ADT acc-impl-reg (+0x100000)", ok,
          "; ".join(detail) if detail else "6/6 clusters line up, die-1 = die-0 + 0x2000000000")

    # --- MCC: compat, counts, 20 reg entries, die folding, 16 instances from reg[4] ---
    mcc = have(adt, "/arm-io/mcc")
    if mcc is None:
        check("mcc node", False, "/arm-io/mcc absent")
    else:
        check("mcc compatible (mcc,t6031 dispatch in mcc_init_m3)",
              list(mcc.compatible) == ["mcc,t6031"], list(mcc.compatible))
        check("mcc dcs-count-per-amcc", mcc.dcs_count_per_amcc == 4, mcc.dcs_count_per_amcc)
        check("mcc plane-count-per-amcc", mcc.plane_count_per_amcc == 4, mcc.plane_count_per_amcc)
        check("mcc reg count 20 (3 header + 8 instances/die)", len(mcc.reg) == 20,
              f"{len(mcc.reg)} entries")
        # Physical (range-translated) addresses: this is what m1n1's adt_get_reg() hands out.
        phys = [mcc.get_reg(i)[0] for i in range(len(mcc.reg))]
        check("mcc phys reg[3] == phys reg[2] + PMGR_DIE_OFFSET",
              phys[3] == phys[2] + PMGR_DIE_OFFSET,
              f"{hex(phys[2])} -> {hex(phys[3])}")
        inst = phys[4:]
        check("16 MCC instances from reg[4] (reg_offset = 3 + (T6032)), fits MAX_MCC_INSTANCES",
              len(inst) == 16 == MAX_MCC_INSTANCES, f"{len(inst)} instances")
        die0, die1 = inst[:8], inst[8:]
        check("instance windows (physical): 8 die-0 at 0x220000000+, die-1 at +PMGR_DIE_OFFSET",
              all(b == MCC_DIE0_FIRST + 0x2000000 * i for i, b in enumerate(die0)) and
              all(b == MCC_DIE0_FIRST + 0x2000000 * i + PMGR_DIE_OFFSET for i, b in enumerate(die1)),
              f"die0 {hex(die0[0])}..{hex(die0[-1])}, die1 {hex(die1[0])}..{hex(die1[-1])}")

    # --- isp: absent on the Studio, isp_init() no-ops ---
    check("/arm-io/isp and /arm-io/dart-isp absent (isp_init returns early)",
          absent(adt, "/arm-io/isp") and absent(adt, "/arm-io/dart-isp") and
          absent(adt, "/arm-io/isp0") and absent(adt, "/arm-io/dart-isp0"),
          "no isp node of either naming")

    # --- early UART (translated physical: /arm-io ranges[0] maps bus 0 -> 0x200000000) ---
    uart = have(adt, "/arm-io/uart0")
    if uart is None:
        check("early uart", False, "/arm-io/uart0 absent")
    else:
        base = uart.get_reg(0)[0]
        check("EARLY_UART_BASE(T6032) == /arm-io/uart0 reg0, range-translated",
              base == T6032_EARLY_UART_BASE, hex(base))

    # --- handoff gating facts (kboot.c: PMP none, uuids yes, facts no) ---
    check("/arm-io/pmp and /arm-io/dart-pmp absent (no PMP values handoff for J575D)",
          absent(adt, "/arm-io/pmp") and absent(adt, "/arm-io/dart-pmp"),
          "PMP handoff stays T6031-boards-only")
    gfx = have(adt, "/arm-io/gfx-asc")
    check("/arm-io/gfx-asc/iop-gfx-nub0 present (fw uuids owner 'gfx')",
          gfx is not None and any(s.name == "iop-gfx-nub0" for s in gfx),
          "iop-gfx-nub0 present")
    nubs = ["dcp", "pmp"]
    ok, detail = True, []
    if have(adt, "/arm-io/dcp") is not None:
        ok = False
        detail.append("unexpected /arm-io/dcp")
    if absent(adt, "/arm-io/pmp"):
        detail.append("dcp/pmp absent -> 'absent' in the uuid list (schema handles it)")
    check("uuid owners dcp/pmp absent as the schema expects", ok, "; ".join(detail))
    ext = [i for i in range(8) if have(adt, f"/arm-io/dcpext{i}") is not None]
    check("dcpext0..7 present with iop nubs (dcpext4..7 beyond the T6031 uuid list)",
          ext == list(range(8)) and all(
              any(s.name == f"iop-dcpext{i}-nub" for s in adt[f"/arm-io/dcpext{i}"])
              for i in range(8)), f"present: {ext}")
    check("/arm-io/disp0 absent (facts export stays T6031-only)",
          absent(adt, "/arm-io/disp0"), "no disp0 clock facts on the Studio")
    check("gpu-side: /arm-io/sgx compatible gpu,t6032 (refusal names the missing ABI)",
          list(adt["/arm-io/sgx"].compatible) == ["gpu,t6032"],
          list(adt["/arm-io/sgx"].compatible))

    # --- kboot must not fail earlier: nodes other paths touch ---
    aop_dart = have(adt, "/arm-io/dart-aop")
    check("dapf: /arm-io/dart-aop present with dapf-instance-0; absent dart-pmp/dart-isp skipped",
          aop_dart is not None and "dapf-instance-0" in aop_dart._properties and
          absent(adt, "/arm-io/dart-pmp") and absent(adt, "/arm-io/dart-isp"),
          "dapf_init_fdt() skips missing nodes")
    ans = have(adt, "/arm-io/ans")
    if ans is None:
        check("ans present, translated reg[3] valid (nvme nvmmu path), die from bit 37 = 1",
              False, "FAIL: /arm-io/ans missing")
    else:
        reg3 = ans.get_reg(3)[0]
        check("ans present, translated reg[3] valid (nvme nvmmu path), die from bit 37 = 1",
              reg3 != 0 and ((reg3 >> 37) & 3) == 1, hex(reg3))
    check("smc and aop present (ascwrap-v6, as other M3-family ADTs)",
          have(adt, "/arm-io/smc") is not None and have(adt, "/arm-io/aop") is not None,
          "both present")
    check("chosen/memory-map present (SEPFW etc. are iBoot-filled at runtime, not in this file)",
          have(adt, "/chosen/memory-map") is not None,
          "static ADT: runtime entries added by iBoot")

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
