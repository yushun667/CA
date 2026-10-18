/* ast_static_b/unit.c — duplicated basename fixture (see ast_static_a/unit.c).
 *
 * Mirrors ast_static_a/unit.c so the test can prove that internal-linkage ids
 * include the repo-relative file rather than only the shared basename USR.
 */
static int helper(int x)
{
    return x - 1;
}

int unit_b(int x)
{
    return helper(x);
}
