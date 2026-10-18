/* Callback / function-pointer resolution fixture for ca.analyze.
 *
 * Positive patterns:
 *   - callback registered into a struct field and called there / elsewhere in
 *     the same TU (``o->fn = handler_a`` then ``o->fn()``);
 *   - ``&handler`` address-of form;
 *   - function passed as an argument and called through the parameter;
 *   - function-pointer array + array element call;
 *   - typedef'd callback global assigned from a parameter (two-hop);
 *   - function returning a callback pointer, called via ``factory()()``.
 * Negative patterns (must not produce candidates):
 *   - indirect call through a pointer that never receives a function address;
 *   - assignment with a mismatched function-pointer signature.
 */
struct ops {
    int (*fn)(int);
    void (*notify)(void);
};

typedef int (*cb_t)(int);

int handler_a(int x)
{
    return x + 1;
}

int handler_b(int x)
{
    return x + 2;
}

int handler_neg(int x)
{
    return -x;
}

void cb_void(void)
{
}

static int (*table[2])(int) = {handler_a, handler_b};
static cb_t g_cb;

void register_cb(cb_t cb)
{
    g_cb = cb;
}

void setup_cb(void)
{
    register_cb(handler_b);
}

int call_global(void)
{
    return g_cb(5); /* expects handler_b via setup_cb -> register_cb */
}

static int dispatch(int (*cb)(int), int x)
{
    return cb(x); /* expects handler_neg passed by use_dispatch */
}

int use_dispatch(void)
{
    return dispatch(handler_neg, 3);
}

int store_field(struct ops *o)
{
    o->fn = handler_a;
    return o->fn(1); /* expects handler_a */
}

int call_field(struct ops *o)
{
    return o->fn(2); /* expects handler_a from store_field */
}

int address_of(void)
{
    void (*notify)(void);
    notify = &cb_void;
    notify(); /* expects cb_void */
    return 0;
}

int call_array(int i)
{
    return table[i](4); /* expects handler_a and handler_b */
}

static int (*factory(void))(int)
{
    return handler_a;
}

int call_factory(void)
{
    return factory()(5); /* expects handler_a via factory return */
}

void mismatched(void)
{
    void (*vp)(void);
    vp = (void (*)(void))handler_a; /* signature mismatch: filtered */
    vp();
}

int unmatched(struct ops *o)
{
    int (*fp)(int);
    fp = 0;
    return fp(1); /* never receives a function address: no candidate */
}

static int (*designated[3])(int) = {[0] = handler_a, [2] = handler_b};

int call_designated(void)
{
    return designated[2](6); /* expects handler_a and handler_b */
}
