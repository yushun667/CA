/* ast_fnptr.c — C function-pointer type/signature extraction fixture (P3-5).
 *
 * Construction intent (see tests/test_parse_fnptr.py): the analyzer needs the
 * *type spelling* of function-pointer parameters, fields and typedefs to match
 * indirect-call candidates. This TU pins down:
 *   - a function-pointer typedef (`ast_callback_t`) and its underlying type
 *   - function-pointer parameters, both typedef'd and written inline
 *   - function-pointer struct fields (typedef'd / raw / void-returning)
 *   - indirect calls through parameters, typedef'd parameters and struct fields
 *
 * Known boundary: the schema's CallRecord has no function-pointer type field;
 * analyzers must join a call's callee variable name back to the enclosing
 * ParamRecord/FieldRecord/VAR_DECL type_spelling.
 */
typedef int (*ast_callback_t)(int);
typedef void (*ast_notify_t)(void *ctx, int code);

struct ast_ops {
    ast_callback_t handler;
    int (*raw_fn)(int, int);
    ast_notify_t notify;
};

int ast_add(int a, int b)
{
    return a + b;
}

int ast_apply_typedef(ast_callback_t cb, int value)
{
    return cb(value);
}

int ast_apply_inline(int (*fn)(int), int value)
{
    return fn(value);
}

int ast_apply_two(int (*fn)(int), ast_callback_t cb, int value)
{
    return fn(value) + cb(value);
}

int ast_use_ops(struct ast_ops *ops, int value)
{
    return ops->raw_fn(value, value) + ops->handler(value);
}

void ast_dispatch(ast_notify_t notify, void *ctx)
{
    notify(ctx, 0);
}
