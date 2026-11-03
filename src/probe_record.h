/* SPDX-License-Identifier: MIT */

#ifndef PROBE_RECORD_H
#define PROBE_RECORD_H

#include "types.h"

/*
 * T6032 on-screen probe: shareable record.
 *
 * The record carries NO raw firmware bytes: only sizes, counts, digests and
 * addresses. It is packed little-endian (the SoC and the host decoder are
 * both little-endian), CRC-32'd (IEEE, via tinf_crc32), then base32-encoded
 * (RFC 4648 alphabet, no padding) for the on-screen backup block.
 *
 * Record layout v1:
 *   0   8  magic  "T6032PR1"
 *   8   4  u32 crc32 of the payload (everything below)
 *   12  4  u32 payload length
 *  16   4  u32 version = 1
 *  20   4  u32 flags (bit 0: the probe powered the sgx domain)
 *  24   8  u64 chip id
 *  32  24  model, NUL padded
 *  56  16  target type, NUL padded
 *  72  32  8 x u32 sgx words (0xffffffff = bus fault)
 * 104   1  cores per cluster (0 = unknown)
 * 105   1  clusters per die
 * 106   1  dies
 * 107   1  total guarded-read faults
 * 108   4  u32 gpu text size (0 = not read)
 * 112   4  u32 gpu text non-zero byte count
 * 116   4  u32 tag count (capped at 4)
 * 120  32  sha256 of the gpu text (raw), all zero when not read
 * 152 144  4 x { u32 tag offset; u8 sha256[32] } (8 bytes after each tag
 *           zeroed); unused entries are zero
 * 296  48  3 x { u64 phys; u64 size } gfx-asc segment-ranges entries
 * = 328 payload bytes, 344 byte record, 551 base32 characters.
 */

#define PROBE_RECORD_MAGIC "T6032PR1"
#define PROBE_RECORD_VERSION 1
#define PROBE_MAX_TAGS 4
#define PROBE_MAX_SEGS 3
#define PROBE_SGX_WORDS 8

typedef struct {
    u32 offset;
    u8 sha[32];
} probe_tag_digest;

typedef struct {
    u8 model[24];
    u8 target[16];
    u64 chip_id;
    u32 sgx_words[PROBE_SGX_WORDS];
    u8 family, variant;
    u8 cores_per_cluster, clusters_per_die, dies;
    u8 fault_count;
    u8 powered_gpu;
    u32 seg_count;
    u64 seg_phys[PROBE_MAX_SEGS];
    u64 seg_size[PROBE_MAX_SEGS];
    u32 text_size;
    u32 nonzero_count;
    u32 tag_count;
    u8 text_sha[32];
    probe_tag_digest tags[PROBE_MAX_TAGS];
} probe_t6032_data;

/* ID_COUNTS_1 fields as src/kboot_gpu.c documents them. */
void probe_decode_hwid(u32 v, u32 c, probe_t6032_data *d);

/* Packs the record (magic + crc + payload). Returns the length or -1. */
int probe_record_pack(const probe_t6032_data *d, u8 *out, u32 out_max);

/* RFC 4648 base32, no padding, NUL-terminated. Returns the length or -1. */
int probe_base32_encode(const u8 *in, u32 len, char *out, u32 out_max);

#endif
