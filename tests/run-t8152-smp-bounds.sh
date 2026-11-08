#!/bin/sh
# Host test of smp_init_t8152() (src/smp.c) against MAX_CPUS overrides, with ASan/UBSan: below 12
# it must refuse without an out-of-bounds write; at 12 and the default 32 it accepts 12 CPUs.
#
#   sh tests/run-t8152-smp-bounds.sh
set -eu
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

awk '/^static int t8152_cpu_index\(/ { f = 1 } f { print } f && /^}$/ { exit }' src/smp.c \
    > "$tmp/t8152_smp.inc"
awk '/^static int smp_init_t8152\(/ { f = 1 } f { print } f && /^}$/ { exit }' src/smp.c \
    >> "$tmp/t8152_smp.inc"
grep -q 'smp_init_t8152(void)' "$tmp/t8152_smp.inc"

fail=0
for n in 8 11 12 24 32; do
    ${CC:-cc} -std=gnu11 -O1 -g -Wall -Wno-unused-function -Wno-unused-parameter \
        -fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all \
        -DMAX_CPUS=$n -I"$tmp" tests/t8152_smp_bounds_host.c -o "$tmp/t8152-$n"
    "$tmp/t8152-$n" || fail=1
done
[ $fail = 0 ] && echo "T8152 SMP bounds: every MAX_CPUS passes"
exit $fail
