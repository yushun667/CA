/* CFG/branch fixture for ca.analyze.
 *
 * Intended coverage: nested if / else-if / else, while + break + continue,
 * do-while, for (with declaration init and increment), switch with adjacent
 * case labels and fallthrough, goto/label, short-circuit && / ||, a ternary
 * used as a condition (boolean context), a negated condition, and an implicit
 * return path in a non-void function.
 */
int classify(int x, int y, int flag)
{
    int score = 0;

    if (x > 0 && y > 0) {
        score = 1;
    } else if (x < 0 || y < 0) {
        score = 2;
    } else {
        score = 3;
    }

    while (score > 0) {
        score--;
        if (score == 2)
            break;
        if (score == 1)
            continue;
        score += 10;
    }

    do {
        score += 2;
    } while (score < 5);

    for (int i = 0; i < 3; i++) {
        if (i == 1)
            continue;
        score += i;
    }

    switch (x) {
    case 0:
        score = 0;
        break;
    case 1:
    case 2:
        score = 7;
        break;
    default:
        score--;
    }

    if (flag ? x : y) {
        score = 42;
    }

    if (!flag) {
        score = 43;
    }

    if (x > 100)
        goto out;

    score = 44;

out:
    return score;
}

/* Non-void function with a fallthrough path: the analyzer must synthesize an
 * implicit return record. */
int implicit_path(int x)
{
    if (x)
        return 1;
}
