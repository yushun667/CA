/* Cross-TU callback registration fixture (caller side).
 *
 * Indirect calls reach globals whose function pointers are registered in
 * ast_crosstu_registry.c; the local-instance and wrong-signature calls must
 * stay unresolved.
 */

typedef int (*handler_fn)(int);

struct ops {
    handler_fn fn;
    handler_fn alt;
};

extern struct ops g_ops_a;
extern struct ops g_ops_b;
extern handler_fn g_table[2];
extern handler_fn g_table2[2];
extern handler_fn g_init_table[2];
extern int (*g_voidcb)(int);
extern handler_fn provider(void);
extern int apply(handler_fn cb, int x);

static int caller_impl(int x) { return x - 1; }

int call_ops_a(int x) { return g_ops_a.fn(x); }

int call_ops_b(int x) { return g_ops_b.fn(x); }

int call_table(int i, int x) { return g_table[i](x); }

int call_table2(int i, int x) { return g_table2[i](x); }

int call_init_table(int i, int x) { return g_init_table[i](x); }

int call_param(int x) { return apply(caller_impl, x); }

int call_return(int x) { return provider()(x); }

int call_wrong(int x) { return g_voidcb(x); }

int call_local(int x) { struct ops local; return local.fn(x); }
