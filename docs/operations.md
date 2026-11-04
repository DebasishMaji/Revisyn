# Embedded-worker operations

This is a local-storage operational baseline for a research-alpha SDK, not a
distributed memory service, compliance certification, or automatic recovery
system.

## Provisioning, ownership, and fail-closed startup

Provision a new store deliberately before admitting worker traffic:

```python
from revisyn import MemoryStore

with MemoryStore("/srv/agent/memory.sqlite3", namespace="orders"):
    pass
```

Workers should use `MemoryStore(path, namespace="orders", require_existing=True)`.
Missing or uninitialized state then fails rather than becoming a new empty
memory store. Default construction remains available for explicit first-time
provisioning and disposable examples. Do not put provisioning in an automatic
fallback after a worker-open error.

`require_existing` is not authentication of a database or mounted volume. A
different valid live file can still contain the wrong or incomplete history.
Deployment owners must keep the correct live volume authoritative.

Create each connection in its owning process/thread, after fork. Reads, writes,
and close reject ownership misuse explicitly. Separate connections may share
the same trusted local SQLite file; writes remain serialized through
`BEGIN IMMEDIATE`, WAL, and `synchronous=FULL`. This is not distributed
coordination. Do not use network shares, synchronize live files with file-sync
tools, swap files under open connections, or mutate tables outside the SDK.
Do not alias live SQLite files with hard links; their WAL paths can differ.
Capture rejects hard-linked sources and conservatively reserves case-folded
source/sidecar destinations on both case-sensitive and case-insensitive filesystems.

## Read-only access and bounded operator pages

```python
with MemoryStore("memory.sqlite3", namespace="orders", read_only=True) as reader:
    plan = reader.recall("deploy-plan")
    page = reader.list_memories(limit=100)
    audit = reader.inspect_current("deploy-plan")
```

Read-only mode opens an existing file without initialization, migration, or WAL
conversion. Mutations raise `ReadOnlyStore`; SQLite also enforces query-only
access. Each read call takes a **fresh, consistent transaction**, so a long-lived
read-only retrieval tool sees evidence committed before its next call. It does
not hold a lifetime-stale read view. No read locks evidence across a later action.

`inspect_current`, `get`, and explanations are audit APIs, including unsupported
content. Use `recall` for operational reuse of a live store; never replace it
with audit retrieval.

```sh
revisyn list memory.sqlite3 --namespace orders --limit 100
revisyn inspect memory.sqlite3 --namespace orders --key deploy-plan
revisyn history memory.sqlite3 --namespace orders --id REVISION_ID --limit 100
revisyn status memory.sqlite3 --namespace orders --timeout 5
```

`list` returns heads by default; `--include-history` includes older revisions.
Use `next_sequence` as the next `--after-sequence`; history selects a fixed
revision ID. Page limits are integers from 1 through 1000 and cursors are
nonnegative SQLite integers. Each page is consistent, but separate pages can see
later commits; there is no multi-page frozen export session.

Lists and inspection omit content unless `--include-payload` is supplied.
Keys, provenance, support references, and history reasons are still sensitive.
Page bounds limit **record count**, not payload/graph size or total query cost.
The old `memories`, `history`, and `explain` convenience APIs remain unbounded.
Enforce application payload/graph budgets and storage quotas independently.

`status` checks SQLite structure/foreign keys across the database and reports
state/event counts for the selected namespace. Namespace selection is trusted
configuration, not authorization. A namespace with no records is allowed; there
is no namespace registration or existence guarantee. `healthy=true` does not
prove evidence fresh, a rule sound, or provenance complete.

Commands emit JSON on stdout. Operational errors exit 1 with stderr; argument
errors exit 2; failed structural checks emit `healthy=false` JSON and exit 3.
Operator commands never bootstrap a missing file.

## Full-database inspection snapshots

```sh
revisyn snapshot memory.sqlite3 --out archives/memory-incident.sqlite3 --timeout 30
revisyn status archives/memory-incident.sqlite3 --namespace orders
revisyn inspect archives/memory-incident.sqlite3 --namespace orders --key deploy-plan
```

```python
from revisyn import snapshot_database

receipt = snapshot_database("memory.sqlite3", "incident.sqlite3")
print(receipt.as_dict())
with MemoryStore("incident.sqlite3", namespace="orders", read_only=True) as audit:
    print(audit.status())
```

The destination directory must already exist and be trusted. The snapshot
contains **all namespaces, raw content, provenance, proofs, heads, and history**,
not just an operator's selected namespace. It is not an erasure, redaction, or
tenant-isolated export. Artifacts are mode `0600`; protect archive directories,
transport, and receipts with deployment-level controls.

SQLite backup captures a pinned, committed view including WAL data while live
writes continue. The resulting complete database is integrity/foreign-key
checked, tagged inspection-only, converted to standalone rollback journaling,
hashed, file-synced, and atomically published without overwriting existing files
or symlinks. Publication requires local hard-link and directory-sync support.
Long read views can retain WAL pages; monitor space during capture.

A distinct application ID/version quarantines the artifact from old and current
writable stores. The new read-only mode can audit it, but **`recall` on a
snapshot raises `SnapshotUsageError`**, even for a revision labeled supported.
That label describes the saved proof graph, not current evidence. This also
prevents a LangGraph retrieval tool from serving an archive as live authority.
Re-capturing a snapshot as fresh live evidence is rejected.

Receipts identify the artifact, creation time, source schema, captured global
event watermark, byte count, and SHA-256. The checksum is not a signature;
creation time is not proof that evidence was current then. Retain receipts
independently for transfer-integrity comparisons.

Timeouts are cooperative checks during SQLite execution, backup chunks, and
hashing, not hard interruption of source opening or blocked filesystem calls.
Failures remove private temporary files. A failure after publication, such as
directory-sync failure, can leave a complete artifact without a success receipt.
Preserve and investigate it instead of retrying with overwrite or blindly
deleting a possibly consumed artifact.

## Recovery, retention, and production acceptance

An old store can resurrect a plan whose evidence was subsequently retracted.
There is **no automatic restore or supported snapshot-promotion API**. Do not
rewrite snapshot markers to bypass quarantine, replace live state with an old
file, or copy a WAL database's main file alone.

For process failure with an intact live database, reopen the same file with
`require_existing=True` and recheck through `recall`. For missing/corrupt state,
stop operational retrieval/actions and rebuild only from authoritative sources
under application-owned validation. The library does not reconstruct omitted
evidence, revoke already-issued model context, or undo external actions.

Exercise the actual graph sizes, source-update delays, concurrency, cancellation,
disk exhaustion, volume loss, and operator procedures before production use.
Alert on storage/ownership/schema errors, unexpected empty state, disk/WAL growth,
and snapshot failures. Protect plaintext databases and sidecars with access
control and encryption at rest. Define archive retention and audit export
externally; retraction preserves history and is not data erasure. These
safeguards do not establish regulatory compliance or production adoption.
