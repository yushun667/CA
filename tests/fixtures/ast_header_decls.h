/* ast_header_decls.h — header declaration-index fixture (task-25 item 4).
 *
 * Construction intent (see tests/test_parse_header_decls.py): under the
 * large-corpus parse mode `record_scope="main_file"` header entities are
 * normally filtered out. With `emit_header_declarations=True` this header's
 * function *declarations* must still be indexed (is_definition=False) so store
 * queries can report "declaration location / definition missing", while header
 * *definitions* (hdr_inline_impl) stay excluded and defined-in-.c functions are
 * not duplicated.
 *
 * `hdr_declared_only` has no definition anywhere, so it can only ever appear as
 * a declaration record. `hdr_defined_in_c` is defined in ast_header_decls.c and
 * must be recorded there as a definition (declaration replaced), with the same
 * USR-based id in both TUs.
 */
#ifndef CA_AST_HEADER_DECLS_H
#define CA_AST_HEADER_DECLS_H

int hdr_defined_in_c(int x);

int hdr_declared_only(int x, const char *name);

typedef int (*hdr_cb_t)(int);

int hdr_register_cb(hdr_cb_t cb);

static inline int hdr_inline_impl(int x)
{
    return x + 1;
}

#endif /* CA_AST_HEADER_DECLS_H */
