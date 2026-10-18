/* Self-candidate suppression fixture for ca.analyze.
 *
 * A field-name suffix match that resolves back to the calling function is a
 * same-named field collision (no exact object/registration match); the
 * resolver must not emit that candidate.
 */
struct ops_self {
    int (*fn)(int);
};

static struct ops_self *other_ops;

int target_self(struct ops_self *o)
{
    return o->fn(1); /* only a suffix-keyed registration exists: other_ops->fn */
}

void setup_self(struct ops_self *o)
{
    other_ops->fn = target_self;
    (void)o;
}
