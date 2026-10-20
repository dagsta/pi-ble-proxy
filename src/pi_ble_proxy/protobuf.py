"""Minimaler Protobuf-Codec (proto3) für die ESPHome Native API.

Nachrichten sind Dataclasses, deren Felder über ``field()`` Nummer und Typ
bekommen. Unterstützt werden genau die Typen, die die API braucht.
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Any, ClassVar

VARINT, I64, LEN, I32 = 0, 1, 2, 5

_FIXED32_KINDS = {"fixed32", "float"}
_VARINT_KINDS = {"uint32", "uint64", "int32", "int64", "sint32", "bool", "enum"}
_MASK64 = (1 << 64) - 1


class DecodeError(ValueError):
    pass


def field(number: int, kind: Any, *, repeated: bool = False, default: Any = None):
    """Deklariert ein Protobuf-Feld. ``kind`` ist ein Typname oder eine Message-Klasse."""
    if default is None:
        default = _default_for(kind)
    meta = {"pb": (number, kind, repeated)}
    if repeated:
        return dataclasses.field(default_factory=list, metadata=meta)
    return dataclasses.field(default=default, metadata=meta)


def _default_for(kind: Any) -> Any:
    if kind == "string":
        return ""
    if kind == "bytes":
        return b""
    if kind == "bool":
        return False
    if kind == "float":
        return 0.0
    if isinstance(kind, str):
        return 0
    return None  # eingebettete Message


def encode_varint(value: int) -> bytes:
    value &= _MASK64  # negative Zahlen als 64-Bit-Zweierkomplement (10 Bytes)
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise DecodeError("Varint abgeschnitten")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise DecodeError("Varint zu lang")


def _zigzag(n: int) -> int:
    return ((n << 1) ^ (n >> 31)) & 0xFFFFFFFF


def _unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def _encode_scalar(kind: str, value: Any) -> bytes:
    if kind == "bool":
        return b"\x01" if value else b"\x00"
    if kind == "sint32":
        return encode_varint(_zigzag(value))
    if kind == "fixed32":
        return struct.pack("<I", value)
    if kind == "float":
        return struct.pack("<f", value)
    return encode_varint(value)


def _wire_type(kind: Any) -> int:
    if kind in _VARINT_KINDS:
        return VARINT
    if kind in _FIXED32_KINDS:
        return I32
    return LEN


class Message:
    """Basisklasse. Unterklassen setzen ``ID`` (API-Nachrichtentyp, falls vorhanden)."""

    ID: ClassVar[int | None] = None

    def encode(self) -> bytes:
        out = bytearray()
        for f in sorted(dataclasses.fields(self), key=lambda f: f.metadata["pb"][0]):
            number, kind, repeated = f.metadata["pb"]
            value = getattr(self, f.name)
            if repeated:
                if not value:
                    continue
                if kind in _VARINT_KINDS:  # proto3: numerische Listen sind "packed"
                    body = b"".join(_encode_scalar(kind, v) for v in value)
                    out += encode_varint(number << 3 | LEN) + encode_varint(len(body)) + body
                else:
                    for v in value:
                        self._encode_one(out, number, kind, v)
            elif value != f.default and value is not None:
                self._encode_one(out, number, kind, value)
        return bytes(out)

    @staticmethod
    def _encode_one(out: bytearray, number: int, kind: Any, value: Any) -> None:
        wt = _wire_type(kind)
        out += encode_varint(number << 3 | wt)
        if wt == LEN:
            if kind == "string":
                body = value.encode()
            elif kind == "bytes":
                body = bytes(value)
            else:
                body = value.encode()
            out += encode_varint(len(body)) + body
        else:
            out += _encode_scalar(kind, value)

    @classmethod
    def decode(cls, data: bytes | memoryview):
        spec = {}
        for f in dataclasses.fields(cls):
            number, kind, repeated = f.metadata["pb"]
            spec[number] = (f.name, kind, repeated)
        values: dict[str, Any] = {}
        pos, end = 0, len(data)
        while pos < end:
            tag, pos = decode_varint(data, pos)
            number, wt = tag >> 3, tag & 7
            if wt == VARINT:
                raw, pos = decode_varint(data, pos)
            elif wt == I64:
                raw, pos = bytes(data[pos : pos + 8]), pos + 8
            elif wt == I32:
                raw, pos = bytes(data[pos : pos + 4]), pos + 4
            elif wt == LEN:
                size, pos = decode_varint(data, pos)
                if pos + size > end:
                    raise DecodeError("Feld länger als Nachricht")
                raw, pos = bytes(data[pos : pos + size]), pos + size
            else:
                raise DecodeError(f"Unbekannter Wire-Type {wt}")
            if pos > end:
                raise DecodeError("Nachricht abgeschnitten")
            if number not in spec:
                continue  # unbekannte Felder (neuere API-Versionen) überspringen
            name, kind, repeated = spec[number]
            if repeated:
                lst = values.setdefault(name, [])
                if wt == LEN and kind in _VARINT_KINDS:
                    p = 0
                    while p < len(raw):
                        v, p = decode_varint(raw, p)
                        lst.append(_from_varint(kind, v))
                else:
                    lst.append(_from_wire(kind, wt, raw))
            else:
                values[name] = _from_wire(kind, wt, raw)
        return cls(**values)


def _from_varint(kind: str, v: int) -> Any:
    if kind == "bool":
        return bool(v)
    if kind == "sint32":
        return _unzigzag(v)
    if kind == "int32":
        return _signed(v, 32)
    if kind == "int64":
        return _signed(v, 64)
    if kind == "uint32":
        return v & 0xFFFFFFFF
    return v


def _from_wire(kind: Any, wt: int, raw: Any) -> Any:
    if kind in _VARINT_KINDS:
        if wt != VARINT:
            raise DecodeError("Wire-Type passt nicht zum Feldtyp")
        return _from_varint(kind, raw)
    if kind in _FIXED32_KINDS and wt != I32:
        raise DecodeError("Wire-Type passt nicht zum Feldtyp")
    if kind == "fixed32":
        return struct.unpack("<I", raw)[0]
    if kind == "float":
        return struct.unpack("<f", raw)[0]
    if wt != LEN:
        raise DecodeError("Wire-Type passt nicht zum Feldtyp")
    if kind == "string":
        return raw.decode("utf-8", "replace")
    if kind == "bytes":
        return raw
    return kind.decode(raw)
