/* SPDX-License-Identifier: MIT */

#ifndef HEAPBLOCK_H
#define HEAPBLOCK_H

#include "types.h"

void heapblock_init(void);

void *heapblock_alloc(size_t size);
void *heapblock_alloc_aligned(size_t size, size_t align);
void heapblock_set_limit(void *limit);
uintptr_t heapblock_high_water(void);
#ifdef T6050_MTP_RSV
size_t heapblock_room(size_t align);
#endif

#endif
