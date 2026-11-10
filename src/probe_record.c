/* SPDX-License-Identifier: MIT */

/* Pure packing/decoding helpers for the T6032 on-screen probe record. */

#include "probe_record.h"

#include "string.h"
#include "tinf/tinf.h"

void probe_decode_hwid(u32 v, u32 c, probe_t6032_data *d)
{
    d->family = v >> 24;
    d->variant = (v >> 16) & 0xff;
    d->cores_per_cluster = c & 0xff;
    d->clusters_per_die = (c >> 8) & 0xff;
    d->dies = (c >> 16) & 0xf;
}

static void put_u32(u8 *p, u32 v)
{
    p[0] = v;
    p[1] = v >> 8;
    p[2] = v >> 16;
    p[3] = v >> 24;
}

static void put_u64(u8 *p, u64 v)
{
    put_u32(p, v);
    put_u32(p + 4, v >> 32);
}

int probe_record_pack(const probe_t6032_data *d, u8 *out, u32 out_max)
{
    const u32 payload_len = 4 + 4 + 8 + 24 + 16 + 4 * PROBE_SGX_WORDS + 4 + 4 + 4 + 4 + 32
                            + PROBE_MAX_TAGS * (4 + 32) + PROBE_MAX_SEGS * 16;
    u8 *p;
    u32 i;

    if (out_max < 16 + payload_len)
        return -1;

    memset(out, 0, 16 + payload_len);
    memcpy(out, PROBE_RECORD_MAGIC, 8);
    put_u32(out + 12, payload_len);

    p = out + 16;
    put_u32(p + 0, PROBE_RECORD_VERSION);
    put_u32(p + 4, d->powered_gpu ? 1 : 0);
    put_u64(p + 8, d->chip_id);
    memcpy(p + 16, d->model, sizeof(d->model));
    memcpy(p + 40, d->target, sizeof(d->target));
    for (i = 0; i < PROBE_SGX_WORDS; i++)
        put_u32(p + 56 + 4 * i, d->sgx_words[i]);
    p[88] = d->cores_per_cluster;
    p[89] = d->clusters_per_die;
    p[90] = d->dies;
    p[91] = d->fault_count;
    put_u32(p + 92, d->text_size);
    put_u32(p + 96, d->nonzero_count);
    put_u32(p + 100, d->tag_count);
    memcpy(p + 104, d->text_sha, 32);
    for (i = 0; i < PROBE_MAX_TAGS; i++) {
        put_u32(p + 136 + 36 * i, d->tags[i].offset);
        memcpy(p + 140 + 36 * i, d->tags[i].sha, 32);
    }
    for (i = 0; i < PROBE_MAX_SEGS; i++) {
        put_u64(p + 280 + 16 * i, d->seg_phys[i]);
        put_u64(p + 288 + 16 * i, d->seg_size[i]);
    }

    put_u32(out + 8, tinf_crc32(p, payload_len));
    return 16 + payload_len;
}

static const char b32_alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

int probe_base32_encode(const u8 *in, u32 len, char *out, u32 out_max)
{
    u32 olen = (len * 8 + 4) / 5;
    u32 i, o = 0;

    if (olen + 1 > out_max)
        return -1;

    for (i = 0; i < len; i += 5) {
        u32 nb = len - i < 5 ? len - i : 5;
        u32 nc = (nb * 8 + 4) / 5;
        u64 acc = 0;
        u32 n;

        for (n = 0; n < 5; n++)
            acc = (acc << 8) | (n < nb ? in[i + n] : 0);
        for (n = 0; n < nc; n++)
            out[o++] = b32_alphabet[(acc >> (35 - 5 * n)) & 31];
    }
    out[o] = '\0';
    return o;
}
