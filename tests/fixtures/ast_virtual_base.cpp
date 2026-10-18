/* ast_virtual_base.cpp — Base definitions (TU 1 of the virtual fixture). */
#include "ast_virtual.hpp"

namespace ast_virtual {

Base::~Base() = default;

int Base::value(int scale) const
{
    return scale;
}

int Base::plain() const
{
    return 0;
}

int Unrelated::other() const
{
    return 1;
}

}  // namespace ast_virtual
