"""Small, non-executing numeric NPY validator; no pickle, archives or NumPy import.

Only primitive numeric arrays in NPY 1.0/2.0/3.0 are supported. The caller bounds
the whole file before this module inspects its header and exact payload length.
Format: https://numpy.org/doc/stable/reference/generated/numpy.lib.format.html
"""
from __future__ import annotations

import ast
import math
import re
import struct
import sys

_FORMATS = {"b1": "?", "i1": "b", "u1": "B", "i2": "h", "u2": "H",
            "i4": "i", "u4": "I", "i8": "q", "u8": "Q", "f4": "f", "f8": "d"}


def numeric_array_layout(header_data: bytes, file_size: int) -> tuple[tuple[int, ...], int, str]:
    """Parse only a bounded header and verify the advertised exact file length.

    Shared by small and streamed readers. Never unpickle or import NumPy.
    """
    try:
        if (header_data[:6] != b"\x93NUMPY"
                or header_data[6:8] not in (b"\x01\x00", b"\x02\x00", b"\x03\x00")):
            raise ValueError("unsupported array format")
        offset = 10 if header_data[6] == 1 else 12
        size = int.from_bytes(header_data[8:offset], "little")
        if not 1 <= size <= 4096 or len(header_data) < offset + size:
            raise ValueError("invalid array header size")
        tree = ast.parse(header_data[offset:offset + size].decode(
            "utf-8" if header_data[6] == 3 else "latin1").strip(), mode="eval").body
        if not isinstance(tree, ast.Dict) or len(tree.keys) != 3:
            raise ValueError("invalid array header")
        fields = ast.literal_eval(tree)
        if set(fields) != {"descr", "shape", "fortran_order"}:
            raise ValueError("invalid array header fields")
        shape, descriptor = fields["shape"], fields["descr"]
        if (not isinstance(shape, tuple) or len(shape) > 8
                or any(type(n) is not int or not 0 <= n <= 1_000_000_000 for n in shape)
                or type(fields["fortran_order"]) is not bool or not isinstance(descriptor, str)
                or re.fullmatch(r"[<>=|](?:[iu][1248]|f[48]|b1)", descriptor) is None):
            raise ValueError("unsupported array dtype or shape")
        endian, dtype = descriptor[0], descriptor[1:]
        item_size = int(dtype[1:])
        if endian == "|" and item_size != 1:
            raise ValueError("invalid array byte order")
        if file_size - offset - size != math.prod(shape) * item_size:
            raise ValueError("array payload length mismatch")
        order = endian if endian in "<>" else ("<" if sys.byteorder == "little" else ">")
        return tuple(shape), offset + size, order + _FORMATS[dtype]
    except (TypeError, SyntaxError, UnicodeError, RecursionError, struct.error) as error:
        raise ValueError("invalid numeric array") from error


def numeric_array_shape(
    data: bytes, *, finite: bool = True, nonconstant: bool = False,
    minimum: float | None = None, maximum: float | None = None,
) -> tuple[int, ...]:
    """Validate a complete bounded primitive array and return its actual shape."""
    try:
        shape, offset, fmt = numeric_array_layout(data, len(data))
        payload = data[offset:]
        constrained = nonconstant or minimum is not None or maximum is not None
        if constrained or (finite and fmt[-1] in "fd"):
            values = struct.iter_unpack(fmt, payload)
            first: int | float | None = None
            varied = False
            for (value,) in values:
                if not math.isfinite(value):
                    raise ValueError("nonfinite array values")
                if ((minimum is not None and value < minimum)
                        or (maximum is not None and value > maximum)):
                    raise ValueError("array values outside declared range")
                if first is None:
                    first = value
                elif value != first:
                    varied = True
            if nonconstant and not varied:
                raise ValueError("empty or constant numeric array")
        return tuple(shape)
    except (TypeError, SyntaxError, UnicodeError, RecursionError, struct.error) as error:
        raise ValueError("invalid numeric array") from error


def numeric_threshold_count(data: bytes, threshold: float, *, inclusive: bool = False
                            ) -> tuple[int, int]:
    """Count actual scalar observations, with strict comparison by default.

    Reuses all primitive/size/finite validation; never imports NumPy or loads
    pickles. One-dimensional nonempty scores only; no flattening or rounding.
    The caller bounds the file just as for numeric_array_shape.
    """
    if not math.isfinite(threshold):
        raise ValueError("invalid threshold")
    shape = numeric_array_shape(data)
    if len(shape) != 1 or not shape[0]:
        raise ValueError("threshold scores must be a nonempty vector")
    offset = 10 if data[6] == 1 else 12
    header_size = int.from_bytes(data[8:offset], "little")
    # The complete header and payload have already been validated above.
    fields = ast.literal_eval(data[offset:offset + header_size].decode(
        "utf-8" if data[6] == 3 else "latin1").strip())
    endian, dtype = fields["descr"][0], fields["descr"][1:]
    order = endian if endian in "<>" else ("<" if sys.byteorder == "little" else ">")
    count = sum(value >= threshold if inclusive else value > threshold
                for (value,) in struct.iter_unpack(order + _FORMATS[dtype],
                                                   data[offset + header_size:]))
    return count, shape[0]
