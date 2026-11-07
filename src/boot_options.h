/* SPDX-License-Identifier: MIT */
#ifndef BOOT_OPTIONS_H
#define BOOT_OPTIONS_H
#include <stdbool.h>
#include <stddef.h>

bool boot_option_present(const char *line, size_t capacity, const char *option);
void boot_options_init(const char *line, size_t capacity);
bool m1n1_debug_enabled(void);
void boot_options_set_stage1(bool stage1);
bool m1n1_proxy_enabled(void);
bool m1n1_wdt_keep_enabled(void);
#endif
