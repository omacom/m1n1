/* SPDX-License-Identifier: MIT */

/*
 * On-screen probe for the Mac Studio (M3 Ultra, T6032).
 *
 * Runs in stage 2 right after init, before the proxy: reads what only a
 * running loader can read, draws it in large text on the firmware
 * framebuffer iBoot left behind (a phone photo is the transport), prints the
 * same lines to the console, and returns so the proxy comes up as usual.
 *
 * Board gate first: nothing but the ADT identity properties are touched
 * unless the chip is T6032 and the ADT says J575dAP / J575d. The probe never
 * writes a GPU register, never starts firmware, and never boots anything.
 * Shareable output (screen, console, record) carries no raw firmware bytes:
 * only sizes, counts, addresses and SHA-256 digests.
 */

#include "probe_t6032.h"

#include "adt.h"
#include "exception.h"
#include "fb.h"
#include "malloc.h"
#include "pmgr.h"
#include "probe_record.h"
#include "sha256.h"
#include "soc.h"
#include "string.h"
#include "types.h"
#include "utils.h"

#ifdef PROBE_HOST
#include "probe_host_shim.h"
#endif

/* Hardware access funnels. Weak so the host harness can mock and count
 * them; the device bodies below are the only implementations that ship. */

u32 __attribute__((weak)) probe_mmio_read32(u64 addr)
{
    return read32(addr);
}

int __attribute__((weak)) probe_mem_read(u64 addr, void *dst, u32 len)
{
    int before = exc_count;

    exc_guard = GUARD_MARK | GUARD_SILENT;
    memcpy(dst, (void *)(unsigned long)addr, len);
    exc_guard = GUARD_OFF;
    return exc_count == before ? 0 : -1;
}

int __attribute__((weak)) probe_power_enable(const char *path)
{
    return pmgr_adt_power_enable(path);
}

const void *__attribute__((weak)) probe_adt_prop(int node, const char *name, u32 *len)
{
    return adt_getprop(adt, node, name, len);
}

int __attribute__((weak)) probe_adt_path(const char *path)
{
    return adt_path_offset(adt, path);
}

int __attribute__((weak)) probe_sgx_base(u64 *base)
{
    int path[16];
    int node = adt_path_offset_trace(adt, "/arm-io/sgx", path);

    if (node < 0)
        return -1;
    return adt_get_reg(adt, path, "reg", 0, base, NULL) ? -1 : 0;
}

#define PROBE_TITLE "T6032 PROBE v1"
#define PROBE_GKTS_TAG 0x53544b47u /* 'GKTS' little endian */

static const u32 probe_sgx_offsets[PROBE_SGX_WORDS] = {
    0xD04000, 0xD04010, 0xD04014, 0xD04018, 0xD0401C, 0xE01500, 0xE01504, 0xE01508,
};

static u32 probe_faults;

static u32 probe_guarded_read32(u64 addr, int *faulted)
{
    int before = exc_count;
    u32 v;

    exc_guard = GUARD_MARK | GUARD_SILENT;
    v = probe_mmio_read32(addr);
    *faulted = exc_count != before;
    exc_guard = GUARD_OFF;
    if (*faulted)
        probe_faults++;
    return v;
}

/* ---- screen ---- */

static rgb_t probe_white = {255, 255, 255};
static rgb_t probe_green = {0, 255, 0};
static rgb_t probe_red = {255, 80, 80};

static void probe_hex(char *dst, u64 v, int digits)
{
    static const char d[] = "0123456789abcdef";
    int i;

    for (i = digits - 1; i >= 0; i--) {
        dst[i] = d[v & 0xf];
        v >>= 4;
    }
    dst[digits] = '\0';
}

static void probe_hexbuf(char *dst, const u8 *b, u32 n)
{
    while (n--) {
        u8 v = *b++;

        dst[0] = "0123456789abcdef"[v >> 4];
        dst[1] = "0123456789abcdef"[v & 15];
        dst += 2;
    }
    *dst = '\0';
}

static u32 probe_font_h(void)
{
    u32 h = fb_font_height(); /* 16 normally, 32 when iBoot flags the display retina */

    return h ? h : 16; /* 0 = console not initialised */
}

static u32 probe_font_w(void)
{
    u32 w = fb_font_width();

    return w ? w : 8;
}

/* The whole shareable screen: big facts on top, small backup block below. */
static void probe_render(const probe_t6032_data *d, const char *title, const char *b32)
{
    char line[560];
    char hex[65];
    u32 scale, small, y, lh, lh_s, b32_len, per_line, b32_lines, rows_small, s;
    int i;

    b32_len = strlen(b32);

    /* Fit big and small rows together: the base32 wrap depends on the small
     * scale, and the line count feeds the height. */
    s = fb.width / (80 * probe_font_w());
    if (s < 2)
        s = 2;
    if (s > 8)
        s = 8;
    for (;; s--) {
        small = s / 2;
        if (small < 1)
            small = 1;
        per_line = (fb.width - 48) / (probe_font_w() * small);
        if (per_line > sizeof(line) - 1)
            per_line = sizeof(line) - 1;
        if (!per_line)
            per_line = 1;
        b32_lines = (b32_len + per_line - 1) / per_line;
        rows_small = 2 + d->tag_count + b32_lines;
        if (s <= 2 || 8 * probe_font_h() * s + rows_small * probe_font_h() * small
                          <= fb.height * 9 / 10)
            break;
    }
    scale = s;
    lh = probe_font_h() * scale;
    lh_s = probe_font_h() * small;

    y = fb.height - (8 * lh + rows_small * lh_s) - fb.height / 20;
    if ((int)y < 0)
        y = 0;
    fb_fill(0, y ? y - 8 : 0, fb.width, fb.height - (y ? y - 8 : 0), (rgb_t){0, 0, 0});

    fb_draw_scaled_text(24, y, scale, probe_white, title);
    y += lh;
    snprintf(line, sizeof(line), "%s %s", d->model, d->target);
    fb_draw_scaled_text(24, y, scale, probe_white, line);
    y += lh;
    for (i = 0; i < 3; i++) {
        int base = i * 3;

        if (base + 2 < PROBE_SGX_WORDS)
            snprintf(line, sizeof(line), "%06x=%08x %06x=%08x %06x=%08x",
                     probe_sgx_offsets[base], d->sgx_words[base], probe_sgx_offsets[base + 1],
                     d->sgx_words[base + 1], probe_sgx_offsets[base + 2], d->sgx_words[base + 2]);
        else
            snprintf(line, sizeof(line), "%06x=%08x %06x=%08x", probe_sgx_offsets[base],
                     d->sgx_words[base], probe_sgx_offsets[base + 1], d->sgx_words[base + 1]);
        fb_draw_scaled_text(24, y, scale, probe_white, line);
        y += lh;
    }
    snprintf(line, sizeof(line), "fam=%d var=%d cores/cl=%d cl/die=%d dies=%d faults=%d",
             d->family, d->variant, d->cores_per_cluster, d->clusters_per_die, d->dies,
             d->fault_count);
    fb_draw_scaled_text(24, y, scale, probe_white, line);
    y += lh;
    probe_hexbuf(hex, d->text_sha, 32);
    for (i = 0; i < 2; i++) {
        memcpy(line, hex + i * 32, 32);
        line[32] = '\0';
        fb_draw_scaled_text(24, y, scale, probe_white, line);
        y += lh;
    }

    /* small rows: full digest, stats, base32 backup */
    fb_draw_scaled_text(24, y, small, probe_white, hex);
    y += lh_s;
    snprintf(line, sizeof(line), "size=%u nonzero=%u tags=%u powered=%d", d->text_size,
             d->nonzero_count, d->tag_count, d->powered_gpu);
    fb_draw_scaled_text(24, y, small, probe_white, line);
    y += lh_s;
    for (i = 0; i < (int)d->tag_count; i++) {
        char hex2[80], off[12];

        probe_hex(off, d->tags[i].offset, 8);
        probe_hexbuf(hex2, d->tags[i].sha, 32);
        snprintf(line, sizeof(line), "tag@%s %s", off, hex2);
        fb_draw_scaled_text(24, y, small, probe_white, line);
        y += lh_s;
    }
    for (i = 0; i < (int)b32_lines; i++) {
        u32 n = per_line;

        if (i * per_line + n > b32_len)
            n = b32_len - i * per_line;
        memcpy(line, b32 + i * per_line, n);
        line[n] = '\0';
        fb_draw_scaled_text(24, y, small, probe_white, line);
        y += lh_s;
    }
    fb_draw_scaled_text(24, y, scale, probe_green, "DONE");
    fb_update();
}

static void probe_refuse_screen(const char *why)
{
    if (!fb.ptr)
        return;
    fb_fill(0, fb.height - 3 * probe_font_h() * 2 - 32, fb.width, 3 * probe_font_h() * 2 + 32,
            (rgb_t){0, 0, 0});
    fb_draw_scaled_text(24, fb.height - 3 * probe_font_h() * 2 - 16, 2, probe_red,
                        "T6032 PROBE: WRONG BOARD - REFUSED");
    fb_draw_scaled_text(24, fb.height - probe_font_h() * 2 - 8, 2, probe_red, why);
    fb_update();
}

/* One progress line before each risky step, on screen and console, so a
 * photo shows exactly where a stop happened. */
static void probe_progress(const char *msg)
{
    printf("probe: %s\n", msg);
    if (!fb.ptr)
        return;
    fb_fill(0, fb.height - 64, fb.width, 64, (rgb_t){0, 0, 0});
    fb_draw_scaled_text(24, fb.height - 56, 2, probe_white, msg);
    fb_update();
}

/* ---- probe proper ---- */

static int probe_has_str(const char *hay, u32 len, const char *needle)
{
    u32 nlen = strlen(needle);

    if (len < nlen)
        return 0;
    for (u32 i = 0; i + nlen <= len; i++)
        if (!memcmp(hay + i, needle, nlen))
            return 1;
    return 0;
}

static void probe_hash_text(const u8 *text, u32 size, probe_t6032_data *d)
{
    sha256_ctx ctx;
    u32 i, nz = 0, off;
    static const u8 zeros[8] = {0};
    const u32 tag = PROBE_GKTS_TAG;

    for (i = 0; i < size; i++)
        nz += text[i] != 0;
    d->nonzero_count = nz;
    d->text_size = size;

    sha256(text, size, d->text_sha);

    d->tag_count = 0;
    for (off = 0; off + 12 <= size && d->tag_count < PROBE_MAX_TAGS; off++) {
        if (memcmp(&text[off], &tag, 4))
            continue;
        sha256_init(&ctx);
        sha256_update(&ctx, text, off + 4);
        sha256_update(&ctx, zeros, 8);
        sha256_update(&ctx, text + off + 12, size - (off + 12));
        sha256_final(&ctx, d->tags[d->tag_count].sha);
        d->tags[d->tag_count].offset = off;
        d->tag_count++;
    }
}

static int probe_board_ok(char *model, u32 model_max, char *target, u32 target_max)
{
    const void *p;
    u32 len;
    int ok = 0;

    p = probe_adt_prop(0, "compatible", &len);
    ok = p && probe_has_str(p, len, "J575dAP");
    p = probe_adt_prop(0, "target-type", &len);
    ok = ok && p && len >= 6 && !memcmp(p, "J575d", 6);
    if (p && len < target_max) {
        memcpy(target, p, len);
        target[len] = '\0';
    } else {
        memcpy(target, "?", 2);
    }
    p = probe_adt_prop(0, "model", &len);
    if (p && len < model_max) {
        memcpy(model, p, len);
        model[len] = '\0';
    } else {
        memcpy(model, "?", 2);
    }
    return ok;
}

void probe_t6032_run(void)
{
    probe_t6032_data d;
    const void *p;
    u32 len, i;
    char b32[600];
    char line[320];
    u8 record[512];
    int rec_len, faulted;
    u64 sgx_base = 0;

    if (chip_id != T6032)
        return; /* never on any other chip */

    memset(&d, 0, sizeof(d));
    d.chip_id = chip_id;

    /* Board gate: identity properties only, everything else stays untouched. */
    if (!probe_board_ok((char *)d.model, sizeof(d.model), (char *)d.target, sizeof(d.target))) {
        printf("probe: refusing: not a j575dap Mac Studio (chip_id=%x)\n", chip_id);
        probe_refuse_screen("needs j575dap / J575d");
        return;
    }

    printf("\nprobe: " PROBE_TITLE " (results on screen; photo is the transport)\n");

    probe_progress("gfx-asc segments...");
    d.seg_count = 0;
    i = probe_adt_path("/arm-io/gfx-asc");
    p = i >= 0 ? probe_adt_prop(i, "segment-ranges", &len) : NULL;
    if (p) {
        for (i = 0; i < PROBE_MAX_SEGS && (i + 1) * 32 <= len; i++) {
            memcpy(&d.seg_phys[i], (const u8 *)p + 32 * i, 8);
            memcpy(&d.seg_size[i], (const u8 *)p + 32 * i + 24, 8);
            d.seg_size[i] &= 0xffffffff;
            printf("probe: seg%d phys=%llx size=%llx\n", i, d.seg_phys[i], d.seg_size[i]);
            d.seg_count++;
        }
    } else {
        printf("probe: no segment-ranges; gpu text not read\n");
    }

    if (d.seg_count && d.seg_size[0]) {
        u32 size = d.seg_size[0];
        u8 *text;

        if (size > 512 << 20) {
            printf("probe: segment size %u capped to 512MiB for the scan\n", size);
            size = 512 << 20;
        }
        probe_progress("reading gpu text...");
        text = malloc(size);
        if (!text || probe_mem_read(d.seg_phys[0], text, size)) {
            printf("probe: gpu text not readable (bus fault or no memory)\n");
            d.fault_count++;
            free(text);
        } else {
            probe_progress("hashing gpu text...");
            probe_hash_text(text, size, &d);
            free(text);
        }
    }

    probe_progress("sgx words...");
    if (probe_sgx_base(&sgx_base)) {
        printf("probe: no sgx node; words not read\n");
    } else {
        printf("probe: sgx base %llx\n", sgx_base);
        d.sgx_words[0] = probe_guarded_read32(sgx_base + probe_sgx_offsets[0], &faulted);
        if (faulted) {
            printf("probe: sgx idle; powering domain via pmgr (lost at restart)\n");
            probe_progress("powering sgx via pmgr...");
            d.powered_gpu = 1;
            probe_power_enable("/arm-io/sgx");
        }
        for (i = faulted ? 0 : 1; i < PROBE_SGX_WORDS; i++) {
            d.sgx_words[i] = probe_guarded_read32(sgx_base + probe_sgx_offsets[i], &faulted);
            if (faulted)
                d.sgx_words[i] = 0xffffffff;
        }
        if (!d.sgx_words[0] || d.sgx_words[0] == 0xffffffff)
            memset(&d.family, 0, 5); /* decoded fields stay unknown */
        else
            probe_decode_hwid(d.sgx_words[0], d.sgx_words[1], &d);
    }
    d.fault_count += probe_faults;

    probe_progress("record...");
    rec_len = probe_record_pack(&d, record, sizeof(record));
    if (rec_len < 0 || probe_base32_encode(record, rec_len, b32, sizeof(b32)) < 0) {
        printf("probe: record packing failed\n");
        probe_refuse_screen("record packing failed");
        return;
    }

    printf("probe: %s\n", PROBE_TITLE);
    printf("probe: model=%s target=%s\n", d.model, d.target);
    for (i = 0; i < PROBE_SGX_WORDS; i++) {
        char off[8], val[12];

        probe_hex(off, probe_sgx_offsets[i], 6);
        probe_hex(val, d.sgx_words[i], 8);
        printf("probe: sgx+%s = %s\n", off, val);
    }
    printf("probe: fam=%d var=%d cores/cl=%d cl/die=%d dies=%d faults=%d\n", d.family, d.variant,
           d.cores_per_cluster, d.clusters_per_die, d.dies, d.fault_count);
    probe_hexbuf(line, d.text_sha, 32);
    printf("probe: gpu text sha256 %s\n", line);
    for (i = 0; i < d.tag_count; i++) {
        probe_hex(line, d.tags[i].offset, 8);
        probe_hexbuf(line + 8, d.tags[i].sha, 32);
        printf("probe: tag@%s zeroed sha256 %s\n", line, line + 8);
    }
    printf("probe: size=%u nonzero=%u powered=%d\n", d.text_size, d.nonzero_count, d.powered_gpu);
    printf("probe: record (base32, crc32 inside):\n%s\n", b32);

    probe_render(&d, PROBE_TITLE, b32);
    printf("probe: done\n");
    fb_set_active(false); /* later console lines would tear the screen */
}

void probe_fb_demo_run(void)
{
    probe_t6032_data d;
    u8 record[512];
    char b32[600];
    int rec_len, i;

    memset(&d, 0, sizeof(d));
    memcpy(d.model, "DEMO", 5);
    memcpy(d.target, "DEMO", 5);
    d.chip_id = chip_id;
    d.sgx_words[0] = 0x07040000;
    d.sgx_words[1] = 0x0002040a;
    d.sgx_words[2] = 0x0a040014;
    for (i = 3; i < PROBE_SGX_WORDS; i++)
        d.sgx_words[i] = 0xdead0000 + i;
    probe_decode_hwid(d.sgx_words[0], d.sgx_words[1], &d);
    d.seg_count = 1;
    d.seg_phys[0] = 0x100000abcull;
    d.seg_size[0] = 0x123456;
    d.text_size = 0x123456;
    d.nonzero_count = 0xf0001;
    d.fault_count = 0;
    for (i = 0; i < 32; i++)
        d.text_sha[i] = 0x30 + (i % 10);
    d.tag_count = 1;
    d.tags[0].offset = 0x1234;
    for (i = 0; i < 32; i++)
        d.tags[0].sha[i] = 0xa0 | i;

    rec_len = probe_record_pack(&d, record, sizeof(record));
    if (rec_len < 0 || probe_base32_encode(record, rec_len, b32, sizeof(b32)) < 0)
        return;
    printf("probe: DEMO screen (no hardware touched)\n");
    probe_render(&d, "T6032 PROBE DEMO", b32);
    fb_set_active(false);
}
