/* ast_static_a/unit.c — duplicated basename fixture (see ast_static_b/unit.c).
 *
 * Both files define an internal-linkage helper and call it. libclang's USR for
 * static functions only contains the *file name* (c:unit.c@F@helper), so the
 * two helpers must still receive different ca.core.ids ids: internal linkage
 * ids add the repo-relative file.
 */
static int helper(int x)
{
    return x + 1;
}

int unit_a(int x)
{
    return helper(x);
}
