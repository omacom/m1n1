# SPDX-License-Identifier: MIT
"""dptxport_connect() accepts the connect replies real firmware sends.

Compiles the real dptxport_connect() and its request/reply structs from
src/dcp/dptx_port_ep.c against a fake EPIC transport. The fake records the
request and returns a canned reply, so no firmware or DCP is involved.

Seen on hardware: EPIC v1 firmware (J475c / T6021, OS firmware 13.5) is sent
unk 0 and echoes unk 0. The M2 Ultra (J475d / T6022) replies 0x100. Both are
successful connects and must not abort display setup.
"""
from pathlib import Path
import os
import re
import subprocess

REPO = Path(os.environ.get("M1N1_DCP_SOURCE", Path(__file__).resolve().parents[2]))


def function(source, name):
    match = re.search(r"^(?:static )?int " + name + r"\([^;]*?\)\s*\{", source, re.M | re.S)
    assert match, name
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


def struct(source, name):
    match = re.search(r"^struct " + name + r" \{.*?\} __attribute__\(\(packed\)\);", source,
                      re.M | re.S)
    assert match, name
    return match.group(0)


HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef int32_t s32;
typedef uint64_t u64;
#define BIT(x)                 (1UL << (x))
#define GENMASK(msb, lsb)      ((BIT((msb + 1) - (lsb)) - 1) << (lsb))
#define _FIELD_LSB(field)      ((field) & ~(field - 1))
#define FIELD_PREP(field, val) (((val) * (_FIELD_LSB(field))) & (field))
@DEFINES@
typedef struct { bool v2; } afk_epic_ep_t;
typedef struct { afk_epic_ep_t *epic; } afk_epic_service_t;
static bool afk_epic_is_v2(const afk_epic_ep_t *epic) { return epic->v2; }
@STRUCTS@

static int call_ret, calls;
static u32 sent_unk, sent_target, sent_command;
static struct dcpdptx_connection_reply canned;

static int afk_service_call(afk_epic_service_t *service, u16 group, u32 command, const void *data,
                            size_t data_size, size_t data_pad, void *output, size_t output_size,
                            size_t output_pad)
{
    (void)service; (void)group; (void)data_pad; (void)output_pad;
    const struct dcpdptx_connection_cmd *cmd = data;
    assert(data_size == sizeof(*cmd) && output_size == sizeof(canned));
    calls++;
    sent_command = command;
    sent_unk = cmd->unk;
    sent_target = cmd->target;
    if (!call_ret)
        memcpy(output, &canned, sizeof(canned));
    return call_ret;
}

@CONNECT@

#define TARGET (FIELD_PREP(DCPDPTX_REMOTE_PORT_DFP, 4) | DCPDPTX_REMOTE_PORT_CONNECTED)

static int connect(bool v2, u32 reply_unk, u32 reply_target, u32 status)
{
    afk_epic_ep_t ep = { .v2 = v2 };
    afk_epic_service_t service = { .epic = &ep };
    calls = 0;
    memset(&canned, 0, sizeof(canned));
    canned.unk = reply_unk;
    canned.target = reply_target;
    canned.status = status;
    int ret = dptxport_connect(&service, 0, 4, 0);
    assert(calls == 1 && sent_command == 11 && sent_target == TARGET);
    assert(sent_unk == (v2 ? 0x100u : 0u));
    return ret;
}

int main(void)
{
    assert(TARGET == 0x8040); /* matches the J475c m1n1 log */

    /* EPIC v1 (J475c): unk 0 sent, 0 echoed. This was rejected before. */
    assert(connect(false, 0, TARGET, 0) == 0);
    /* A 0x100 reply (J475d, and EPIC v2) is accepted either way. */
    assert(connect(false, 0x100, TARGET, 0) == 0);
    assert(connect(true, 0x100, TARGET, 0) == 0);
    /* An unexpected unk is logged, not fatal: Linux does the same. */
    assert(connect(false, 0x55, TARGET, 0) == 0);
    assert(connect(true, 0, TARGET, 0) == 0);

    /* Still fatal: a reply for another port. */
    assert(connect(false, 0, TARGET ^ 0x10, 0) == -1);
    assert(connect(true, 0x100, 0x8000, 0) == -1);
    /* Still fatal: a v2 error status, before anything else is checked. */
    assert(connect(true, 0x100, TARGET, 0xe00002c2) == (s32)0xe00002c2);
    /* v1 has no status field to honour. */
    assert(connect(false, 0, TARGET, 7) == 0);
    /* Still fatal: the transport call failing. */
    call_ret = -5;
    assert(connect(false, 0, TARGET, 0) == -5);
    call_ret = 0;

    puts("PASS dptxport_connect 10 cases");
    return 0;
}
'''


def test_dptxport_connect_reply(tmp_path):
    source = (REPO / "src/dcp/dptx_port_ep.c").read_text()
    header = (REPO / "src/dcp/dptx_port_ep.h").read_text()
    defines = "\n".join(re.findall(r"^#define DCPDPTX_REMOTE_PORT_\w+ +.*$", header, re.M))
    assert defines.count("#define") == 4
    code = (HARNESS
            .replace("@DEFINES@", defines)
            .replace("@STRUCTS@", struct(source, "dcpdptx_connection_cmd") + "\n" +
                     struct(source, "dcpdptx_connection_reply"))
            .replace("@CONNECT@", function(source, "dptxport_connect")))
    binary = tmp_path / "connect"
    subprocess.run(
        [os.environ.get("CC", "cc"), "-std=gnu11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=undefined", "-fno-sanitize-recover=all", "-x", "c", "-", "-o", str(binary)],
        input=code, text=True, check=True,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS dptxport_connect 10 cases" in result.stdout
