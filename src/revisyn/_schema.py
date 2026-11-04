import sqlite3

from .errors import SchemaError, SnapshotUsageError
from .snapshot import (
    SNAPSHOT_APPLICATION_ID,
    SNAPSHOT_VERSION,
    SnapshotInfo,
    read_snapshot_info,
)

APPLICATION_ID = 0x5256594E
VERSION = 1

STATEMENTS = (
    """
    CREATE TABLE memories (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        namespace TEXT NOT NULL,
        id TEXT NOT NULL,
        key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('fact', 'derived')),
        state TEXT NOT NULL CHECK (state IN
            ('supported', 'invalidated', 'retracted', 'superseded')),
        content_json TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        superseded_by TEXT,
        UNIQUE (namespace, id),
        FOREIGN KEY (namespace, superseded_by) REFERENCES memories(namespace, id),
        CHECK ((state = 'superseded') = (superseded_by IS NOT NULL))
    )
    """,
    """
    CREATE TABLE heads (
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        memory_id TEXT NOT NULL,
        PRIMARY KEY (namespace, key),
        FOREIGN KEY (namespace, memory_id) REFERENCES memories(namespace, id)
    )
    """,
    """
    CREATE TABLE supports (
        namespace TEXT NOT NULL,
        child_id TEXT NOT NULL,
        clause INTEGER NOT NULL CHECK (clause >= 0),
        parent_id TEXT NOT NULL,
        PRIMARY KEY (namespace, child_id, clause, parent_id),
        FOREIGN KEY (namespace, child_id) REFERENCES memories(namespace, id),
        FOREIGN KEY (namespace, parent_id) REFERENCES memories(namespace, id)
    )
    """,
    "CREATE INDEX support_children ON supports(namespace, parent_id, child_id)",
    """
    CREATE TABLE events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        namespace TEXT NOT NULL,
        memory_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('supported', 'invalidated', 'retracted', 'superseded')),
        reason TEXT NOT NULL,
        caused_by TEXT,
        replacement TEXT,
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (namespace, memory_id) REFERENCES memories(namespace, id),
        FOREIGN KEY (namespace, caused_by) REFERENCES memories(namespace, id),
        FOREIGN KEY (namespace, replacement) REFERENCES memories(namespace, id)
    )
    """,
    "CREATE INDEX memory_events ON events(namespace, memory_id, sequence)",
)


def read_format(
    db: sqlite3.Connection, *, allow_snapshot: bool = False
) -> tuple[int, SnapshotInfo | None]:
    application = db.execute("PRAGMA application_id").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    info = None
    if application == SNAPSHOT_APPLICATION_ID or version == SNAPSHOT_VERSION:
        if not allow_snapshot:
            raise SnapshotUsageError("Inspection snapshots cannot be used as writable stores.")
        if application != SNAPSHOT_APPLICATION_ID or version != SNAPSHOT_VERSION:
            raise SchemaError("Invalid inspection snapshot format.")
        info = read_snapshot_info(db)
        version = info.source_schema_version
    elif application != APPLICATION_ID:
        raise SchemaError("Not a supported Revisyn application identifier.")
    if version != VERSION:
        raise SchemaError(f"Unsupported Revisyn schema version {version}.")
    expected = {
        "memories": (
            "sequence",
            "namespace",
            "id",
            "key",
            "kind",
            "state",
            "content_json",
            "source",
            "created_at",
            "superseded_by",
        ),
        "heads": ("namespace", "key", "memory_id"),
        "supports": ("namespace", "child_id", "clause", "parent_id"),
        "events": (
            "sequence",
            "namespace",
            "memory_id",
            "state",
            "reason",
            "caused_by",
            "replacement",
            "recorded_at",
        ),
    }
    for table, columns in expected.items():
        actual = tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})"))
        if actual != columns:
            raise SchemaError(f"Invalid Revisyn table layout: {table}.")
    return version, info
