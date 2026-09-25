"""Safe YAML for user-authored documents (scenarios, twins, policies; spec §112).

* Only plain data: no object construction, no custom or unknown tags.
* No anchors or aliases (they enable exponential "billion laughs" expansion).
* Bounded size, nesting depth and node count; one document per input.
* Duplicate mapping keys and non-string keys are errors, not silent overrides.
* YAML 1.2 core-schema scalars: ``true``/``false`` only (``no`` stays the
  string ``"no"``), decimal integers and floats (no ``.inf``/``.nan``, no
  sexagesimal or legacy octal numbers), and no timestamp conversion (dates
  stay strings) — the result is always JSON-compatible.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    CollectionStartEvent,
    DocumentStartEvent,
    NodeEvent,
    ScalarEvent,
)
from yaml.nodes import MappingNode, Node

__all__ = ["YAMLDocumentError", "dump_yaml", "load_yaml"]

DEFAULT_MAX_BYTES = 256 * 1024
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_NODES = 20_000

_STD_TAGS = {
    "tag:yaml.org,2002:str",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:null",
    "tag:yaml.org,2002:map",
    "tag:yaml.org,2002:seq",
}


class YAMLDocumentError(ValueError):
    """The document is not acceptable; ``line``/``column`` are 1-based when known."""

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        where = f" (line {line}, column {column})" if line is not None else ""
        super().__init__(message + where)
        self.message = message
        self.line = line
        self.column = column


def _mark(mark: Any) -> tuple[int | None, int | None]:
    if mark is None:
        return None, None
    return mark.line + 1, mark.column + 1


class _Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 core-schema resolution and strict mappings."""

    def construct_mapping(self, node: Node, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise YAMLDocumentError("expected a mapping", *_mark(node.start_mark))
        self.flatten_mapping(node)
        out: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise YAMLDocumentError(
                    f"mapping keys must be strings, got {type(key).__name__}", *_mark(key_node.start_mark)
                )
            if key in out:
                raise YAMLDocumentError(f"duplicate key {key!r}", *_mark(key_node.start_mark))
            out[key] = self.construct_object(value_node, deep=deep)
        return out


_Loader.yaml_implicit_resolvers = {}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:null", re.compile(r"^(?:~|null|Null|NULL|)$"), ["~", "n", "N", ""]
)
_Loader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^[-+]?[0-9]+$"), list("-+0123456789"))
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$"),
    list("-+0123456789."),
)


def _int(loader: _Loader, node: Node) -> int:
    value = loader.construct_scalar(node)  # type: ignore[arg-type]
    if not re.fullmatch(r"[-+]?[0-9]+", str(value)):
        raise YAMLDocumentError(f"invalid integer {value!r}", *_mark(node.start_mark))
    return int(str(value))


def _float(loader: _Loader, node: Node) -> float:
    value = str(loader.construct_scalar(node))  # type: ignore[arg-type]
    try:
        f = float(value)
    except ValueError:
        raise YAMLDocumentError(f"invalid number {value!r}", *_mark(node.start_mark)) from None
    if f != f or f in (float("inf"), float("-inf")):
        raise YAMLDocumentError("infinite and NaN numbers are not allowed", *_mark(node.start_mark))
    return f


def _bool(loader: _Loader, node: Node) -> bool:
    value = str(loader.construct_scalar(node))  # type: ignore[arg-type]
    if value.lower() not in ("true", "false"):
        raise YAMLDocumentError(f"invalid boolean {value!r}", *_mark(node.start_mark))
    return value.lower() == "true"


_Loader.add_constructor("tag:yaml.org,2002:int", _int)
_Loader.add_constructor("tag:yaml.org,2002:float", _float)
_Loader.add_constructor("tag:yaml.org,2002:bool", _bool)
_Loader.add_constructor("tag:yaml.org,2002:map", _Loader.construct_mapping)


def _check_events(text: str, max_depth: int, max_nodes: int) -> None:
    depth = nodes = documents = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            line, col = _mark(event.start_mark)
            if isinstance(event, DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise YAMLDocumentError("only one YAML document is allowed", line, col)
            if isinstance(event, AliasEvent):
                raise YAMLDocumentError("YAML aliases are not allowed", line, col)
            if isinstance(event, NodeEvent):
                if event.anchor:
                    raise YAMLDocumentError("YAML anchors are not allowed", line, col)
                nodes += 1
                if nodes > max_nodes:
                    raise YAMLDocumentError(f"document has more than {max_nodes} nodes", line, col)
                # The parser expands "!!str" to "tag:yaml.org,2002:str"; "!" is
                # the non-specific tag. Anything else is an application tag.
                tag = getattr(event, "tag", None)
                if tag and tag != "!" and tag not in _STD_TAGS:
                    raise YAMLDocumentError(f"tag {tag!r} is not allowed", line, col)
            if isinstance(event, CollectionStartEvent):
                depth += 1
                if depth > max_depth:
                    raise YAMLDocumentError(f"document is nested deeper than {max_depth} levels", line, col)
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            elif isinstance(event, ScalarEvent) and len(event.value) > 1_000_000:
                raise YAMLDocumentError("scalar value is too large", line, col)
    except yaml.YAMLError as err:
        mark = getattr(err, "problem_mark", None)
        raise YAMLDocumentError(
            f"invalid YAML: {getattr(err, 'problem', None) or err}", *_mark(mark)
        ) from None


def load_yaml(
    text: str | bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> Any:
    """Parses one untrusted YAML (or JSON) document into plain data."""
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > max_bytes:
        raise YAMLDocumentError(f"document is larger than {max_bytes} bytes")
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise YAMLDocumentError("document is not valid UTF-8") from None
    _check_events(source, max_depth, max_nodes)
    try:
        return yaml.load(source, Loader=_Loader)  # noqa: S506 - _Loader is a restricted SafeLoader
    except YAMLDocumentError:
        raise
    except yaml.YAMLError as err:
        mark = getattr(err, "problem_mark", None)
        raise YAMLDocumentError(
            f"invalid YAML: {getattr(err, 'problem', None) or err}", *_mark(mark)
        ) from None


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _looks_implicit(value: str) -> bool:
    """True when a plain scalar would not read back as a string, under our
    YAML 1.2 rules or under YAML 1.1 (other tools reading exported files)."""
    for resolvers in (_Loader.yaml_implicit_resolvers, yaml.SafeLoader.yaml_implicit_resolvers):
        for tag, regexp in resolvers.get(value[:1], []):
            if tag != "tag:yaml.org,2002:str" and regexp.match(value):
                return True
    return False


# Characters a YAML reader normalizes (NEL, line/paragraph separators, BOM)
# or that are unsafe outside escapes: only double quotes preserve them.
_NEEDS_ESCAPE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff]")


def _str_representer(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if _NEEDS_ESCAPE.search(value):
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    style = "'" if _looks_implicit(value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str_representer)


def dump_yaml(value: Any) -> str:
    """Serializes plain data to block-style YAML that :func:`load_yaml` reads back."""
    return yaml.dump(
        value, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
    )
