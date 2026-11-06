import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from revisyn import (
    MemoryStore,
    NamespaceMismatch,
    ReadOnlyStore,
    SchemaError,
    SnapshotUsageError,
    StoreUsageError,
    UnsupportedMemory,
    snapshot,
    snapshot_database,
)


def cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "revisyn", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def assert_sqlite_main_lock_held(path: Path) -> None:
    """Probe SQLite's shared-lock range from a child, leaving this process's locks alone."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import fcntl
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0x40000002)
    except BlockingIOError:
        print("held")
    else:
        print("released")
finally:
    os.close(descriptor)
""",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "held"


@pytest.mark.parametrize("mode", ["default", "require_existing", "read_only"])
def test_starting_another_store_preserves_the_original_sqlite_lock(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / "live.db"
    with MemoryStore(path, namespace="orders") as original:
        owner = original.observe("owner", "A", source="catalog").memory
        assert_sqlite_main_lock_held(path)
        with MemoryStore(
            path,
            namespace="orders",
            require_existing=mode == "require_existing",
            read_only=mode == "read_only",
        ) as another:
            assert_sqlite_main_lock_held(path)
            assert another.recall("owner") == owner
        assert_sqlite_main_lock_held(path)
        assert original.recall("owner") == owner


def test_provisioning_never_overwrites_a_concurrent_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live.db"

    def race(source: Path, target: Path) -> None:
        target.write_bytes(b"another provisioner's file")
        raise FileExistsError("Another provisioner won.")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", race)
        with pytest.raises(sqlite3.DatabaseError):
            MemoryStore(path, namespace="orders")
    assert path.read_bytes() == b"another provisioner's file"
    assert not list(tmp_path.glob(".revisyn-provision-*"))


def test_failed_provisioning_cleans_temporary_files_without_creating_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live.db"

    def fail(source: Path, target: Path) -> None:
        raise OSError("Injected provisioning failure.")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", fail)
        with pytest.raises(OSError, match="Injected provisioning"):
            MemoryStore(path, namespace="orders")
    assert not path.exists()
    assert not list(tmp_path.glob(".revisyn-provision-*"))
    with MemoryStore(path, namespace="orders"):
        pass
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.geteuid() == 0, reason="Root can write files without write mode bits.")
def test_writable_store_still_rejects_a_filesystem_read_only_database(tmp_path: Path) -> None:
    path = tmp_path / "live.db"
    with MemoryStore(path, namespace="orders"):
        pass
    path.chmod(0o400)
    try:
        with pytest.raises(PermissionError):
            MemoryStore(path, namespace="orders", require_existing=True)
        with MemoryStore(path, namespace="orders", read_only=True) as reader:
            assert reader.memories() == ()
    finally:
        path.chmod(0o600)


def test_read_only_recall_is_fresh_and_all_mutations_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "memory ?#.db"
    with MemoryStore(path, namespace="orders") as writer:
        fact = writer.observe("owner", {"team": "A"}, source="catalog").memory
        plan = writer.derive("plan", "secret plan", supports=((fact.ref,),), rule="plan/1").memory
        with MemoryStore(path, namespace="orders", read_only=True) as reader:
            assert reader.read_only
            assert reader.recall("plan") == plan
            writer.observe("owner", {"team": "B"}, source="catalog/2", replaces=fact.ref)
            with pytest.raises(UnsupportedMemory):
                reader.recall("plan")
            assert reader.inspect_current("owner").content == {"team": "B"}
            assert reader.get(plan.ref).content == "secret plan"
            for action in (
                lambda: reader.observe("new", 1, source="catalog"),
                lambda: reader.derive("new", 1, supports=((plan.ref,),), rule="rule"),
                lambda: reader.retract(plan.ref, reason="reason"),
            ):
                with pytest.raises(ReadOnlyStore):
                    action()
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader._connection.execute("DELETE FROM events")
    before = path.read_bytes()
    with MemoryStore(path, namespace="orders", read_only=True) as reader:
        reader.status()
    assert path.read_bytes() == before


def test_require_existing_and_read_only_never_bootstrap_missing_or_empty_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        MemoryStore(path, namespace="orders", require_existing=True)
    with pytest.raises(sqlite3.OperationalError):
        MemoryStore(path, namespace="orders", read_only=True)
    assert not path.exists()
    path.touch()
    with pytest.raises(SchemaError):
        MemoryStore(path, namespace="orders", require_existing=True)
    with pytest.raises(SchemaError):
        MemoryStore(path, namespace="orders", read_only=True)
    assert path.read_bytes() == b""
    with MemoryStore(path, namespace="orders"):
        pass
    with MemoryStore(path, namespace="orders", require_existing=True):
        pass
    with pytest.raises(TypeError):
        MemoryStore(tmp_path / "invalid.db", namespace="orders", read_only="yes")
    assert not (tmp_path / "invalid.db").exists()


def test_owner_checks_cover_reads_writes_close_and_fork_misuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with MemoryStore(tmp_path / "live.db", namespace="orders") as store:
        with ThreadPoolExecutor(max_workers=1) as worker:
            for action in (
                lambda: store.memories(),
                lambda: store.observe("owner", 1, source="catalog"),
                store.close,
            ):
                with pytest.raises(StoreUsageError):
                    worker.submit(action).result()
        original = os.getpid()
        with monkeypatch.context() as patch:
            patch.setattr(os, "getpid", lambda: original + 1)
            with pytest.raises(StoreUsageError):
                store.memories()
            with pytest.raises(StoreUsageError):
                store.close()
        assert store.memories() == ()


def test_memory_and_event_pages_are_ordered_scoped_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "live.db"
    with MemoryStore(path, namespace="orders") as store:
        old = store.observe("owner", "A", source="catalog").memory
        first_plan = store.derive("plan", "p", supports=((old.ref,),), rule="plan").memory
        store.observe("owner", "B", source="catalog/2", replaces=old.ref)
        with MemoryStore(path, namespace="other") as other:
            hidden = other.observe("secret", "other secret", source="private").memory
        first = store.list_memories(include_history=True, limit=2)
        second = store.list_memories(
            include_history=True, after_sequence=first.next_sequence, limit=2
        )
        assert first.memories == (store.get(old.ref), store.get(first_plan.ref))
        assert len(second.memories) == 1
        assert second.next_sequence is None
        assert hidden not in first.memories + second.memories
        assert len(store.list_memories().memories) == 2
        events = store.list_events(first_plan.ref, limit=1)
        last = store.list_events(first_plan.ref, after_sequence=events.next_sequence, limit=1)
        assert events.events + last.events == store.history(first_plan.ref)
        assert last.next_sequence is None
        with pytest.raises(NamespaceMismatch):
            store.list_events(hidden.ref)
        for limit in (0, 1001, True, 1.5):
            with pytest.raises(ValueError):
                store.list_memories(limit=limit)
            with pytest.raises(ValueError):
                store.list_events(old.ref, limit=limit)
        for sequence in (-1, True, 2**63):
            with pytest.raises(ValueError):
                store.list_memories(after_sequence=sequence)
        assert store.list_memories(after_sequence=2**63 - 1).memories == ()


def test_full_database_snapshot_keeps_every_namespace_and_refuses_operational_reuse(
    tmp_path: Path,
) -> None:
    path, target = tmp_path / "live.db", tmp_path / "capture.db"
    with MemoryStore(path, namespace="orders") as store:
        owner = store.observe("owner", "A", source="catalog").memory
        plan = store.derive("plan", "private plan", supports=((owner.ref,),), rule="plan").memory
        with MemoryStore(path, namespace="other") as other:
            other_ref = other.observe("secret", {"token": "demo-only"}, source="private").memory.ref
        artifact = snapshot_database(path, target)
        store.retract(owner.ref, reason="ownership changed after capture")
        with MemoryStore(target, namespace="orders", read_only=True) as reader:
            assert reader.get(plan.ref) == plan
            assert reader.explain(plan.ref).satisfied_supports == ((owner.ref,),)
            assert reader.snapshot_info == artifact.info
            with pytest.raises(SnapshotUsageError):
                reader.recall("plan")
        with MemoryStore(target, namespace="other", read_only=True) as reader:
            assert reader.get(other_ref).content == {"token": "demo-only"}
        with pytest.raises(UnsupportedMemory):
            store.recall("plan")
    assert artifact.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    assert artifact.size_bytes == target.stat().st_size
    assert target.stat().st_mode & 0o777 == 0o600
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{target}{suffix}").exists()
    before = target.read_bytes()
    with pytest.raises(SnapshotUsageError):
        MemoryStore(target, namespace="orders")
    assert target.read_bytes() == before
    with pytest.raises(SnapshotUsageError):
        snapshot_database(target, tmp_path / "relabeled.db")


def test_capture_keeps_a_consistent_view_when_evidence_changes_during_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, target = tmp_path / "live.db", tmp_path / "capture.db"
    with MemoryStore(path, namespace="orders") as writer:
        fact = writer.observe("owner", "A", source="catalog").memory
        plan = writer.derive("plan", "old plan", supports=((fact.ref,),), rule="plan").memory
        original = snapshot.publish_snapshot

        def change_then_copy(db, source, destination, **kwargs):
            writer.retract(fact.ref, reason="concurrent evidence change")
            return original(db, source, destination, **kwargs)

        monkeypatch.setattr(snapshot, "publish_snapshot", change_then_copy)
        receipt = snapshot_database(path, target)
        with pytest.raises(UnsupportedMemory):
            writer.recall("plan")
        with MemoryStore(target, namespace="orders", read_only=True) as saved:
            assert saved.get(plan.ref) == plan
            assert saved.get(fact.ref) == fact
            assert saved.status()["healthy"] is True
            assert receipt.info.source_event_sequence == 2


def test_corrupt_foreign_keys_are_reported_and_never_published(tmp_path: Path) -> None:
    path, target = tmp_path / "live.db", tmp_path / "capture.db"
    with MemoryStore(path, namespace="orders"):
        pass
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO heads (namespace, key, memory_id) VALUES ('orders', 'bad', 'absent')"
        )
    result = cli("status", str(path), "--namespace", "orders")
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stdout)["healthy"] is False
    with pytest.raises(SchemaError, match="inconsistent"):
        snapshot_database(path, target)
    assert not target.exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal", ".lock"])
def test_snapshot_never_targets_source_or_sidecars(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / "live.db"
    with MemoryStore(path, namespace="orders"):
        with pytest.raises(SnapshotUsageError):
            snapshot_database(path, Path(f"{path}{suffix}"))


def test_case_alias_sidecars_and_hard_link_sources_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "Live.db"
    with MemoryStore(source, namespace="orders"):
        pass
    with pytest.raises(SnapshotUsageError):
        snapshot_database(source, tmp_path / "live.DB-WAL")
    alias = tmp_path / "alias.db"
    os.link(source, alias)
    with pytest.raises(SnapshotUsageError, match="hard-link"):
        snapshot_database(alias, tmp_path / "capture.db")


def test_dangling_symlinks_and_publication_races_are_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, target = tmp_path / "live.db", tmp_path / "capture.db"
    with MemoryStore(path, namespace="orders"):
        pass
    target.symlink_to(tmp_path / "absent.db")
    with pytest.raises(FileExistsError):
        snapshot_database(path, target)
    assert target.is_symlink()
    target.unlink()
    original = os.link

    def race(first: Path, second: Path) -> None:
        second.write_bytes(b"winner")
        original(first, second)

    monkeypatch.setattr(snapshot.os, "link", race)
    with pytest.raises(FileExistsError):
        snapshot_database(path, target)
    assert target.read_bytes() == b"winner"
    assert not list(tmp_path.glob(".inspection-*"))


@pytest.mark.parametrize("failure_call", [1, 2])
def test_snapshot_sync_failures_never_look_like_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int
) -> None:
    path, target = tmp_path / "live.db", tmp_path / "capture.db"
    with MemoryStore(path, namespace="orders"):
        pass
    original = os.fsync
    calls = 0

    def fail(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("injected sync failure")
        original(descriptor)

    monkeypatch.setattr(snapshot.os, "fsync", fail)
    with pytest.raises(OSError, match="injected"):
        snapshot_database(path, target)
    assert target.exists() is (failure_call == 2)
    if target.exists():
        with MemoryStore(target, namespace="orders", read_only=True) as reader:
            assert reader.status()["healthy"] is True
    assert not list(tmp_path.glob(".inspection-*"))


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_snapshot_invalid_and_expired_budgets_leave_no_files(
    tmp_path: Path, timeout: float
) -> None:
    path, target = tmp_path / "live.db", tmp_path / "capture.db"
    with MemoryStore(path, namespace="orders"):
        pass
    with pytest.raises(ValueError):
        snapshot_database(path, target, timeout_seconds=timeout)
    with pytest.raises(TimeoutError):
        snapshot_database(path, target, timeout_seconds=1e-12)
    assert not target.exists()
    assert not list(tmp_path.glob(".inspection-*"))


def test_missing_and_spoofed_sources_are_not_initialized(tmp_path: Path) -> None:
    path, target = tmp_path / "live.db", tmp_path / "capture.db"
    with pytest.raises(sqlite3.OperationalError):
        snapshot_database(path, target)
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA application_id = 0x5256594E")
        db.execute("PRAGMA user_version = 1")
    before = path.read_bytes()
    with pytest.raises(SchemaError):
        snapshot_database(path, target)
    assert path.read_bytes() == before
    assert not target.exists()


def test_partial_backup_timeout_removes_only_its_private_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "live.db", tmp_path / "capture.db"
    with MemoryStore(source, namespace="orders") as store:
        reference = store.observe("large", "x" * 2_000_000, source="catalog").memory.ref
    copying = False
    partial = False
    original = sqlite3.connect

    class TimedConnection(sqlite3.Connection):
        def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.250):
            nonlocal copying
            copying = True

            def step(status, remaining, total):
                nonlocal partial
                partial = remaining > 0
                progress(status, remaining, total)

            return super().backup(target, pages=pages, progress=step, name=name, sleep=sleep)

    monkeypatch.setattr(snapshot, "monotonic", lambda: 31.0 if copying else 0.0)
    monkeypatch.setattr(
        snapshot.sqlite3,
        "connect",
        lambda *args, **kwargs: original(*args, **kwargs, factory=TimedConnection),
    )
    with pytest.raises(TimeoutError):
        snapshot_database(source, target, timeout_seconds=30.0)
    assert partial
    assert not target.exists()
    assert not list(tmp_path.glob(".inspection-*"))
    with MemoryStore(source, namespace="orders", require_existing=True) as store:
        assert store.get(reference).content == "x" * 2_000_000


def test_operator_cli_has_payload_opt_in_pages_snapshot_and_explicit_failures(
    tmp_path: Path,
) -> None:
    path, target = tmp_path / "live.db", tmp_path / "capture.db"
    with MemoryStore(path, namespace="orders") as store:
        ref = store.observe("owner", {"private": "value"}, source="catalog").memory.ref
        store.observe("other", "second", source="catalog")
        listed = cli("list", str(path), "--namespace", "orders", "--limit", "1")
        assert listed.returncode == 0, listed.stderr
        page = json.loads(listed.stdout)
        assert "content" not in page["memories"][0]
        next_page = cli(
            "list",
            str(path),
            "--namespace",
            "orders",
            "--after-sequence",
            str(page["next_sequence"]),
        )
        assert json.loads(next_page.stdout)["memories"][0]["key"] == "other"
        detail = cli(
            "inspect", str(path), "--namespace", "orders", "--key", "owner", "--include-payload"
        )
        assert json.loads(detail.stdout)["memory"]["content"] == {"private": "value"}
        history = cli("history", str(path), "--namespace", "orders", "--id", ref.id)
        assert len(json.loads(history.stdout)["events"]) == 1
        captured = cli("snapshot", str(path), "--out", str(target))
        assert captured.returncode == 0, captured.stderr
    assert json.loads(captured.stdout)["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    saved = cli("status", str(target), "--namespace", "orders")
    assert json.loads(saved.stdout)["snapshot"]["inspection_only"] is True
    for flag, value in (("--limit", "1001"), ("--after-sequence", str(2**63))):
        invalid = cli("list", str(path), "--namespace", "orders", flag, value)
        assert invalid.returncode == 2
        assert invalid.stdout == ""
    missing = tmp_path / "missing.db"
    result = cli("status", str(missing), "--namespace", "orders")
    assert result.returncode == 1
    assert result.stdout == ""
    assert not missing.exists()
