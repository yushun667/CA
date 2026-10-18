/* Macro-expansion fixture for ca.analyze.
 *
 * Macro bodies must not be persisted as source-level assignment/branch facts:
 * their extents can mix the macro definition with the usage site and produce
 * huge or misleading lhs/rhs/condition text. Expected: macro-internal control
 * flow stays opaque, all recorded text stays within the configured limits, and
 * ordinary source facts around the macro calls are still recovered.
 */
#define SET_SCORE(v) (score = (v))
#define POSITIVE(x) ((x) > 0)
#define ASSERT_POSITIVE(x) do { if (!(x)) { } } while (0)

int macro_flow(int x)
{
    int score = 0;

    SET_SCORE(5);
    ASSERT_POSITIVE(x);

    if (POSITIVE(x))
        return score;

    return 0;
}

/* Macro-provided loop header: the body is ordinary source and its facts must
 * still be recovered even though the `for` statement comes from the macro. */
#define FOR_EACH_NODE(item, head) \
    for ((item) = (head); (item) != 0; (item) = (item)->next)

struct macro_node {
    struct macro_node *next;
    char *extra;
};

extern void *malloc(unsigned long size);
extern void free(void *ptr);

int free_macro_loop(struct macro_node *head)
{
    struct macro_node *item;
    char *buf = malloc(8);

    FOR_EACH_NODE(item, head) {
        free(item->extra);
    }
    free(buf);
    return 0;
}
