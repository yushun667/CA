/* ast_cpp_basic.hpp — shared header fixture for the C++ parse tests (P2-3).
 *
 * Construction intent: exercise the C++ entity surface the analyzer cares
 * about. Expectations (see tests/test_parse_cpp_basic.py):
 *   - namespace ast_cpp: qualified names carry the namespace
 *   - struct Point / enum class Color: C++ record kinds and layouts
 *   - class Base / class Widget: kind=class, virtual + overridden methods,
 *     constructor/destructor, static member functions, inline and const
 *     member functions, overloaded methods, protected/private fields
 *   - template function max_of and class template Box: recorded as declaration
 *     entities (libclang does not expose template instantiations)
 *   - using alias PointAlias maps onto TypeRecord kind=typedef
 *   - member function operator+ for operator-overload call probing
 *
 * Known boundary: base-class relations are not representable in the current
 * ca.core.schema TypeRecord (no bases field), so inheritance is only visible
 * through the derived class record and its member parent ids.
 */
#ifndef CA_AST_CPP_BASIC_HPP
#define CA_AST_CPP_BASIC_HPP

namespace ast_cpp {

struct Point {
    int x = 0;
    int y = 0;
};

enum class Color : unsigned char {
    Red = 1,
    Green = 2,
};

class Base {
public:
    explicit Base(int seed);
    virtual ~Base();
    virtual int value(int scale) const;
    int seed() const { return seed_; }
    static int live_count();

protected:
    int seed_ = 0;
};

class Widget : public Base {
public:
    Widget();
    ~Widget() override;
    int value(int scale) const override;
    int value(int scale, int extra) const;
    void bump() { ++uses_; }
    static int created();

private:
    int uses_ = 0;
};

struct Vec2 {
    int x = 0;
    int y = 0;
    Vec2 operator+(const Vec2 &other) const;
};

template <typename T>
T max_of(T a, T b)
{
    return a > b ? a : b;
}

template <typename T>
class Box {
public:
    T value{};
    T get() const { return value; }
};

using PointAlias = Point;

int helper(int x);

}  // namespace ast_cpp

int cpp_entry(ast_cpp::Widget &widget, const ast_cpp::Base &base);

#endif /* CA_AST_CPP_BASIC_HPP */
