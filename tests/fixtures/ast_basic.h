/* ast_basic.h — header included by ast_basic.c.
 *
 * Exercises entities defined inside a header that is part of the repository:
 * they must be recorded with repo-relative locations and repated records from
 * several TUs are deduplicated later by the store.
 */
#ifndef AST_BASIC_H
#define AST_BASIC_H

struct hdr_record {
    int field_a;
    long field_b;
};

typedef unsigned long uint64_alias;

enum hdr_enum {
    HDR_A = 1,
    HDR_B = 2
};

static inline int header_inline(int x)
{
    return x + 1;
}

extern int declared_in_header(int x);

#endif /* AST_BASIC_H */
