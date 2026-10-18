"""FTS5 symbol-search tests: consistency, fallback, rebuild, escaping.

The FTS5 trigram index is an optimization, not a new contract: results must
match the old ``LIKE '%pattern%'`` behaviour (same rows, same ordering, same
dict keys) and queries must keep working when FTS5 is unavailable or the
database predates the index.
"""

from __future__ import annotations

import sqlite3

import pytest

from ca.core.ids import call_id, function_id, tu_id
from ca.core.schema import CallRecord, CompilationUnit, FunctionRecord, ParseResult, Sloc
from ca.store import SqliteStore

REPO = "/repo"
PROC_FILE = "kernel/proc.c"

TU = CompilationUnit(
    id=tu_id(PROC_FILE, REPO, ["-c", PROC_FILE]),
    file=PROC_FILE,
    directory=REPO,
    repo_root=REPO,
    arguments=("-c", PROC_FILE),
)

FUNCTION_KEYS = {
    "id",
    "name",
    "qualified_name",
    "kind",
    "file",
    "line",
    "is_definition",
    "is_static",
    "linkage",
}


def _function(
    name: str,
    line: int,
    *,
    file: str = PROC_FILE,
    is_definition: bool = True,
    qualified_name: str | None = None,
    usr: str | None = None,
) -> FunctionRecord:
    sloc = Sloc(file, line, 3, line, 20)
    usr_value = f"c:@F@{name}" if usr is None else usr
    return FunctionRecord(
        id=function_id(name=name, usr=usr_value, sloc=sloc, linkage="external"),
        tu_id=TU.id,
        name=name,
        qualified_name=qualified_name if qualified_name is not None else name,
        usr=usr_value,
        kind="function",
        sloc=sloc,
        is_definition=is_definition,
        linkage="external",
    )


SYS_SLEEP = _function("sys_sleep", 10)
WAKEUP = _function("wakeup", 40)
WAKEUP_HELPER = _function("wakeup_helper", 50)
ALLOC = _function("kmalloc_alloc", 60)
ZONE_ALPHA = _function("zone_alpha", 20, file="kernel/z.c")
ZONE_BETA = _function("zone_beta", 10, file="kernel/a.c", is_definition=False)
ZONE_GAMMA = _function("zone_gamma", 30, file="kernel/b.c")
CJK = _function("分配内存", 70)
QUOTE = _function('weird"name', 80)
PERCENT = _function("100%done", 90)
STAR = _function("x*y", 100)
PAREN = _function("(paren_func", 110)
DASH = _function("dash-name", 120)
BSLASH = _function("back\\slash", 130)
TWO_CHARS = _function("ab", 140)
QUALIFIED = _function("foo", 150, qualified_name="struct foo_bar")
CALLER = _function("main_loop", 200)
CALLEE = _function("get_user_data", 210)

CALL = CallRecord(
    id=call_id(
        caller_id=CALLER.id,
        callee_usr=CALLEE.usr,
        callee_name=CALLEE.name,
        sloc=Sloc(PROC_FILE, 205, 5, 205, 28),
    ),
    tu_id=TU.id,
    caller_id=CALLER.id,
    sloc=Sloc(PROC_FILE, 205, 5, 205, 28),
    callee_id=CALLEE.id,
    callee_usr=CALLEE.usr,
    callee_name=CALLEE.name,
    callee_qualified_name=CALLEE.name,
    is_resolved=True,
)

FUNCTIONS = [
    SYS_SLEEP,
    WAKEUP,
    WAKEUP_HELPER,
    ALLOC,
    ZONE_ALPHA,
    ZONE_BETA,
    ZONE_GAMMA,
    CJK,
    QUOTE,
    PERCENT,
    STAR,
    PAREN,
    DASH,
    BSLASH,
    TWO_CHARS,
    QUALIFIED,
    CALLER,
    CALLEE,
]

PARSE_RESULT = ParseResult(tu=TU, functions=FUNCTIONS, calls=[CALL])


def _like_ids(store: SqliteStore, pattern: str, limit: int = 1000) -> list[str]:
    """Reference result: the pre-FTS LIKE query."""

    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    rows = store.connection.execute(
        "SELECT id FROM functions "
        "WHERE name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\' "
        "ORDER BY is_definition DESC, file, line, id LIMIT ?",
        (like, like, limit),
    ).fetchall()
    return [row[0] for row in rows]


@pytest.fixture()
def populated(tmp_path):
    store = SqliteStore(tmp_path / "fts.db")
    with store:
        store.begin()
        store.add_parse_result(PARSE_RESULT)
        store.commit()
        store.finalize()
        yield store


def test_fts_index_is_built_with_meta_flag(populated) -> None:
    row = populated.connection.execute(
        "SELECT value FROM meta WHERE key = 'fts_ready'"
    ).fetchone()
    assert row is not None and row[0] == "1"
    table = populated.connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'functions_fts'"
    ).fetchone()
    assert table is not None
    assert "fts5" in table[0]
    assert "trigram" in table[0]


def test_substring_results_match_like_reference(populated) -> None:
    patterns = [
        "sleep",
        "wake",
        "_helper",
        "alloc",
        "zone_",
        "分配内",
        'eird"n',
        "100%",
        "x*y",
        "(par",
        "dash-",
        "ack\\s",
        "foo_bar",
        "ab",  # < 3 chars: LIKE fallback path
        "分配",  # 2-char CJK: LIKE fallback path
        "zzz_absent",
    ]
    for pattern in patterns:
        expected = _like_ids(populated, pattern)
        got = [row["id"] for row in populated.query_functions(pattern, limit=1000)]
        assert got == expected, f"mismatch for {pattern!r}"


def test_exact_match_priority_and_keys_unchanged(populated) -> None:
    rows = populated.query_functions("wakeup")
    assert [row["id"] for row in rows] == [WAKEUP.id]
    assert set(rows[0]) == FUNCTION_KEYS

    # Substring search still reaches helpers when nothing matches exactly.
    assert {row["name"] for row in populated.query_functions("wakeup_he")} == {"wakeup_helper"}


def test_limit_and_stable_ordering_unchanged(populated) -> None:
    expected = _like_ids(populated, "zone_")
    got = [row["id"] for row in populated.query_functions("zone_", limit=2)]
    assert got == expected[:2]
    # Definitions sort before declarations; file/line/id break ties.
    assert [row["id"] for row in populated.query_functions("zone_")] == expected
    assert populated.query_functions("zone_", limit=0) == []


def test_special_characters_are_literal_and_safe(populated) -> None:
    cases = {
        'weird"n': [QUOTE.id],
        "100%": [PERCENT.id],
        "x*y": [STAR.id],
        "(par": [PAREN.id],
        "dash-": [DASH.id],
        "ack\\s": [BSLASH.id],
    }
    for pattern, expected_ids in cases.items():
        assert [row["id"] for row in populated.query_functions(pattern)] == expected_ids
    # Malformed FTS-looking input must not raise and must not match absent rows.
    for pattern in ('"', '""', '"""', '*', '()', '-', "'; DROP TABLE functions; --", "NEAR("):
        assert isinstance(populated.query_functions(pattern), list)
    assert populated.stats()["tables"]["functions"] == len(FUNCTIONS)


def test_qualified_name_is_searchable(populated) -> None:
    assert [row["id"] for row in populated.query_functions("foo_bar")] == [QUALIFIED.id]
    assert [row["id"] for row in populated.query_functions("bar")] == [QUALIFIED.id]


def test_short_patterns_use_like_fallback(populated) -> None:
    queries = populated._require_queries()
    assert queries._fts_ready_for("ab") is False
    assert queries._fts_ready_for("abc") is True
    assert [row["id"] for row in populated.query_functions("ab")] == [TWO_CHARS.id]
    assert [row["id"] for row in populated.query_functions("分配")] == [CJK.id]
    assert [row["id"] for row in populated.query_functions("分配内")] == [CJK.id]


def test_fallback_when_fts_flag_is_off(populated) -> None:
    populated.connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('fts_ready', '0')"
    )
    populated.connection.commit()
    populated._require_queries().reset_caches()
    assert [row["id"] for row in populated.query_functions("sleep")] == _like_ids(
        populated, "sleep"
    )
    # Restoring the flag switches back to the FTS path.
    populated.connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('fts_ready', '1')"
    )
    populated.connection.commit()
    populated._require_queries().reset_caches()
    assert [row["id"] for row in populated.query_functions("sleep")] == [SYS_SLEEP.id]


def test_missing_fts_table_falls_back_and_finalize_repairs(populated) -> None:
    populated.connection.execute("DROP TABLE functions_fts")
    populated.connection.commit()
    populated._require_queries().reset_caches()
    # Missing table: the FTS query fails inside the engine, then LIKE takes over.
    assert [row["id"] for row in populated.query_functions("sleep")] == [SYS_SLEEP.id]

    # finalize() rebuilds the index for an existing/older database.
    populated.finalize()
    row = populated.connection.execute(
        "SELECT value FROM meta WHERE key = 'fts_ready'"
    ).fetchone()
    assert row is not None and row[0] == "1"
    assert [row["id"] for row in populated.query_functions("sleep")] == [SYS_SLEEP.id]


def test_incremental_add_then_finalize_reindexes(populated, tmp_path) -> None:
    added = _function("new_symbol_xyz", 300, file="kernel/new.c")
    populated.begin()
    populated.add_parse_result(ParseResult(tu=TU, functions=[added]))
    populated.commit()
    populated.finalize()
    assert [row["id"] for row in populated.query_functions("symbol_xy")] == [added.id]
    assert [row["id"] for row in populated.query_functions("sleep")] == [SYS_SLEEP.id]
    assert populated.query_functions("new_symbol_xyz")[0]["is_definition"] is True


def test_reopen_existing_database_and_lazy_rebuild(populated) -> None:
    populated.connection.execute("DROP TABLE functions_fts")
    populated.connection.execute("DELETE FROM meta WHERE key = 'fts_ready'")
    populated.connection.commit()
    populated.close()

    reopened = SqliteStore(str(populated.db_path))
    with reopened:
        # No FTS yet (old database): substring still correct through LIKE.
        assert [row["id"] for row in reopened.query_functions("sleep")] == [SYS_SLEEP.id]
        reopened.finalize()
        assert reopened.connection.execute(
            "SELECT value FROM meta WHERE key = 'fts_ready'"
        ).fetchone()[0] == "1"
        assert [row["id"] for row in reopened.query_functions("sleep")] == [SYS_SLEEP.id]


def test_callers_resolution_uses_substring_search(populated) -> None:
    rows = populated.query_callers("user_dat")
    assert len(rows) == 1
    assert rows[0]["caller_name"] == "main_loop"
    assert rows[0]["callee_name"] == "get_user_data"
    assert set(rows[0]) >= {
        "caller_id",
        "caller_name",
        "caller_file",
        "caller_line",
        "callee_name",
        "call_file",
        "call_line",
        "is_resolved",
    }
    callees = populated.query_callees("main_lo")
    assert [row["callee_name"] for row in callees] == ["get_user_data"]
