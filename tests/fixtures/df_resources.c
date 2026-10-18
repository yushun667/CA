/* Resource/data-flow fixture for ca.analyze.
 *
 * Intended coverage: balanced malloc/free, leak on an early-return path,
 * double free, balanced and leaking locks, balanced/leaking file handles,
 * conditional free on both branches, a release without any matching acquire,
 * and custom APIs that only the configurable annotation table knows about
 * (`my_alloc` / `my_free`).
 */
/* Self-contained: libc prototypes are declared locally so the fixture parses
 * without a host SDK sysroot. */
typedef struct df_file FILE;
extern void *malloc(unsigned long size);
extern void free(void *ptr);
extern int open(const char *path, int flags);
extern int close(int fd);
extern FILE *fopen(const char *path, const char *mode);
extern int fclose(FILE *stream);

struct lock {
    int locked;
};

extern void spin_lock(struct lock *lk);
extern void spin_unlock(struct lock *lk);
extern void *kmalloc(unsigned long size);
extern void kfree(void *ptr);
extern void *my_alloc(unsigned long size);
extern void my_free(void *ptr);

int balanced_malloc(int n)
{
    char *p = malloc(n);
    if (p == 0)
        return -1;
    free(p);
    return 0;
}

int leak_on_error(int n)
{
    char *p = malloc(n);
    if (!p)
        return -1;
    if (n > 10)
        return -2; /* leak: p is not freed on this path */
    free(p);
    return 0;
}

int double_free(void)
{
    char *p = malloc(8);
    free(p);
    free(p); /* double release */
    return 0;
}

int lock_pair(struct lock *lk, int x)
{
    spin_lock(lk);
    if (x)
        spin_unlock(lk);
    else
        spin_unlock(lk);
    return 0;
}

int lock_leak(struct lock *lk, int x)
{
    spin_lock(lk);
    if (x)
        return -1; /* leak: lock not released on this path */
    spin_unlock(lk);
    return 0;
}

int file_pair(void)
{
    int fd = open("/tmp/ca-df", 0);
    if (fd < 0)
        return -1;
    close(fd);
    return 0;
}

int file_leak(void)
{
    FILE *fp = fopen("/tmp/ca-df", "r");
    if (!fp)
        return -1;
    return 0; /* leak: fp is not closed */
}

int conditional_free(int x)
{
    char *p = malloc(16);
    if (x)
        free(p);
    else
        free(p);
    return 0;
}

int unmatched_release(int n)
{
    char *owned = malloc(n);        /* makes this function resource-managing */
    void *scratch = (void *)0x1000; /* local, never acquired */
    if (!owned)
        return -1;
    free(owned);
    kfree(scratch);                 /* release with no acquiring call */
    return 0;
}

int custom_alloc(int n)
{
    char *p = my_alloc(n);
    if (!p)
        return -1;
    my_free(p);
    return 0;
}

int custom_leak(void)
{
    char *p = my_alloc(8);
    return p != 0; /* leak only once my_alloc is configured */
}
