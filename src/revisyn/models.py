from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias, cast

from .errors import InvalidContent

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


def text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string.")
    return value


def encode_content(value: JSONValue) -> str:
    pending: list[object] = [value]
    visited: set[int] = set()
    while pending:
        item = pending.pop()
        if item is None or type(item) in (str, bool, int):
            continue
        if type(item) is float and math.isfinite(item):
            continue
        if type(item) not in (list, dict):
            raise InvalidContent("Content must contain only finite, exact JSON values.")
        if id(item) in visited:
            continue
        visited.add(id(item))
        if isinstance(item, dict):
            if not all(type(key) is str for key in item):
                raise InvalidContent("Content object keys must be strings.")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    try:
        return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise InvalidContent(f"Cannot encode memory content: {exc}") from exc


class Kind(StrEnum):
    FACT = "fact"
    DERIVED = "derived"


class State(StrEnum):
    SUPPORTED = "supported"
    INVALIDATED = "invalidated"
    RETRACTED = "retracted"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True, order=True)
class MemoryRef:
    namespace: str
    id: str

    def __post_init__(self) -> None:
        text(self.namespace, "namespace")
        text(self.id, "id")


@dataclass(frozen=True, slots=True)
class Memory:
    sequence: int
    ref: MemoryRef
    key: str
    kind: Kind
    state: State
    content_json: str
    source: str
    supports: tuple[tuple[MemoryRef, ...], ...]
    created_at: str
    superseded_by: MemoryRef | None

    @property
    def content(self) -> JSONValue:
        return cast(JSONValue, json.loads(self.content_json))


@dataclass(frozen=True, slots=True)
class Change:
    memory: Memory
    invalidated: tuple[MemoryRef, ...]
    rechecked: tuple[MemoryRef, ...]
    preserved: tuple[MemoryRef, ...]


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    sequence: int
    ref: MemoryRef
    state: State
    reason: str
    caused_by: MemoryRef | None
    replacement: MemoryRef | None
    recorded_at: str


@dataclass(frozen=True, slots=True)
class Explanation:
    target: Memory
    memories: tuple[Memory, ...]
    events: tuple[MemoryEvent, ...]

    @property
    def satisfied_supports(self) -> tuple[tuple[MemoryRef, ...], ...]:
        states = {memory.ref: memory.state for memory in self.memories}
        return tuple(
            clause
            for clause in self.target.supports
            if all(states[parent] is State.SUPPORTED for parent in clause)
        )
