"""Read-only memory operations and the disposable memory-change laboratory."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from importlib.metadata import version

from .errors import RevisynError
from .lab import run_lab
from .models import JSONValue, Memory, MemoryEvent, MemoryRef
from .snapshot import deadline_for, snapshot_database
from .store import MemoryStore


def _nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _sequence(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative SQLite integer") from exc
    if not 0 <= result <= 2**63 - 1:
        raise argparse.ArgumentTypeError("must be a nonnegative SQLite integer")
    return result


def _limit(value: str) -> int:
    result = _sequence(value)
    if not 1 <= result <= 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return result


def _timeout(value: str) -> float:
    try:
        result = float(value)
        deadline_for(result)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive finite number") from exc
    return result


def _reference(ref: MemoryRef | None) -> JSONValue:
    return {"namespace": ref.namespace, "id": ref.id} if ref is not None else None


def _memory(memory: Memory, *, include_payload: bool) -> dict[str, JSONValue]:
    supports: list[JSONValue] = [[_reference(ref) for ref in clause] for clause in memory.supports]
    result: dict[str, JSONValue] = {
        "sequence": memory.sequence,
        "reference": _reference(memory.ref),
        "key": memory.key,
        "kind": memory.kind.value,
        "state": memory.state.value,
        "source": memory.source,
        "supports": supports,
        "created_at": memory.created_at,
        "superseded_by": _reference(memory.superseded_by),
    }
    if include_payload:
        result["content"] = memory.content
    return result


def _event(event: MemoryEvent) -> dict[str, JSONValue]:
    return {
        "sequence": event.sequence,
        "reference": _reference(event.ref),
        "state": event.state.value,
        "reason": event.reason,
        "caused_by": _reference(event.caused_by),
        "replacement": _reference(event.replacement),
        "recorded_at": event.recorded_at,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="revisyn", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('revisyn')}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("lab", help="compare plan reuse after a controlled fact change")
    snapshot = commands.add_parser(
        "snapshot", help="capture all namespaces and content into an inspection-only artifact"
    )
    snapshot.add_argument("database", type=_nonempty)
    snapshot.add_argument("--out", type=_nonempty, required=True)
    snapshot.add_argument("--timeout", type=_timeout, default=30.0)
    for name in ("list", "inspect", "history", "status"):
        command = commands.add_parser(name, help=f"read-only {name} of an existing database")
        command.add_argument("database", type=_nonempty)
        command.add_argument("--namespace", type=_nonempty, required=True)
        if name in ("list", "history"):
            command.add_argument("--after-sequence", type=_sequence, default=0)
            command.add_argument("--limit", type=_limit, default=100)
        if name in ("list", "inspect"):
            command.add_argument("--include-payload", action="store_true")
        if name == "list":
            command.add_argument("--include-history", action="store_true")
        if name == "inspect":
            selector = command.add_mutually_exclusive_group(required=True)
            selector.add_argument("--key", type=_nonempty)
            selector.add_argument("--id", type=_nonempty)
        if name == "history":
            command.add_argument("--id", type=_nonempty, required=True)
        if name == "status":
            command.add_argument("--timeout", type=_timeout, default=5.0)
    return parser


def _query(store: MemoryStore, arguments: argparse.Namespace) -> dict[str, JSONValue]:
    report: dict[str, JSONValue] = {
        "format_version": 1,
        "namespace": store.namespace,
        "source_schema_version": store.schema_version,
        "snapshot": store.snapshot_info.as_dict() if store.snapshot_info is not None else None,
    }
    if arguments.command == "status":
        return store.status(timeout_seconds=arguments.timeout)
    if arguments.command == "list":
        page = store.list_memories(
            include_history=arguments.include_history,
            after_sequence=arguments.after_sequence,
            limit=arguments.limit,
        )
        report["memories"] = [
            _memory(memory, include_payload=arguments.include_payload) for memory in page.memories
        ]
        report["next_sequence"] = page.next_sequence
    elif arguments.command == "inspect":
        memory = (
            store.inspect_current(arguments.key)
            if arguments.key is not None
            else store.get(MemoryRef(store.namespace, arguments.id))
        )
        report["memory"] = _memory(memory, include_payload=arguments.include_payload)
    else:
        events = store.list_events(
            MemoryRef(store.namespace, arguments.id),
            after_sequence=arguments.after_sequence,
            limit=arguments.limit,
        )
        report["events"] = [_event(event) for event in events.events]
        report["next_sequence"] = events.next_sequence
    return report


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    exit_code = 0
    try:
        if arguments.command == "lab":
            report = run_lab().as_dict()
        elif arguments.command == "snapshot":
            report = snapshot_database(
                arguments.database, arguments.out, timeout_seconds=arguments.timeout
            ).as_dict()
        else:
            with MemoryStore(
                arguments.database, namespace=arguments.namespace, read_only=True
            ) as store:
                report = _query(store, arguments)
                if arguments.command == "status":
                    exit_code = 0 if report["healthy"] else 3
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    except (RevisynError, sqlite3.Error, OSError) as exc:
        print(f"revisyn: error: {exc}", file=sys.stderr)
        return 1
    return exit_code
