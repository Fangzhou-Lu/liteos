/*
 * unwind_personality.c — ARM EHABI personality routine stubs.
 *
 * Why these exist
 * ===============
 * The freestanding `-nostdlib` kernel link does not pull in libgcc/libunwind,
 * so the ARM EHABI personality routines `__aeabi_unwind_cpp_pr0/pr1/pr2` are
 * not normally present. Two facts force us to provide them ourselves:
 *
 *   1) clang on `arm-liteos-ohos` emits `.ARM.exidx` index entries whenever
 *      `-funwind-tables` is enabled. Even non-personality (CANTUNWIND /
 *      inlined) entries require the relocations to resolve to *some* symbol
 *      named `__aeabi_unwind_cpp_pr0` (and friends).
 *   2) The linker script now KEEPs `.ARM.exidx*`, which we need so GDB can
 *      backtrace inside ordinary C functions like VfsExfatChattr after
 *      stopping at a breakpoint reached via a syscall trampoline that has
 *      no `.cfi_*` annotations.
 *
 * The personality routines must exist for the link to succeed, but they
 * are never actually executed in this kernel: we do not throw / catch C++
 * exceptions and the kernel does not run a stack unwinder at runtime. GDB
 * walks `.ARM.exidx` on the *host* side from inside the debugger; it does
 * not call into these symbols on the target.
 *
 * Linux ARM uses the same pattern in `arch/arm/kernel/unwind.c` and
 * `arch/arm/lib/`. We follow that convention here: provide minimal stubs
 * that just return _URC_FAILURE so any accidental runtime call is a clean
 * abort rather than a crash. The stubs are marked weak so a future
 * libgcc-style provider can override them without changes here.
 */

#include <stdint.h>

/* ARM EHABI: enum _Unwind_Reason_Code values. */
#define _URC_FAILURE        9

/* These signatures match the ARM EHABI spec exactly; we cannot let the
 * compiler optimize them away (hence the weak attribute and explicit
 * 'used' so LTO keeps them even if the symbol appears unreferenced
 * outside of `.ARM.exidx` relocations). */

__attribute__((weak, used))
int __aeabi_unwind_cpp_pr0(int state, void *ucbp, void *context)
{
    (void)state;
    (void)ucbp;
    (void)context;
    return _URC_FAILURE;
}

__attribute__((weak, used))
int __aeabi_unwind_cpp_pr1(int state, void *ucbp, void *context)
{
    (void)state;
    (void)ucbp;
    (void)context;
    return _URC_FAILURE;
}

__attribute__((weak, used))
int __aeabi_unwind_cpp_pr2(int state, void *ucbp, void *context)
{
    (void)state;
    (void)ucbp;
    (void)context;
    return _URC_FAILURE;
}
