"""Function-pointer type/signature extraction tests (task P3-5).

OpenHarmony code bases carry most indirection through function-pointer typedefs
and callback structs, so the analyzer must be able to match a call site back to
the pointer's declared type. This suite pins down what the parser records:

- typedefs of function pointers keep their underlying spelling
  (``int (*)(int)``) and pointer layout;
- parameters and fields keep the *declared* spelling (typedef name when used,
  raw ``int (*)(int)`` otherwise) so analyzers can type-match either style;
- function signatures embed the same spellings;
- calls through function pointers are ``indirect`` with the variable/field name,
  empty ``callee_id`` and ``is_resolved=False``.

Known schema boundary (reported, not changed here): ``CallRecord`` has no
function-pointer type field, so the analyzer joins the callee variable name to
the enclosing :class:`ParamRecord`/:class:`FieldRecord` spelling.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ca.core import ids
from ca.core.schema import CompilationUnit
from ca.parse import ParsedTranslationUnit, parse_compilation_unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_unit(filename: str, arguments: tuple[str, ...]) -> CompilationUnit:
    if os.environ.get("CA_LIBCLANG_LIBRARY"):
        # OH's matched-version libclang has narrow host support; the OHOS target
        # parses these header-free fixtures on both backends.
        arguments = tuple(arguments) + ("--target=aarch64-linux-ohos",)
    file_path = (FIXTURES / filename).resolve()
    return CompilationUnit(
        id=ids.tu_id(str(file_path), str(FIXTURES), arguments),
        file=str(file_path),
        directory=str(FIXTURES),
        repo_root=str(FIXTURES),
        arguments=arguments,
        language="c" if filename.endswith(".c") else "c++",
        source="test",
    )


@pytest.fixture(scope="module")
def c_fnptr() -> ParsedTranslationUnit:
    return parse_compilation_unit(make_unit("ast_fnptr.c", ("-std=gnu99",)))


@pytest.fixture(scope="module")
def cpp_fnptr() -> ParsedTranslationUnit:
    return parse_compilation_unit(make_unit("ast_fnptr.cpp", ("-std=c++17",)))


def functions_by_name(parsed: ParsedTranslationUnit) -> dict[str, object]:
    return {function.name: function for function in parsed.functions}


def types_by_name(parsed: ParsedTranslationUnit) -> dict[str, object]:
    return {(record.qualified_name or record.name): record for record in parsed.types}


# --------------------------------------------------------------------------- #
# C
# --------------------------------------------------------------------------- #


def test_function_pointer_typedefs(c_fnptr: ParsedTranslationUnit) -> None:
    assert c_fnptr.parse_ok is True, c_fnptr.error
    types = types_by_name(c_fnptr)
    callback = types["ast_callback_t"]
    assert callback.kind == "typedef"
    assert callback.underlying_type == "int (*)(int)"
    assert callback.size_bytes == 8 and callback.align_bytes == 8
    assert callback.sloc.file == "ast_fnptr.c"
    assert callback.id == ids.type_id(
        name=callback.name, usr=callback.usr, sloc=callback.sloc, kind=callback.kind
    )

    notify = types["ast_notify_t"]
    assert notify.kind == "typedef"
    assert notify.underlying_type == "void (*)(void *, int)"


def test_function_pointer_params_keep_spelling(c_fnptr: ParsedTranslationUnit) -> None:
    functions = functions_by_name(c_fnptr)
    typedef_param = functions["ast_apply_typedef"]
    assert typedef_param.params[0].name == "cb"
    assert typedef_param.params[0].type_spelling == "ast_callback_t"
    assert typedef_param.signature == "int ast_apply_typedef(ast_callback_t, int)"

    inline_param = functions["ast_apply_inline"]
    assert inline_param.params[0].name == "fn"
    assert inline_param.params[0].type_spelling == "int (*)(int)"
    assert inline_param.signature == "int ast_apply_inline(int (*)(int), int)"

    two = functions["ast_apply_two"]
    assert [param.type_spelling for param in two.params] == [
        "int (*)(int)",
        "ast_callback_t",
        "int",
    ]
    void_return = functions["ast_dispatch"]
    assert void_return.params[0].type_spelling == "ast_notify_t"
    assert void_return.params[1].type_spelling == "void *"


def test_function_pointer_fields(c_fnptr: ParsedTranslationUnit) -> None:
    ops = types_by_name(c_fnptr)["ast_ops"]
    assert ops.kind == "struct"
    assert ops.size_bytes == 24  # three 8-byte pointers on the test hosts
    fields = {field.name: field for field in c_fnptr.fields}
    assert set(fields) == {"handler", "raw_fn", "notify"}
    assert fields["handler"].type_spelling == "ast_callback_t"
    assert fields["raw_fn"].type_spelling == "int (*)(int, int)"
    assert fields["notify"].type_spelling == "ast_notify_t"
    assert [fields[name].offset_bits for name in ("handler", "raw_fn", "notify")] == [0, 64, 128]
    for field in c_fnptr.fields:
        assert field.type_id == ops.id
        assert field.id == ids.field_id(
            type_id_value=field.type_id, name=field.name, sloc=field.sloc, index=field.index
        )


def test_indirect_calls_through_function_pointers(c_fnptr: ParsedTranslationUnit) -> None:
    functions = functions_by_name(c_fnptr)
    by_caller: dict[str, list] = {}
    for call in c_fnptr.calls:
        by_caller.setdefault(call.caller_id, []).append(call)

    typedef_calls = by_caller[functions["ast_apply_typedef"].id]
    assert len(typedef_calls) == 1
    call = typedef_calls[0]
    assert call.callee_name == "cb"
    assert call.kind == "indirect"
    assert call.is_resolved is False
    assert call.callee_id == "" and call.callee_usr == ""
    assert call.arg_count == 1

    inline_calls = by_caller[functions["ast_apply_inline"].id]
    assert inline_calls[0].callee_name == "fn"
    assert inline_calls[0].kind == "indirect"

    two_calls = by_caller[functions["ast_apply_two"].id]
    assert {call.callee_name for call in two_calls} == {"fn", "cb"}
    assert all(call.kind == "indirect" and not call.is_resolved for call in two_calls)

    ops_calls = by_caller[functions["ast_use_ops"].id]
    assert {call.callee_name for call in ops_calls} == {"raw_fn", "handler"}
    assert all(call.kind == "indirect" for call in ops_calls)

    dispatch_calls = by_caller[functions["ast_dispatch"].id]
    assert dispatch_calls[0].callee_name == "notify"
    assert dispatch_calls[0].kind == "indirect"


def test_every_c_fnptr_call_id_and_caller(c_fnptr: ParsedTranslationUnit) -> None:
    function_ids = {function.id for function in c_fnptr.functions}
    for call in c_fnptr.calls:
        assert call.caller_id in function_ids
        assert call.id == ids.call_id(
            caller_id=call.caller_id,
            callee_usr=call.callee_usr,
            callee_name=call.callee_name,
            sloc=call.sloc,
        )


# --------------------------------------------------------------------------- #
# C++ (`using` style, common in OpenHarmony)
# --------------------------------------------------------------------------- #


def test_cpp_using_function_pointer_aliases(cpp_fnptr: ParsedTranslationUnit) -> None:
    assert cpp_fnptr.parse_ok is True, cpp_fnptr.error
    types = types_by_name(cpp_fnptr)
    callback = types["ast_cpp_fnptr::callback_t"]
    assert callback.kind == "typedef"
    assert callback.underlying_type == "int (*)(int)"
    assert callback.size_bytes == 8
    notify = types["ast_cpp_fnptr::notify_t"]
    assert notify.underlying_type == "void (*)(void *, int)"


def test_cpp_function_pointer_members_and_fields(
    cpp_fnptr: ParsedTranslationUnit,
) -> None:
    functions = functions_by_name(cpp_fnptr)
    types = types_by_name(cpp_fnptr)
    dispatcher = types["ast_cpp_fnptr::Dispatcher"]

    constructor = functions["Dispatcher"]
    assert constructor.kind == "constructor"
    assert constructor.parent_id == dispatcher.id
    # libclang 18 prints the typedef name unqualified, libclang 15 qualifies it
    # with the namespace; both spellings are valid type matches for analyzers.
    assert constructor.params[0].type_spelling in {
        "callback_t",
        "ast_cpp_fnptr::callback_t",
    }

    apply = functions["apply"]
    assert apply.parent_id == dispatcher.id
    assert apply.params[0].type_spelling == "int (*)(int)"
    assert apply.signature == "int apply(int (*)(int), int)"

    dispatch = functions["dispatch"]
    assert dispatch.signature == "int dispatch(int) const"
    assert dispatch.parent_id == dispatcher.id

    fields = {field.name: field for field in cpp_fnptr.fields}
    assert fields["handler"].type_spelling in {"callback_t", "ast_cpp_fnptr::callback_t"}
    assert fields["raw_fn"].type_spelling == "int (*)(int, int)"
    assert fields["notify"].type_spelling in {"notify_t", "ast_cpp_fnptr::notify_t"}
    assert fields["cb_"].type_spelling in {"callback_t", "ast_cpp_fnptr::callback_t"}
    assert fields["cb_"].access == "private"


def test_cpp_indirect_calls_through_aliases(cpp_fnptr: ParsedTranslationUnit) -> None:
    functions = functions_by_name(cpp_fnptr)
    by_caller: dict[str, list] = {}
    for call in cpp_fnptr.calls:
        by_caller.setdefault(call.caller_id, []).append(call)

    dispatch_calls = by_caller[functions["dispatch"].id]
    assert [call.callee_name for call in dispatch_calls] == ["cb_"]
    assert dispatch_calls[0].kind == "indirect"
    assert dispatch_calls[0].is_resolved is False

    apply_calls = by_caller[functions["apply"].id]
    assert apply_calls[0].callee_name == "fn"
    assert apply_calls[0].kind == "indirect"

    ops_calls = by_caller[functions["use_ops"].id]
    assert {call.callee_name for call in ops_calls} == {"handler", "raw_fn"}
