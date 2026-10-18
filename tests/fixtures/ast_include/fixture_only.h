/* ast_include/fixture_only.h — header found through "-I." + working directory. */
#ifndef FIXTURE_ONLY_H
#define FIXTURE_ONLY_H

static inline int fixture_only(int x)
{
    return x * 3;
}

#endif /* FIXTURE_ONLY_H */
