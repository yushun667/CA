/* ast_includes_main.c — include dependency contract fixture (task-38).
 *
 * Expecations (see tests/test_parse_includes.py): direct includes are
 * ast_includes_a.h only (repo-local); ast_includes_b.h appears in the header
 * include edges (a -> b), and the system <stddef.h> is excluded because it is
 * outside repo_root.
 */
#include "ast_includes_a.h"
#include <stddef.h>

int includes_main(void)
{
    return ast_includes_value();
}
