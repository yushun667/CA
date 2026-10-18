/* ast_virtual_this.cpp - implicit-this vs explicit-receiver virtual calls.
 *
 * Base::CallImplicit() calls Open() on this. Only Base::Open and overrides of
 * classes derived from Base are possible; Sibling::Open must not appear.
 * CallExplicit() calls through an IService pointer and may reach all overrides.
 */
namespace vthis {

class IService {
public:
    virtual int Open(int value) = 0;
    virtual ~IService() = default;
};

class Base : public IService {
public:
    int Open(int value) override { return value; }
    int CallImplicit() { return Open(1); }
};

class Derived : public Base {
public:
    int Open(int value) override { return value + 1; }
};

class Sibling : public IService {
public:
    int Open(int value) override { return value + 2; }
};

int CallExplicit(IService *service) { return service->Open(3); }

}  // namespace vthis
