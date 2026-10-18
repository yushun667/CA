/* ast_cpp_second.cpp — second C++ TU for cross-translation-unit id stability.
 *
 * Includes only the shared header, so class/method declarations are parsed
 * again in another TU: their USR-based ids must match the definitions from
 * ast_cpp_basic.cpp. The local `second_entry` function is a plain free
 * function call target for the second unit.
 */
#include "ast_cpp_basic.hpp"

int second_entry(int x)
{
    return ast_cpp::helper(x) + ast_cpp::Base::live_count();
}
