/* Cross-TU callback registration fixture (registry side).
 *
 * Global callback tables are populated here; the indirect calls live in
 * ast_crosstu_callers.c. The resolver must join facts across both TUs.
 */

typedef int (*handler_fn)(int);

struct ops {
    handler_fn fn;
    handler_fn alt;
};

int impl_a(int x);
int impl_b(int x);
void impl_void(void);

struct ops g_ops_a;
struct ops g_ops_b;
handler_fn g_table[2];
handler_fn g_table2[2];
handler_fn g_init_table[2] = {[0] = impl_a, [1] = impl_b};
void (*g_voidcb)(void);

int impl_a(int x) { return x + 1; }
int impl_b(int x) { return x + 2; }
void impl_void(void) { }

void setup_globals(void)
{
    g_ops_a.fn = impl_a;
    g_ops_b.fn = impl_b;
    g_table[0] = impl_a;
    g_table[1] = impl_b;
    g_table2[0] = impl_a;
    g_table2[1] = impl_b;
    g_voidcb = impl_void;
}

int apply(handler_fn cb, int x)
{
    return cb(x);
}

handler_fn provider(void)
{
    return impl_a;
}

void set_local_only(void)
{
    struct ops local;
    local.fn = impl_a;
}
