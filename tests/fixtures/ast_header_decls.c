/* ast_header_decls.c — definition side of the header declaration-index fixture.
 *
 * Defines hdr_defined_in_c (so its declaration from ast_header_decls.h is
 * replaced by the definition within this TU) and calls header-declared helpers,
 * proving call resolution does not depend on the declaration index.
 */
#include "ast_header_decls.h"

int hdr_defined_in_c(int x)
{
    return hdr_inline_impl(x);
}

int hdr_use_declared(int x)
{
    return hdr_declared_only(x, "v") + hdr_register_cb(0);
}
