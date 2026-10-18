/* ast_basic.c — parse module fixture.
 *
 * Construction intent (see tests/test_parse_basic.py):
 *   - definitions and declarations of the same external function (add)
 *   - a static (internal-linkage) helper defined in this file
 *   - struct/union/enum/typedef definitions, bitfields, nested/anonymous struct
 *   - direct calls, a call through a function pointer, a macro-generated call
 *   - a variadic declaration and a static inline function from ast_basic.h
 *
 * Expected boundaries: no libc headers are included, so the file parses with
 * only -std=gnu99; the macro call is attributed to its expansion site.
 */
#include "ast_basic.h"

struct point {
    int x;
    int y;
};

typedef struct point point_t;

union value {
    int i;
    float f;
};

enum color {
    COLOR_RED,
    COLOR_GREEN = 5,
    COLOR_BLUE
};

struct flags {
    unsigned int a : 3;
    unsigned int b : 5;
    int c;
};

struct nested {
    struct point origin;
    union {
        int as_int;
        float as_float;
    } variant;
    struct {
        int inner;
    };
};

static int static_helper(int v)
{
    return v * 2;
}

int add(int a, int b);

int add(int a, int b)
{
    return a + b;
}

int callee(int x)
{
    return x + 1;
}

int caller(int x)
{
    int (*fp)(int) = callee;
    int r = callee(x);
    r += fp(x);
    r += static_helper(x);
    r += add(x, x);
    r += header_inline(x);
    return r;
}

#define CALL_MACRO(x) callee(x)

int macro_user(int x)
{
    return CALL_MACRO(x);
}

int variadic_decl(const char *fmt, ...);
