# SPDX-License-Identifier: MIT
"""Reserve prior-stage tables after the chainload-local record has gone away.

The tested block and final reservation call are extracted from kboot_prepare_dt.
Real in-tree libfdt validates the outgoing reservation. The chip gate is the real
dart_legacy_display_chip() from src/dart.c (T8103, T600x, T602x). Boot arguments and image
addresses are simulated; no actual chainload, firmware, or DMA is exercised.
"""
from pathlib import Path
import os
import re
import subprocess

import pytest

SOURCE = Path(os.environ.get("DART_DISPLAY_SOURCE", Path(__file__).resolve().parents[2]))


def test_inherited_reservation(tmp_path):
    text = (SOURCE / "src/kboot.c").read_text()
    start = text.index("    u64 image_end = (u64)_end;", text.index("int kboot_prepare_dt("))
    end = text.index("    if (chip_id == T8140)", start)
    block = text[start:end]
    call = re.search(
        r"    if \(fdt_add_mem_rsv\(dt, \(u64\)_base, image_end - \(u64\)_base\)\)\n"
        r"        bail\([^\n]+\);", text[end:]
    )
    assert call, "missing real m1n1 image reservation"
    dart = (SOURCE / "src/dart.c").read_text()
    m = re.search(r"\nbool dart_legacy_display_chip\(void\)\n\{.*?\n\}\n", dart, re.S)
    assert m, "missing real dart_legacy_display_chip"
    t602x = re.search(r"\nstatic bool dart_legacy_display_t602x\(void\)\n\{.*?\n\}\n", dart, re.S)
    assert t602x, "missing real dart_legacy_display_t602x"
    legacy_chip = t602x.group(0) + m.group(0)
    code = tmp_path / "inherited.c"
    code.write_text(r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "libfdt.h"
typedef uint64_t u64;
#define T8103 0x8103
#define T8112 0x8112
#define T6000 0x6000
#define T6001 0x6001
#define T6002 0x6002
#define T6020 0x6020
#define T6021 0x6021
#define T6022 0x6022
#define max(a, b) ((a) > (b) ? (a) : (b))
#define bail(...) do { printf(__VA_ARGS__); return -1; } while (0)
static struct { u64 phys_base, mem_size, top_of_kernel_data; } cur_boot_args;
static unsigned chip_id;
#include <stdbool.h>
''' + legacy_chip + r'''
static u64 image_base, image_end_address;
#define _base ((void *)(uintptr_t)image_base)
#define _end ((void *)(uintptr_t)image_end_address)
static unsigned char tree[4096], before[4096];
static void *dt = tree;
static int reserve(void)
{
''' + block + call.group(0) + r'''
    return 0;
}
static void reset(void)
{
    memset(tree, 0, sizeof(tree));
    assert(!fdt_create_empty_tree(tree, sizeof(tree)));
    memcpy(before, tree, sizeof(tree));
    chip_id = T8103;
    cur_boot_args.phys_base = 0x800000000UL;
    cur_boot_args.mem_size = 0x20000000;
    image_base = cur_boot_args.phys_base + 0x100000;
    image_end_address = image_base + 0x400000;
    cur_boot_args.top_of_kernel_data = image_base + 0xc00000;
}
static void range(u64 expected_end)
{
    u64 address, size;
    assert(!reserve());
    assert(fdt_num_mem_rsv(tree) == 1);
    assert(!fdt_get_mem_rsv(tree, 0, &address, &size));
    assert(address == image_base && size == expected_end - image_base);
    assert(address + size == expected_end);
}
static void refused(void)
{
    assert(reserve() == -1);
    assert(!memcmp(tree, before, sizeof(tree)));
    assert(!fdt_num_mem_rsv(tree));
}
int main(void)
{
    /* Stage 1 published an L2 at +8 MiB, then passed a +12 MiB high-water
     * mark. Its static list does not exist in this fresh stage-2 process. */
    reset();
    u64 previous_table = image_base + 0x800000;
    u64 current_heap = image_base + 0x8000000;
    range(cur_boot_args.top_of_kernel_data);
    u64 address, size;
    assert(!fdt_get_mem_rsv(tree, 0, &address, &size));
    assert(previous_table >= address && previous_table + 0x4000 <= address + size);
    assert(current_heap > address + size); /* Not the current stage's heap. */
    reset(); cur_boot_args.top_of_kernel_data = image_base + 0x200000;
    range(image_end_address);
    reset(); cur_boot_args.top_of_kernel_data = image_end_address;
    range(image_end_address);
    reset(); cur_boot_args.mem_size = 0; refused();
    reset(); cur_boot_args.phys_base = UINT64_MAX - 0x100; refused();
    reset(); image_base = cur_boot_args.phys_base - 0x4000; refused();
    reset(); cur_boot_args.top_of_kernel_data = image_base - 1; refused();
    reset(); cur_boot_args.top_of_kernel_data =
        cur_boot_args.phys_base + cur_boot_args.mem_size + 1; refused();
    reset(); image_end_address = cur_boot_args.phys_base + cur_boot_args.mem_size + 1;
    refused();
    reset();
    fdt_set_totalsize(tree, fdt_off_dt_strings(tree) + fdt_size_dt_strings(tree));
    memcpy(before, tree, sizeof(tree));
    refused(); /* No room to add a reservation: handoff must fail. */
    /* Other chips retain the old image-only reservation and do not interpret
     * this inherited high-water mark as T8103 ownership. */
    reset(); chip_id = T8112;
    range(image_end_address);
    reset(); chip_id = T8112; cur_boot_args.top_of_kernel_data = image_base - 1;
    range(image_end_address);
    /* T600x and T602x publish L2 tables into their locked display DART the
     * same way (m1n1 #46 / #48), so a chainloaded stage must keep the
     * inherited prefix for them too, with the same bounds checks. */
    static const unsigned legacy[] = { T6000, T6001, T6002, T6020, T6021, T6022 };
    for (unsigned i = 0; i < sizeof(legacy) / sizeof(legacy[0]); i++) {
        reset(); chip_id = legacy[i];
        range(cur_boot_args.top_of_kernel_data);
        assert(!fdt_get_mem_rsv(tree, 0, &address, &size));
        assert(previous_table >= address && previous_table + 0x4000 <= address + size);
        reset(); chip_id = legacy[i]; cur_boot_args.top_of_kernel_data = image_base - 1;
        refused();
    }
    puts("PASS inherited reservation 24 cases");
    return 0;
}
''')
    lib = SOURCE / "src/libfdt"
    binary = tmp_path / "inherited"
    command = [os.environ.get("CC", "cc"), "-std=gnu11", "-O1", "-g",
               "-Wall", "-Wextra", "-Werror", "-fsanitize=undefined",
               "-fno-sanitize-recover=all", "-I", str(lib), str(code)]
    command += [str(lib / name) for name in (
        "fdt.c", "fdt_ro.c", "fdt_rw.c", "fdt_wip.c", "fdt_sw.c",
        "fdt_strerror.c", "fdt_empty_tree.c", "fdt_check.c",
    )]
    command += ["-o", str(binary)]
    compiled = subprocess.run(command, capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS inherited reservation 24 cases" in result.stdout
