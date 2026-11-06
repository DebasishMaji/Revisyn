"""Durable justification maintenance for explicitly declared, acyclic memory."""

from __future__ import annotations

import errno
import heapq
import os
import sqlite3
import tempfile
import threading
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
    ReadOnlyStore,
    RevisionConflict,
    SchemaError,
    SnapshotUsageError,
    StoreClosed,
    StoreUsageError,
    UnsupportedMemory,
)
from .models import (
    Change,
    Explanation,
    JSONValue,
    Kind,
    Memory,
    MemoryEvent,
    MemoryEventPage,
    MemoryPage,
    MemoryRef,
    State,
    encode_content,
    text,
)
from .snapshot import SnapshotInfo, database_issues, deadline_for, sqlite_deadline


def _page_arguments(after_sequence: int, limit: int) -> None:
    if (
        isinstance(after_sequence, bool)
        or not isinstance(after_sequence, int)
        or not 0 <= after_sequence <= 2**63 - 1
    ):
        raise ValueError("after_sequence must be a nonnegative SQLite integer.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000.")


def _provision(path: Path, *, require_existing: bool) -> None:
    # Opening and closing an existing file outside SQLite can drop another connection's locks.
    try:
        path.stat()
    except FileNotFoundError:
        if require_existing:
            raise
        descriptor, name = tempfile.mkstemp(prefix=".revisyn-provision-", dir=path.parent)
        temporary = Path(name)
        try:
            os.close(descriptor)
            try:
                os.link(temporary, path)
            except FileExistsError:
                # A concurrent provisioner won; SQLite will validate its file without replacing it.
                pass
        finally:
            temporary.unlink()
    if not os.access(path, os.R_OK | os.W_OK, effective_ids=True):
        raise PermissionError(errno.EACCES, "Read/write database access is required.", path)


class MemoryStore:
    def __init__(
        self,
        path: str | Path,
        *,
        namespace: str,
        read_only: bool = False,
        require_existing: bool = False,
    ) -> None:
        if not isinstance(read_only, bool) or not isinstance(require_existing, bool):
            raise TypeError("read_only and require_existing must be booleans.")
        self._namespace = text(namespace, "namespace")
        self.path = Path(path)
        self._owner = os.getpid(), threading.get_ident()
        self._read_only = read_only
        self._require_existing = require_existing
        self._snapshot_info: SnapshotInfo | None = None
        if str(path) == ":memory:":
            raise ValueError("MemoryStore requires a durable filesystem path.")
        database_path = self.path.resolve()
        if not read_only:
            _provision(database_path, require_existing=require_existing)
        with ExitStack() as setup:
            mode = "ro" if read_only else "rw"
            self._connection = sqlite3.connect(
                f"{database_path.as_uri()}?mode={mode}",
                uri=True,
                isolation_level=None,
                timeout=5.0,
            )
            setup.callback(self._connection.close)
            self._closed = False
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            if read_only:
                self._connection.execute("PRAGMA query_only = ON")
                with self._transaction():
                    self.schema_version, self._snapshot_info = _schema.read_format(
                        self._connection, allow_snapshot=True
                    )
            else:
                with self._transaction(write=True):
                    self._initialize()
                self.schema_version = _schema.VERSION
                mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if mode != "wal":
                    raise SchemaError("Revisyn requires SQLite write-ahead logging.")
            setup.pop_all()

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def snapshot_info(self) -> SnapshotInfo | None:
        return self._snapshot_info

    def _initialize(self) -> None:
        application = self._connection.execute("PRAGMA application_id").fetchone()[0]
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if application != 0 or version != 0:
            _schema.read_format(self._connection)
            return
        tables = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if tables or self._require_existing:
            raise SchemaError("Not an empty database or a supported Revisyn schema.")
        for statement in _schema.STATEMENTS:
            self._connection.execute(statement)
        self._connection.execute(f"PRAGMA application_id = {_schema.APPLICATION_ID}")
        self._connection.execute(f"PRAGMA user_version = {_schema.VERSION}")

    def _check_open(self) -> None:
        if self._closed:
            raise StoreClosed("The memory store is closed.")
        if self._owner != (os.getpid(), threading.get_ident()):
            raise StoreUsageError("Use a memory store only in its owning process and thread.")

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[None]:
        self._check_open()
        if write and self.read_only:
            raise ReadOnlyStore("Read-only stores cannot modify evidence or history.")
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

    def recall(self, key: str, *, expected_ref: MemoryRef | None = None) -> Memory:
        """Return the supported current revision, optionally requiring an exact reference."""
        text(key, "key")
        self._check_recall()
        with self._transaction():
            return self._supported_current(key, expected_ref=expected_ref)

    def recall_many(self, keys: Sequence[str]) -> tuple[Memory, ...]:
        """Return supported current revisions in input order from one read transaction."""
        if isinstance(keys, (str, bytes)) or not isinstance(keys, Sequence):
            raise TypeError("keys must be a sequence of key strings.")
        requested = tuple(keys)
        for key in requested:
            text(key, "key")
        self._check_recall()
        with self._transaction():
            return tuple(self._supported_current(key) for key in requested)

    def _check_recall(self) -> None:
        self._check_open()
        if self.snapshot_info is not None:
            raise SnapshotUsageError(
                "An inspection snapshot cannot authorize current memory reuse."
            )

    def _supported_current(self, key: str, *, expected_ref: MemoryRef | None = None) -> Memory:
        if expected_ref is not None:
            self._ref(expected_ref)
        memory = self._current(key)
        if expected_ref is not None and memory.ref != expected_ref:
            raise RevisionConflict(f"Memory {key!r} no longer matches the expected revision.")
        if memory.state is not State.SUPPORTED:
            raise UnsupportedMemory(f"Memory {key!r} is {memory.state.value}.")
        return memory

    def _current(self, key: str) -> Memory:
        ref = self._head(key)
        if ref is None:
            raise MemoryNotFound(f"No memory with key {key!r}.")
        return self._load(ref)

    def inspect_current(self, key: str) -> Memory:
        """Audit a head, including unsupported content; never use this as a reuse gate."""
        text(key, "key")
        with self._transaction():
            return self._current(key)

    def list_memories(
        self, *, include_history: bool = False, after_sequence: int = 0, limit: int = 100
    ) -> MemoryPage:
        _page_arguments(after_sequence, limit)
        if not isinstance(include_history, bool):
            raise TypeError("include_history must be a boolean.")
        with self._transaction():
            join = (
                ""
                if include_history
                else (" JOIN heads h ON m.namespace = h.namespace AND m.id = h.memory_id")
            )
            rows = self._connection.execute(
                f"SELECT m.id FROM memories m{join} WHERE m.namespace = ? AND m.sequence > ? "
                "ORDER BY m.sequence LIMIT ?",
                (self.namespace, after_sequence, limit + 1),
            ).fetchall()
            memories = tuple(self._load(MemoryRef(self.namespace, row[0])) for row in rows[:limit])
            return MemoryPage(memories, memories[-1].sequence if len(rows) > limit else None)

    def status(self, *, timeout_seconds: float = 5.0) -> dict[str, JSONValue]:
        with self._transaction(), sqlite_deadline(self._connection, deadline_for(timeout_seconds)):
            issues = database_issues(self._connection)
            counts: dict[str, JSONValue] = {
                row[0]: row[1]
                for row in self._connection.execute(
                    "SELECT state, COUNT(*) FROM memories WHERE namespace = ? GROUP BY state",
                    (self.namespace,),
                )
            }
            events = self._connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), 0) FROM events WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()
            return {
                "format_version": 1,
                "source_schema_version": self.schema_version,
                "namespace": self.namespace,
                "healthy": not issues,
                "issues": issues,
                "memories_by_state": counts,
                "event_count": events[0],
                "last_event_sequence": events[1],
                "snapshot": self.snapshot_info.as_dict()
                if self.snapshot_info is not None
                else None,
            }

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
            self._event(
                ref,
                State.SUPPORTED,
                reason,
                now,
                caused_by=previous.ref if previous is not None else None,
            )
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

    def _events(
        self, ref: MemoryRef, *, after_sequence: int = 0, limit: int | None = None
    ) -> tuple[MemoryEvent, ...]:
        parameters: list[str | int] = [self.namespace, self._ref(ref), after_sequence]
        suffix = ""
        if limit is not None:
            suffix = " LIMIT ?"
            parameters.append(limit)
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
                "SELECT * FROM events WHERE namespace = ? AND memory_id = ? "
                f"AND sequence > ? ORDER BY sequence{suffix}",
                parameters,
            )
        )

    def history(self, ref: MemoryRef) -> tuple[MemoryEvent, ...]:
        with self._transaction():
            self._load(ref)
            return self._events(ref)

    def list_events(
        self, ref: MemoryRef, *, after_sequence: int = 0, limit: int = 100
    ) -> MemoryEventPage:
        _page_arguments(after_sequence, limit)
        with self._transaction():
            self._load(ref)
            events = self._events(ref, after_sequence=after_sequence, limit=limit + 1)
            page = events[:limit]
            return MemoryEventPage(page, page[-1].sequence if len(events) > limit else None)

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
            self._check_open()
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
