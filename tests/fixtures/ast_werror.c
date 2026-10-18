/* ast_werror.c — fixture for argument relaxation: compiling with -Werror
 * elevates the unused-variable warning below to an error, so the parser must
 * retry without -Werror and still produce a clean result.
 */
int uses_werror(int x)
{
    int unused;
    return x;
}
