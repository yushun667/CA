/* Linux-kernel style resource fixture for ca.analyze.
 *
 * Self-contained (prototypes only, no kernel headers) and intended to cover
 * the precision patterns that dominate real kernel findings:
 *   - kmem_cache_alloc(cache) / kmem_cache_free(cache, obj) argument order
 *   - kmalloc/free round trips and genuine leaks
 *   - IS_ERR()/PTR_ERR() error-pointer guards (clk_get)
 *   - goto cleanup labels that release every path
 *   - alloc_skb / kfree_skb / consume_skb
 *   - devm_* allocations owned by the driver core (no explicit free needed)
 *   - ownership transfer to the caller via return / struct store
 *   - rcu_read_lock/unlock (no trackable object)
 *   - spin_lock_irqsave/spin_unlock_irqrestore and request_irq/free_irq pairs
 */
struct kmem_cache;
struct sk_buff;
struct device;
struct mutex;
struct resource;

extern void *kmalloc(unsigned long size);
extern void *kzalloc(unsigned long size);
extern void kfree(const void *ptr);
extern void *kmem_cache_alloc(struct kmem_cache *cache);
extern void kmem_cache_free(struct kmem_cache *cache, void *obj);
extern void *devm_kzalloc(struct device *dev, unsigned long size);
extern void *alloc_skb(unsigned int size);
extern void kfree_skb(struct sk_buff *skb);
extern void consume_skb(struct sk_buff *skb);
extern void *clk_get(struct device *dev, const char *id);
extern void clk_put(void *clk);
extern long IS_ERR(const void *ptr);
extern long PTR_ERR(const void *ptr);
extern void mutex_lock(struct mutex *lock);
extern void mutex_unlock(struct mutex *lock);
extern void rcu_read_lock(void);
extern void rcu_read_unlock(void);
extern void spin_lock_irqsave(void *lock, unsigned long flags);
extern void spin_unlock_irqrestore(void *lock, unsigned long flags);
extern int request_irq(unsigned int irq, void *handler, unsigned long flags,
                       const char *name, void *dev);
extern void free_irq(unsigned int irq, void *dev);
extern void *ERR_PTR(long error);

/* kmem_cache round trip: the cache pointer is arg0, the object arg1. */
int cache_roundtrip(struct kmem_cache *cache, int fail)
{
    void *obj = kmem_cache_alloc(cache);
    if (!obj)
        return -1;
    if (fail) {
        kmem_cache_free(cache, obj);
        return -1;
    }
    kmem_cache_free(cache, obj);
    return 0;
}

/* Genuine leak: allocate from a slab cache and never free it. */
int cache_leak(struct kmem_cache *cache)
{
    void *obj = kmem_cache_alloc(cache);
    return obj != 0;
}

/* IS_ERR/PTR_ERR guard: the error-pointer path is not a resource leak. */
int clk_error_path(struct device *dev, long *ret)
{
    void *clk = clk_get(dev, "bus");
    if (IS_ERR(clk)) {
        *ret = PTR_ERR(clk);
        return -1;
    }
    clk_put(clk);
    return 0;
}

/* goto cleanup label: every path releases both allocations. */
int goto_cleanup(int fail)
{
    char *a = kmalloc(8);
    char *b = kmalloc(16);
    int ret = 0;

    if (!a || !b) {
        ret = -1;
        goto out;
    }
    if (fail) {
        ret = -2;
        goto out;
    }
    ret = 0;
out:
    kfree(b);
    kfree(a);
    return ret;
}

/* skb round trip on both the error path and the success path. */
int skb_roundtrip(int fail)
{
    struct sk_buff *skb = alloc_skb(64);

    if (!skb)
        return -1;
    if (fail) {
        kfree_skb(skb);
        return -1;
    }
    consume_skb(skb);
    return 0;
}

/* Genuine skb leak. */
int skb_leak(void)
{
    struct sk_buff *skb = alloc_skb(32);
    return skb != 0;
}

/* devm allocations are owned by the driver core: no explicit free expected. */
int devm_owned(struct device *dev)
{
    char *buf = devm_kzalloc(dev, 128);
    if (!buf)
        return -1;
    buf[0] = 0;
    return 0;
}

/* Ownership returned to the caller (no local free needed). */
char *return_owned(void)
{
    char *buf = kmalloc(64);
    if (!buf)
        return 0;
    buf[0] = 'x';
    return buf;
}

/* Ownership transferred into a struct field. */
struct holder {
    char *data;
    int len;
};

int store_owned(struct holder *h)
{
    h->data = kmalloc(32);
    if (!h->data)
        return -1;
    h->len = 32;
    return 0;
}

/* RCU read section: no trackable object, should only be recorded. */
int rcu_section(int x)
{
    int value;

    rcu_read_lock();
    value = x + 1;
    rcu_read_unlock();
    return value;
}

/* irqsave / irqrestore pair nested inside a mutex section. */
struct ca_state {
    struct mutex *lock;
    int busy;
};

int lock_irqsave_pair(struct ca_state *st, int x)
{
    unsigned long flags;

    mutex_lock(st->lock);
    spin_lock_irqsave(&st->busy, flags);
    x += 1;
    spin_unlock_irqrestore(&st->busy, flags);
    mutex_unlock(st->lock);
    return x;
}

/* request_irq/free_irq pair on both error and success paths. */
int irq_pair(void *handler, void *dev)
{
    unsigned int irq = 5;
    int ret = request_irq(irq, handler, 0, "ca-fixture", dev);

    if (ret)
        return ret;
    free_irq(irq, dev);
    return 0;
}

/* request_irq without free_irq: genuine leak (TP). */
int irq_leak(void *handler, void *dev)
{
    unsigned int irq = 7;
    int ret = request_irq(irq, handler, 0, "ca-fixture", dev);

    return ret;
}

/* Object handed to the driver core / a list: ownership leaves the function. */
extern void dev_set_drvdata(struct device *dev, void *data);
extern void list_add(void *new_node, void *head);

struct priv {
    int value;
};

struct list_head {
    void *next;
    void *prev;
};

struct node {
    struct list_head link;
    int value;
};

int drvdata_owned(struct device *dev)
{
    struct priv *priv = kzalloc(sizeof(*priv));

    if (!priv)
        return -1;
    priv->value = 1;
    dev_set_drvdata(dev, priv);
    return 0;
}

int list_owned(struct list_head *head)
{
    struct node *node = kmalloc(sizeof(*node));

    if (!node)
        return -1;
    list_add(&node->link, head);
    return 0;
}
