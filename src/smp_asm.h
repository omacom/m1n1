/* SPDX-License-Identifier: MIT */

#ifndef __SMP_ASM_H__
#define __SMP_ASM_H__

/* Override at build time, e.g. EXTRA_CFLAGS=-DMAX_CPUS=24, to shrink secondary_stacks. */
#ifndef MAX_CPUS
#define MAX_CPUS 32
#endif
#define MAX_EL3_CPUS 4

#define SECONDARY_STACK_SIZE 0x10000

#endif
