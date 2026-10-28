/* SPDX-License-Identifier: MIT */

#ifndef SHA256_H
#define SHA256_H

#include "types.h"

/* Minimal streaming SHA-256 (FIPS 180-4). No dependency in src/ before this. */

typedef struct {
    u32 state[8];
    u64 bitlen;
    u8 buf[64];
    u32 buflen;
} sha256_ctx;

void sha256_init(sha256_ctx *ctx);
void sha256_update(sha256_ctx *ctx, const void *data, u32 len);
void sha256_final(sha256_ctx *ctx, u8 out[32]);
void sha256(const void *data, u32 len, u8 out[32]);

#endif
