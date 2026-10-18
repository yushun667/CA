/* Path-normalization fixture for ca.analyze.
 *
 * Every Sloc emitted by the analyzer must be a repo-relative POSIX path that
 * resolves inside the analyzed repository; cursor paths that libclang reports
 * relative to the compiler working directory (``./include/...``) are resolved
 * against ``ParsedUnit.tu.repo_root``.
 */
struct path_lock {
    int locked;
};

extern void *kmalloc(unsigned long size);
extern void kfree(const void *ptr);
extern void spin_lock(struct path_lock *lk);
extern void spin_unlock(struct path_lock *lk);

int path_fixture(int flag)
{
    char *buf = kmalloc(16);
    int value = 0;

    if (!buf)
        return -1;
    spin_lock((struct path_lock *)buf);
    if (flag)
        value = 1;
    spin_unlock((struct path_lock *)buf);
    kfree(buf);
    return value;
}
