/* SPDX-License-Identifier: MIT */

#ifndef PCIE_T6050_H
#define PCIE_T6050_H

#include "types.h"

/* Returned when storage changed under the training: the SoC must be reset, not booted */
#define N1_STORAGE_LOST (-2)

/*
 * Train T6050 apcie0 port 0 (the N1) from cold: 0 once its link is up and Linux can adopt it,
 * N1_STORAGE_LOST if storage changed under it, -1 after any other stop (port 0 stays down).
 */
int pcie_t6050_n1_train(bool smc_release);

/*
 * The same with ANS started for the storage checks and stopped again after, if it was stopped
 * (chosen.asahi,t6050-n1=3|4); with ANS running, the same as pcie_t6050_n1_train().
 */
int pcie_t6050_n1_train_ans(bool smc_release);

/* Storage is in doubt and Linux could not mount its root: reset, so iBoot counts a failed boot */
void __attribute__((noreturn)) pcie_t6050_storage_lost(const char *why);

#endif
