"""Round-trip YAML helpers (comments and ordering preserved)."""

from __future__ import annotations

import io
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .. import quantity
from ..models import split_field

YAML_SUFFIXES = (".yaml", ".yml")


def rt_yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def detect_indent(text: str) -> tuple[int, int]:
    """Return (sequence indent, dash offset) used for '- ' lists so rewrites keep the file's style."""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("- ") and idx > 0:
            prev = lines[idx - 1]
            if prev.rstrip().endswith(":"):
                parent = len(prev) - len(prev.lstrip())
                dash = len(line) - len(stripped)
                offset = dash - parent
                return offset + 2, offset
    return 4, 2


def load_all_rt(path: Path) -> tuple[list, YAML, bool]:
    text = path.read_text(encoding="utf-8")
    y = rt_yaml()
    seq, off = detect_indent(text)
    y.indent(mapping=2, sequence=seq, offset=off)
    docs = list(y.load_all(text))
    explicit_start = text.lstrip().startswith("---")
    return docs, y, explicit_start


def dump_all_rt(path: Path, docs: list, y: YAML, explicit_start: bool) -> None:
    y.explicit_start = explicit_start
    buf = io.StringIO()
    if len(docs) == 1:
        y.dump(docs[0], buf)
    else:
        y.dump_all(docs, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


def load_safe_all(text: str) -> list[dict]:
    y = YAML(typ="safe")
    return [d for d in y.load_all(text) if isinstance(d, dict)]


def set_resource_fields(holder: CommentedMap, key: str, changes: dict[str, float]) -> None:
    """Set holder[key][requests|limits][cpu|memory] for each changed field, creating maps as needed."""
    block = holder.get(key)
    if block is None or not isinstance(block, dict):
        block = CommentedMap()
        holder[key] = block
    for fname, value in changes.items():
        resource, kind = split_field(fname)
        section = "requests" if kind == "request" else "limits"
        sect = block.get(section)
        if sect is None or not isinstance(sect, dict):
            sect = CommentedMap()
            block[section] = sect
        sect[resource] = quantity.format_quantity(resource, value)
