/* ast_include/main.c — verifies -working-directory handling.
 *
 * The angle-bracket include is only found when the parser resolves "-I." against
 * the compilation unit's directory (ast_include/), not against the process CWD.
 */
#include <fixture_only.h>

int include_user(int x)
{
    return fixture_only(x);
}
