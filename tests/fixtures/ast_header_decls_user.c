/* ast_header_decls_user.c — second TU including the shared header.
 *
 * Used to verify that header declaration ids are stable across translation
 * units (external-linkage USR based) and that declarations of functions defined
 * in another TU are still indexed here.
 */
#include "ast_header_decls.h"

int hdr_user(int x)
{
    return hdr_defined_in_c(x) + hdr_declared_only(x, "u");
}
