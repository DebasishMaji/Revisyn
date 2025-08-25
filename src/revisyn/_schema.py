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
