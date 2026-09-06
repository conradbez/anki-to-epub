"""Minimal hand-rolled protobuf codec.

AnkiWeb's ``/svc/`` backend speaks protobuf over HTTP
(``Content-Type: application/octet-stream``). We only ever need to encode a
handful of small request messages and walk simple responses, so instead of
pulling in the ``protobuf`` dependency we implement just the two wire types we
care about:

* wire type 0 -- varint (used for ints / bools / enums)
* wire type 2 -- length-delimited (used for strings and nested messages)

Wire types 1 (64-bit) and 5 (32-bit) are decoded defensively so an unexpected
field in a response cannot desync the parser, but we never emit them.
"""

from __future__ import annotations

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("cannot encode negative varint")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _tag(field_no: int, wire: int) -> bytes:
    if field_no < 1:
        raise ValueError("field numbers start at 1")
    return _encode_varint((field_no << 3) | wire)


def pb_int(field_no: int, value: int) -> bytes:
    """Encode an integer field (varint, wire type 0)."""
    return _tag(field_no, WIRE_VARINT) + _encode_varint(value)


def pb_string(field_no: int, value: str) -> bytes:
    """Encode a UTF-8 string field (length-delimited, wire type 2)."""
    raw = value.encode("utf-8")
    return _tag(field_no, WIRE_LEN) + _encode_varint(len(raw)) + raw


def pb_message(field_no: int, value: bytes) -> bytes:
    """Encode an already-serialized nested message (length-delimited)."""
    return _tag(field_no, WIRE_LEN) + _encode_varint(len(value)) + bytes(value)


def pb_parse(data: bytes) -> list[tuple[int, int, object]]:
    """Parse a protobuf message into ``(field_no, wire_type, value)`` triples.

    * varint fields yield an ``int``
    * length-delimited fields yield ``bytes``
    * 64/32-bit fields yield their raw ``bytes`` (skipped gracefully)

    Fields are returned in wire order; repeated fields appear multiple times.
    """
    out: list[tuple[int, int, object]] = []
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _decode_varint(data, pos)
        field_no = tag >> 3
        wire = tag & 0x07
        if field_no == 0:
            raise ValueError("invalid field number 0")
        if wire == WIRE_VARINT:
            value, pos = _decode_varint(data, pos)
        elif wire == WIRE_LEN:
            length, pos = _decode_varint(data, pos)
            if pos + length > n:
                raise ValueError("truncated length-delimited field")
            value = data[pos : pos + length]
            pos += length
        elif wire == WIRE_64BIT:
            value = data[pos : pos + 8]
            pos += 8
        elif wire == WIRE_32BIT:
            value = data[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((field_no, wire, value))
    return out


def pb_field(parsed: list[tuple[int, int, object]], field_no: int):
    """Return the first value for ``field_no`` from a ``pb_parse`` result."""
    for fn, _wire, value in parsed:
        if fn == field_no:
            return value
    return None


def pb_fields(parsed: list[tuple[int, int, object]], field_no: int) -> list:
    """Return all values for a (possibly repeated) ``field_no``."""
    return [value for fn, _wire, value in parsed if fn == field_no]
