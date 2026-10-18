/* ast_virtual_derived.cpp — Derived/Leaf definitions + virtual call sites (TU 2).
 *
 * Expecations (see tests/test_parse_virtual.py): override metadata links
 * Derived::value -> Base::value and Leaf::value -> Derived::value; virtual calls
 * through Base& produce virtual_candidate edges for the known overrides, while
 * Unrelated::other (no overrides) produces none.
 */
#include "ast_virtual.hpp"

namespace ast_virtual {

Derived::~Derived() = default;

int Derived::value(int scale) const
{
    return scale + 1;
}

int Leaf::value(int scale) const
{
    return scale + 2;
}

}  // namespace ast_virtual

int call_through_base(const ast_virtual::Base &base)
{
    return base.value(2);
}

int call_unrelated(const ast_virtual::Unrelated &unrelated)
{
    return unrelated.other();
}
