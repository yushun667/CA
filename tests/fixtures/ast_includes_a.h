/* ast_includes_a.h — first-level repo header that includes another repo header. */
#ifndef CA_AST_INCLUDES_A_H
#define CA_AST_INCLUDES_A_H

#include "ast_includes_b.h"

static inline int ast_includes_value(void)
{
    return AST_INCLUDES_B_VALUE;
}

#endif /* CA_AST_INCLUDES_A_H */
