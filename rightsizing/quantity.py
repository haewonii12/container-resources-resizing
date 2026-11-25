"""Kubernetes resource quantity parsing, formatting and rounding.

CPU values are handled in millicores, memory values in bytes.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

MI = 1024 ** 2
GI = 1024 ** 3

_BINARY = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6}
_DECIMAL = {"n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"), "": Decimal(1),
            "k": Decimal("1e3"), "M": Decimal("1e6"), "G": Decimal("1e9"), "T": Decimal("1e12"),
            "P": Decimal("1e15"), "E": Decimal("1e18")}
_QTY = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$")


class QuantityError(ValueError):
    pass


def parse_number(value: str | int | float) -> Decimal:
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    match = _QTY.match(text)
    if not match:
        raise QuantityError(f"invalid quantity: {value!r}")
    number, suffix = match.group(1), match.group(2) or ""
    try:
        base = Decimal(number)
    except InvalidOperation as exc:
        raise QuantityError(f"invalid quantity: {value!r}") from exc
    if suffix in _BINARY:
        return base * _BINARY[suffix]
    return base * _DECIMAL[suffix]


def parse_cpu(value: str | int | float) -> float:
    """Return millicores. '1' -> 1000, '500m' -> 500, 0.5 -> 500."""
    return float(parse_number(value) * 1000)


def parse_mem(value: str | int | float) -> float:
    """Return bytes. '1Gi' -> 1073741824, '500M' -> 5e8."""
    return float(parse_number(value))


def parse(resource: str, value: str | int | float) -> float:
    if resource == "cpu":
        return parse_cpu(value)
    if resource == "memory":
        return parse_mem(value)
    raise QuantityError(f"unsupported resource {resource!r}")


def round_cpu_up(millicores: float, step: int = 10) -> int:
    return int(math.ceil(millicores / step - 1e-9) * step)


def round_mem_up(num_bytes: float, step: int = 16 * MI) -> int:
    return int(math.ceil(num_bytes / step - 1e-9) * step)


def round_up(resource: str, value: float) -> int:
    return round_cpu_up(value) if resource == "cpu" else round_mem_up(value)


def format_cpu(millicores: float) -> str:
    m = int(round(millicores))
    if m % 1000 == 0:
        return str(m // 1000)
    return f"{m}m"


def format_mem(num_bytes: float) -> str:
    b = int(round(num_bytes))
    if b % GI == 0:
        return f"{b // GI}Gi"
    if b % MI == 0:
        return f"{b // MI}Mi"
    if b % 1024 == 0:
        return f"{b // 1024}Ki"
    return str(b)


def format_quantity(resource: str, value: float) -> str:
    return format_cpu(value) if resource == "cpu" else format_mem(value)


def human(resource: str, value: float | None) -> str:
    """Short human-readable form used in commit messages (e.g. 1.08 cores, 3.9Gi)."""
    if value is None:
        return "unset"
    if resource == "cpu":
        if value < 1000:
            return f"{value:.0f}m"  # 0.002 cores -> "2m" (2 decimals of cores would print "0")
        return f"{value / 1000:.2f}".rstrip("0").rstrip(".")
    if value >= GI:
        return f"{value / GI:.1f}Gi"
    return f"{value / MI:.0f}Mi"
