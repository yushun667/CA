/* Precision fixture for ca.analyze: false-positive reduction patterns.
 *
 * Covers: acquire/release guarded by the same condition expression, infinite
 * loops that never fall through to the function exit, panic()-style noreturn
 * paths, and one genuine leak that must still be reported.
 */
struct lock {
    int locked;
};

extern void acquire(struct lock *lk);
extern void release(struct lock *lk);
extern void *malloc(unsigned long size);
extern void free(void *ptr);
extern void panic(const char *reason);

/* printk-style pattern: the same condition guards acquire and release. */
int guarded_pair(struct lock *lk, int panicking)
{
    if (panicking == 0)
        acquire(lk);
    if (panicking == 0)
        release(lk);
    return 0;
}

/* for(;;) with break: the loop never falls through on its own. */
int loop_with_break(struct lock *lk, int x)
{
    for (;;) {
        acquire(lk);
        if (x)
            break;
        release(lk);
        return 1;
    }
    release(lk);
    return 0;
}

/* while (1) with a conditional break. */
int while_with_break(struct lock *lk, int x)
{
    acquire(lk);
    while (1) {
        if (x)
            break;
    }
    release(lk);
    return 0;
}

/* No exit path at all: must not be reported as a leak. */
int spin_no_exit(struct lock *lk)
{
    acquire(lk);
    for (;;) {
    }
}

/* panic() is noreturn: code after it is unreachable. */
int panic_path(int x)
{
    void *p = malloc(8);
    if (!x)
        panic("bad state");
    free(p);
    return 0;
}

/* Conditional acquire without a matching release: genuine leak (TP). */
int still_leaks(struct lock *lk, int flag)
{
    if (flag)
        acquire(lk);
    return 0;
}

/* Release of a parameter: ownership comes from another function, so no
 * "unreleased" finding should be produced for it. */
void release_param(struct lock *lk)
{
    release(lk);
}
