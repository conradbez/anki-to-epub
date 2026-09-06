"""Protobuf codec round-trip tests."""

from anki.protobuf import (
    pb_field,
    pb_fields,
    pb_int,
    pb_message,
    pb_parse,
    pb_string,
)


def test_string_roundtrip():
    raw = pb_string(1, "hello") + pb_string(2, "world")
    parsed = pb_parse(raw)
    assert pb_field(parsed, 1) == b"hello"
    assert pb_field(parsed, 2) == b"world"


def test_int_roundtrip_varint_boundaries():
    for value in (0, 1, 127, 128, 300, 16384, 2**32, 2**63 - 1):
        raw = pb_int(5, value)
        parsed = pb_parse(raw)
        assert pb_field(parsed, 5) == value


def test_utf8_string():
    raw = pb_string(3, "café ☕ 日本語")
    assert pb_field(pb_parse(raw), 3).decode("utf-8") == "café ☕ 日本語"


def test_nested_message():
    inner = pb_int(1, 42) + pb_string(2, "Spanish")
    raw = pb_message(7, inner)
    outer = pb_parse(raw)
    sub = pb_parse(pb_field(outer, 7))
    assert pb_field(sub, 1) == 42
    assert pb_field(sub, 2) == b"Spanish"


def test_repeated_fields():
    raw = pb_message(2, pb_int(1, 1)) + pb_message(2, pb_int(1, 2))
    parsed = pb_parse(raw)
    subs = [pb_parse(v) for v in pb_fields(parsed, 2)]
    assert [pb_field(s, 1) for s in subs] == [1, 2]


def test_login_body_shape():
    body = pb_string(1, "user") + pb_string(2, "pass")
    parsed = pb_parse(body)
    assert pb_field(parsed, 1) == b"user"
    assert pb_field(parsed, 2) == b"pass"
