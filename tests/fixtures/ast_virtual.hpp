/* ast_virtual.hpp — C++ virtual dispatch fixture (task-37).
 *
 * Single inheritance chain Base <- Derived <- Leaf, overriding methods, one
 * non-virtual method, and an unrelated virtual class (no override) to prove
 * that virtual candidates are not invented without override metadata.
 */
#ifndef CA_AST_VIRTUAL_HPP
#define CA_AST_VIRTUAL_HPP

namespace ast_virtual {

class Base {
public:
    virtual ~Base();
    virtual int value(int scale) const;
    int plain() const;
};

class Derived : public Base {
public:
    ~Derived() override;
    int value(int scale) const override;
};

class Leaf : public Derived {
public:
    int value(int scale) const override;
};

class Unrelated {
public:
    virtual int other() const;
};

}  // namespace ast_virtual

#endif /* CA_AST_VIRTUAL_HPP */
