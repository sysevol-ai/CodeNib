# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Callable names and ranges must survive C/C++ declarator wrappers."""

import pytest

from codenib.code_chunking.cpp_chunker import CppCodeChunker


@pytest.mark.parametrize(
    "signature,name,body",
    [
        ("explicit operator bool() const", "operator bool", "return true;"),
        ("operator const char *() const", "operator const char *", 'return "ok";'),
        ("Value& operator+=(const Value& argument)", "operator+=", "return *this;"),
        ("int operator()(int argument) const", "operator()", "return argument;"),
        ("~Value()", "~Value", "(void)0;"),
    ],
)
def test_member_callable_keeps_full_body(signature, name, body):
    source = f"class Value {{\n  {signature} {{\n    {body}\n  }}\n}};\n"
    chunks = CppCodeChunker().chunk_source(source, file_path="value.hpp")
    methods = [chunk for chunk in chunks if chunk.chunk_type == "method"]
    assert [(chunk.name, chunk.start_line, chunk.end_line) for chunk in methods] == [
        (f"Value.{name}", 1, 3)
    ]
    assert methods[0].content.endswith("\n".join(source.splitlines()[1:4]))


@pytest.mark.parametrize(
    "signature,name,body,suffix",
    [
        ("int *factory(int argument)", "factory", "return 0;", "c"),
        ("int Value::member(int argument)", "Value::member", "return argument;", "cpp"),
        ("Value::operator bool() const", "Value::operator bool", "return true;", "cpp"),
    ],
)
def test_wrapped_and_qualified_functions_use_the_declarator_not_parameters(
    signature, name, body, suffix
):
    source = f"{signature} {{\n  {body}\n}}\n"
    chunks = CppCodeChunker().chunk_source(source, file_path=f"value.{suffix}")
    functions = [chunk for chunk in chunks if chunk.chunk_type == "function"]
    assert [(chunk.name, chunk.start_line, chunk.end_line) for chunk in functions] == [
        (name, 0, 2)
    ]
    assert functions[0].content.endswith(source.rstrip())
