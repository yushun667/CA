/* ast_fnptr.cpp — C++ function-pointer/using style fixture (P3-5).
 *
 * OpenHarmony C++ code commonly writes callback types as `using X = ...;` and
 * keeps them in namespaces. This TU pins down alias underlying spellings, method
 * parameters of callback type, function-pointer fields, and an indirect call.
 */
namespace ast_cpp_fnptr {

using callback_t = int (*)(int);
using notify_t = void (*)(void *ctx, int code);

struct Ops {
    callback_t handler;
    int (*raw_fn)(int, int);
    notify_t notify;
};

int add(int a, int b)
{
    return a + b;
}

class Dispatcher {
public:
    explicit Dispatcher(callback_t cb);
    int dispatch(int value) const;
    static int apply(int (*fn)(int), int value);

private:
    callback_t cb_;
};

Dispatcher::Dispatcher(callback_t cb)
    : cb_(cb)
{
}

int Dispatcher::dispatch(int value) const
{
    return cb_(value);
}

int Dispatcher::apply(int (*fn)(int), int value)
{
    return fn(value);
}

int use_ops(const Ops &ops, int value)
{
    return ops.handler(value) + ops.raw_fn(value, value);
}

}  // namespace ast_cpp_fnptr
