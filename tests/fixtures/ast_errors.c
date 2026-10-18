/* ast_errors.c — fixture for degraded parsing: the translation unit contains a
 * compile error (unknown type) and, when -Werror is present, a warning is
 * escalated. The parser must keep whatever records it can and report
 * parse_ok=False with an error string instead of raising.
 */
struct ok_type {
    int field;
};

int good_function(int x)
{
    return x + 1;
}

int broken_function(void)
{
    missing_type_t value;
    return 0;
}
