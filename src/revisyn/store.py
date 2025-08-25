"""Durable justification maintenance for explicitly declared, acyclic memory."""

from __future__ import annotations

import heapq
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from . import _schema
from .errors import (
    InvalidSupport,
    KeyConflict,
    MemoryNotFound,
    NamespaceMismatch,
    RevisionConflict,
    SchemaError,
    StoreClosed,
    UnsupportedMemory,
)
from .models import (
    Change,
    Explanation,
    JSONValue,
    Kind,
    Memory,
    MemoryEvent,
    MemoryRef,
    State,
    encode_content,
    text,
)


class MemoryStore:
    def __init__(self, path: str | Path, *, namespace: str) -> None:
        self._namespace = text(namespace, "namespace")
        self.path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("MemoryStore requires a durable filesystem path.")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with ExitStack() as setup:
            self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
            setup.callback(self._connection.close)
            self._closed = False
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            with self._transaction(write=True):
                self._initialize()
            self._connection.execute("PRAGMA journal_mode = WAL")
            setup.pop_all()

    @property
    def namespace(self) -> str:
        return self._namespace

    def _initialize(self) -> None:
        application = self._connection.execute("PRAGMA application_id").fetchone()[0]
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if application == _schema.APPLICATION_ID and version == _schema.VERSION:
            return
        tables = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if application != 0 or version != 0 or tables:
            raise SchemaError("Not an empty database or a supported Revisyn schema.")
        for statement in _schema.STATEMENTS:
            self._connection.execute(statement)
        self._connection.execute(f"PRAGMA application_id = {_schema.APPLICATION_ID}")
        self._connection.execute(f"PRAGMA user_version = {_schema.VERSION}")

    def _check_open(self) -> None:
        if self._closed:
            raise StoreClosed("The memory store is closed.")

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[None]:
        self._check_open()
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield

    def _ref(self, ref: MemoryRef) -> str:
        if not isinstance(ref, MemoryRef):
            raise TypeError("Use a MemoryRef, not a bare identifier.")
        if ref.namespace != self.namespace:
            raise NamespaceMismatch("The reference belongs to a different namespace.")
        return ref.id

    def _load(self, ref: MemoryRef) -> Memory:
        row = self._connection.execute(
            "SELECT * FROM memories WHERE namespace = ? AND id = ?",
            (self.namespace, self._ref(ref)),
        ).fetchone()
        if row is None:
            raise MemoryNotFound(f"No memory with reference {ref.id!r}.")
        clauses: dict[int, list[MemoryRef]] = {}
        for support in self._connection.execute(
            "SELECT clause, parent_id FROM supports WHERE namespace = ? AND child_id = ? "
            "ORDER BY clause, parent_id",
            (self.namespace, ref.id),
        ):
            clauses.setdefault(support["clause"], []).append(
                MemoryRef(self.namespace, support["parent_id"])
            )
        replacement = row["superseded_by"]
        return Memory(
            row["sequence"],
            ref,
            row["key"],
            Kind(row["kind"]),
            State(row["state"]),
            row["content_json"],
            row["source"],
            tuple(tuple(group) for group in clauses.values()),
            row["created_at"],
            MemoryRef(self.namespace, replacement) if replacement is not None else None,
        )

    def _head(self, key: str) -> MemoryRef | None:
        row = self._connection.execute(
            "SELECT memory_id FROM heads WHERE namespace = ? AND key = ?",
            (self.namespace, key),
        ).fetchone()
        return MemoryRef(self.namespace, row[0]) if row is not None else None

    def get(self, ref: MemoryRef) -> Memory:
        with self._transaction():
            return self._load(ref)

    def recall(self, key: str) -> Memory:
        """Return the current supported revision, or fail explicitly."""
        text(key, "key")
        with self._transaction():
            ref = self._head(key)
            if ref is None:
                raise MemoryNotFound(f"No memory with key {key!r}.")
            memory = self._load(ref)
            if memory.state is not State.SUPPORTED:
                raise UnsupportedMemory(f"Memory {key!r} is {memory.state.value}.")
            return memory

    def memories(self, *, include_history: bool = False) -> tuple[Memory, ...]:
        with self._transaction():
            if include_history:
                rows = self._connection.execute(
                    "SELECT id FROM memories WHERE namespace = ? ORDER BY sequence",
                    (self.namespace,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT m.id FROM memories m JOIN heads h ON "
                    "m.namespace = h.namespace AND m.id = h.memory_id "
                    "WHERE m.namespace = ? ORDER BY m.sequence",
                    (self.namespace,),
                ).fetchall()
            return tuple(self._load(MemoryRef(self.namespace, row[0])) for row in rows)

    def _proofs(self, supports: Sequence[Sequence[MemoryRef]]) -> tuple[tuple[MemoryRef, ...], ...]:
        if not isinstance(supports, (tuple, list)) or not supports:
            raise InvalidSupport("A derivation needs one or more nonempty support sets.")
        result: list[tuple[MemoryRef, ...]] = []
        for group in supports:
            if not isinstance(group, (tuple, list)) or not group:
                raise InvalidSupport("Each support set needs at least one premise.")
            for ref in group:
                self._ref(ref)
            canonical = tuple(sorted(group))
            if len(set(canonical)) != len(canonical) or canonical in result:
                raise InvalidSupport("Premises and alternative support sets must not repeat.")
            result.append(canonical)
        return tuple(result)

    def observe(
        self,
        key: str,
        content: JSONValue,
        *,
        source: str,
        replaces: MemoryRef | None = None,
        reason: str = "New source evidence.",
    ) -> Change:
        return self._write(key, content, Kind.FACT, source, (), replaces=replaces, reason=reason)

    def derive(
        self,
        key: str,
        content: JSONValue,
        *,
        supports: Sequence[Sequence[MemoryRef]],
        rule: str,
        replaces: MemoryRef | None = None,
        reason: str = "Derived using declared support sets.",
    ) -> Change:
        return self._write(
            key,
            content,
            Kind.DERIVED,
            rule,
            self._proofs(supports),
            replaces=replaces,
            reason=reason,
        )

    def _write(
        self,
        key: str,
        content: JSONValue,
        kind: Kind,
        source: str,
        supports: tuple[tuple[MemoryRef, ...], ...],
        *,
        replaces: MemoryRef | None,
        reason: str,
    ) -> Change:
        text(key, "key")
        text(source, "source or rule")
        text(reason, "reason")
        payload = encode_content(content)
        if replaces is not None:
            self._ref(replaces)
        with self._transaction(write=True):
            current = self._head(key)
            if current is not None and replaces is None:
                raise KeyConflict("The key exists; replacement requires its current reference.")
            if replaces != current:
                raise RevisionConflict("The expected revision is not the current key head.")
            previous = self._load(current) if current is not None else None
            if previous is not None and previous.kind is not kind:
                raise RevisionConflict("A key cannot change between fact and derived memory.")
            for group in supports:
                for parent in group:
                    self._load(parent)
            ref = MemoryRef(self.namespace, uuid4().hex)
            now = datetime.now(UTC).isoformat()
            self._connection.execute(
                "INSERT INTO memories "
                "(namespace,id,key,kind,state,content_json,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    self.namespace,
                    ref.id,
                    key,
                    kind.value,
                    State.SUPPORTED.value,
                    payload,
                    source,
                    now,
                ),
            )
            for index, group in enumerate(supports):
                self._connection.executemany(
                    "INSERT INTO supports(namespace,child_id,clause,parent_id) VALUES (?,?,?,?)",
                    [(self.namespace, ref.id, index, parent.id) for parent in group],
                )
            self._event(ref, State.SUPPORTED, reason, now)
            self._connection.execute(
                "INSERT INTO heads(namespace,key,memory_id) VALUES (?,?,?) "
                "ON CONFLICT(namespace,key) DO UPDATE SET memory_id = excluded.memory_id",
                (self.namespace, key, ref.id),
            )
            invalidated: tuple[MemoryRef, ...] = ()
            rechecked: tuple[MemoryRef, ...] = ()
            preserved: tuple[MemoryRef, ...] = ()
            if previous is not None:
                self._connection.execute(
                    "UPDATE memories SET state = ?, superseded_by = ? "
                    "WHERE namespace = ? AND id = ?",
                    (State.SUPERSEDED.value, ref.id, self.namespace, previous.ref.id),
                )
                self._event(
                    previous.ref,
                    State.SUPERSEDED,
                    reason,
                    now,
                    caused_by=previous.ref,
                    replacement=ref,
                )
                if previous.state is State.SUPPORTED:
                    invalidated, rechecked, preserved = self._invalidate(previous.ref, now)
            if kind is Kind.DERIVED and not self._has_support(ref):
                raise UnsupportedMemory("No complete declared support set survives this revision.")
            return Change(self._load(ref), invalidated, rechecked, preserved)

    def _has_support(self, ref: MemoryRef) -> bool:
        row = self._connection.execute(
            "SELECT EXISTS(SELECT s.clause FROM supports s JOIN memories p ON "
            "s.namespace = p.namespace AND s.parent_id = p.id "
            "WHERE s.namespace = ? AND s.child_id = ? GROUP BY s.clause "
            "HAVING SUM(p.state != 'supported') = 0)",
            (self.namespace, ref.id),
        ).fetchone()
        return bool(row[0])

    def _children(self, ref: MemoryRef) -> list[tuple[int, str]]:
        return [
            (row[0], row[1])
            for row in self._connection.execute(
                "SELECT DISTINCT m.sequence,m.id FROM supports s JOIN memories m ON "
                "s.namespace = m.namespace AND s.child_id = m.id "
                "WHERE s.namespace = ? AND s.parent_id = ? AND m.state = 'supported'",
                (self.namespace, ref.id),
            )
        ]

    def _invalidate(
        self, cause: MemoryRef, now: str
    ) -> tuple[tuple[MemoryRef, ...], tuple[MemoryRef, ...], tuple[MemoryRef, ...]]:
        frontier = self._children(cause)
        heapq.heapify(frontier)
        visited: set[str] = set()
        invalidated: list[MemoryRef] = []
        checked: list[MemoryRef] = []
        preserved: list[MemoryRef] = []
        while frontier:
            _, identifier = heapq.heappop(frontier)
            if identifier in visited:
                continue
            visited.add(identifier)
            ref = MemoryRef(self.namespace, identifier)
            checked.append(ref)
            if self._has_support(ref):
                preserved.append(ref)
                continue
            self._connection.execute(
                "UPDATE memories SET state = 'invalidated' WHERE namespace = ? AND id = ?",
                (self.namespace, identifier),
            )
            self._event(
                ref,
                State.INVALIDATED,
                "All declared support sets lost a supported premise.",
                now,
                caused_by=cause,
            )
            invalidated.append(ref)
            for child in self._children(ref):
                heapq.heappush(frontier, child)
        return tuple(invalidated), tuple(checked), tuple(preserved)

    def retract(self, ref: MemoryRef, *, reason: str) -> Change:
        text(reason, "reason")
        with self._transaction(write=True):
            memory = self._load(ref)
            if self._head(memory.key) != ref:
                raise RevisionConflict("Only the current revision can be retracted.")
            if memory.state is not State.SUPPORTED:
                raise UnsupportedMemory("The current revision is already unsupported.")
            now = datetime.now(UTC).isoformat()
            self._connection.execute(
                "UPDATE memories SET state = 'retracted' WHERE namespace = ? AND id = ?",
                (self.namespace, ref.id),
            )
            self._event(ref, State.RETRACTED, reason, now, caused_by=ref)
            invalidated, checked, preserved = self._invalidate(ref, now)
            return Change(self._load(ref), invalidated, checked, preserved)

    def _event(
        self,
        ref: MemoryRef,
        state: State,
        reason: str,
        now: str,
        *,
        caused_by: MemoryRef | None = None,
        replacement: MemoryRef | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO events"
            "(namespace,memory_id,state,reason,caused_by,replacement,recorded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                self.namespace,
                ref.id,
                state.value,
                reason,
                caused_by.id if caused_by is not None else None,
                replacement.id if replacement is not None else None,
                now,
            ),
        )

    def _events(self, ref: MemoryRef) -> tuple[MemoryEvent, ...]:
        return tuple(
            MemoryEvent(
                row["sequence"],
                ref,
                State(row["state"]),
                row["reason"],
                MemoryRef(self.namespace, row["caused_by"])
                if row["caused_by"] is not None
                else None,
                MemoryRef(self.namespace, row["replacement"])
                if row["replacement"] is not None
                else None,
                row["recorded_at"],
            )
            for row in self._connection.execute(
                "SELECT * FROM events WHERE namespace = ? AND memory_id = ? ORDER BY sequence",
                (self.namespace, self._ref(ref)),
            )
        )

    def history(self, ref: MemoryRef) -> tuple[MemoryEvent, ...]:
        with self._transaction():
            self._load(ref)
            return self._events(ref)

    def explain(self, ref: MemoryRef) -> Explanation:
        with self._transaction():
            nodes: dict[MemoryRef, Memory] = {}
            pending = [ref]
            while pending:
                current = pending.pop()
                if current in nodes:
                    continue
                memory = self._load(current)
                nodes[current] = memory
                pending.extend(parent for group in memory.supports for parent in group)
            memories = tuple(sorted(nodes.values(), key=lambda memory: memory.sequence))
            events = tuple(
                sorted(
                    (event for memory in memories for event in self._events(memory.ref)),
                    key=lambda event: event.sequence,
                )
            )
            return Explanation(nodes[ref], memories, events)

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> MemoryStore:
        self._check_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
