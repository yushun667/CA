/* ast_cpp_basic.cpp — main C++ fixture TU for the C++ parse tests (P2-3).
 *
 * Expectations (see tests/test_parse_cpp_basic.py):
 *   - out-of-line member definitions keep the class as parent_id and the
 *     namespace-qualified name; constructors/destructors have kind
 *     constructor/destructor and an empty return_type
 *   - overloaded methods get distinct USR-based ids
 *   - calls in cpp_entry cover: namespace free function, virtual dispatch
 *     through a derived and a base reference, overload selection, static
 *     member functions, operator overload, anonymous-namespace function
 *   - template calls (max_of, Box<int>::get) stay unresolved: libclang does not
 *     instantiate templates, which is a documented precision limit
 */
#include "ast_cpp_basic.hpp"

namespace ast_cpp {

Base::Base(int seed)
    : seed_(seed)
{
}

Base::~Base() = default;

int Base::value(int scale) const
{
    return seed_ * scale;
}

int Base::live_count()
{
    return 3;
}

Widget::Widget()
    : Base(0)
{
}

Widget::~Widget() = default;

int Widget::value(int scale) const
{
    return uses_ + scale;
}

int Widget::value(int scale, int extra) const
{
    return value(scale) + extra;
}

int Widget::created()
{
    return 4;
}

Vec2 Vec2::operator+(const Vec2 &other) const
{
    return Vec2{x + other.x, y + other.y};
}

int helper(int x)
{
    return max_of(x, x + 1);
}

}  // namespace ast_cpp

namespace {
int local_helper(int x)
{
    return x - 1;
}
}  // namespace

int cpp_entry(ast_cpp::Widget &widget, const ast_cpp::Base &base)
{
    int total = ast_cpp::helper(1);
    total += widget.value(2);
    total += base.value(3);
    total += widget.value(4, 5);
    total += ast_cpp::Base::live_count();
    total += ast_cpp::Widget::created();
    widget.bump();

    ast_cpp::Vec2 first{1, 2};
    ast_cpp::Vec2 second{3, 4};
    ast_cpp::Vec2 sum = first + second;
    total += sum.x;

    total += ast_cpp::max_of(2, 3);
    total += local_helper(1);

    ast_cpp::Box<int> box{7};
    total += box.get();

    ast_cpp::PointAlias alias = ast_cpp::Point{1, 2};
    total += alias.x;
    return total;
}
