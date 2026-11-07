/* SPDX-License-Identifier: MIT */
#include "boot_options.h"

static bool debug_enabled;
static bool proxy_requested;
static bool wdt_keep;
static bool is_stage1;

static bool boot_space(char c)
{
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

bool boot_option_present(const char *line, size_t capacity, const char *option)
{
    if (!line || !option || !option[0])
        return false;
    size_t length = 0;
    while (length < capacity && line[length])
        length++;
    if (length == capacity)
        return false;
    for (size_t pos = 0; pos < length;) {
        if (boot_space(line[pos])) {
            pos++;
            continue;
        }
        size_t end = pos;
        while (end < length && !boot_space(line[end]))
            end++;
        size_t i = 0;
        while (pos + i < end && option[i] && line[pos + i] == option[i])
            i++;
        if (pos + i == end && !option[i])
            return true;
        pos = end;
    }
    return false;
}

void boot_options_init(const char *line, size_t capacity)
{
    debug_enabled = boot_option_present(line, capacity, "m1n1-debug");
    proxy_requested = boot_option_present(line, capacity, "m1n1-proxy");
    wdt_keep = boot_option_present(line, capacity, "m1n1.wdt=keep");
    is_stage1 = true;
}

bool m1n1_debug_enabled(void)
{
    return debug_enabled;
}

void boot_options_set_stage1(bool stage1)
{
    is_stage1 = stage1;
}

bool m1n1_proxy_enabled(void)
{
    return is_stage1 && proxy_requested;
}

bool m1n1_wdt_keep_enabled(void)
{
    return wdt_keep;
}
