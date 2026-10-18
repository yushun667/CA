/* ast_ohos.c — OpenHarmony-style compile-argument compatibility fixture (P3-5).
 *
 * Parsed by tests/test_parse_ohos_compat.py with a *real-looking* OH clang argv:
 * `--target=aarch64-linux-ohos`, `--sysroot=<tmp sysroot>`, `-march=armv8-a`,
 * `-D__OHOS__` and an unknown `-Xclang` vendor passthrough flag. The markers
 * below prove the parser kept the aarch64/ELF target (not a host replay) and
 * that the sysroot include path was honoured:
 *   - ohos_marker/ohos_aarch64_marker only exist for the OHOS target
 *   - host_apple_marker must never be recorded (a host-replay leak)
 *   - ohos_sysroot_cb comes from <ohos_compat_stub.h> placed in the sysroot
 *
 * Everything after the markers is C code from the OH code base style: an
 * extern "C"-less bare function pointer typedef plus a dispatch function.
 */
#ifdef __OHOS__
#define CA_OHOS_TU 1
#else
#define CA_OHOS_TU 0
#endif

#if defined(__aarch64__) && defined(__ELF__) && !defined(__APPLE__)
int ohos_aarch64_marker(void)
{
    return CA_OHOS_TU;
}
#endif

#ifdef __APPLE__
int host_apple_marker(void)
{
    return -1;
}
#endif

#include <ohos_compat_stub.h>

typedef int (*ohos_callback_t)(int);

int ohos_dispatch(ohos_callback_t cb, int value)
{
    return cb(value) + ohos_stub_add(value, 1);
}
