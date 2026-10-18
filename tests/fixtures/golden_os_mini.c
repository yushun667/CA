/*
 * Hand-written OS-style fixture for the accuracy baseline.
 *
 * Every fact category in golden_os_mini.expected.json is represented by at
 * least one construct here; the expectation file is meant to be exhaustive for
 * entities defined in this file so precision and recall are meaningful:
 *   - functions: external and static definitions (one TU, no headers)
 *   - types: struct, union, enum and typedef definitions
 *   - calls: direct calls only (no function pointers)
 *   - branches: if/while conditions (no switch, no short-circuit operators)
 *   - assignments: declaration initializers plus plain assignments
 *   - returns: explicit return statements only
 *   - resources: kmalloc/kfree, file_open/file_close, spin_lock/spin_unlock
 */

struct node {
    struct node *next;
    int value;
};

struct spinlock {
    int locked;
};

enum node_state {
    NODE_FREE = 0,
    NODE_USED = 1
};

union node_value {
    int integer;
    unsigned int bits;
};

typedef unsigned int uint;

void *kmalloc(uint size);
void kfree(void *ptr);
void spin_lock(struct spinlock *lk);
void spin_unlock(struct spinlock *lk);
int open(const char *path);
int close(int handle);

static struct node *node_alloc(int value)
{
    struct node *n = kmalloc(sizeof(struct node));
    if (n == 0) {
        return 0;
    }
    n->value = value;
    n->next = 0;
    return n;
}

static void node_free(struct node *n)
{
    if (n != 0) {
        n->next = 0;
        kfree(n);
    }
}

struct node *queue_push(struct node *head, int value)
{
    struct node *n = node_alloc(value);
    if (n == 0) {
        return 0;
    }
    n->next = head;
    return n;
}

int queue_pop(struct node *head)
{
    struct node *n = head;
    int value = -1;
    if (n == 0) {
        return -1;
    }
    value = n->value;
    return value;
}

int file_read_all(const char *path)
{
    int handle = open(path);
    if (handle < 0) {
        return -1;
    }
    close(handle);
    return 0;
}

int sum_until(int *values, int count)
{
    int total = 0;
    int i = 0;
    while (i < count) {
        total = total + values[i];
        i = i + 1;
    }
    return total;
}

int lock_inc(struct spinlock *lk, int *counter)
{
    spin_lock(lk);
    *counter = *counter + 1;
    spin_unlock(lk);
    return 0;
}
