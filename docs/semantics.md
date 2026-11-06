# Declared-dependency semantics

## Records and proofs

A memory is an immutable content revision with a namespace, random reference,
logical key, kind, provenance label, and creation sequence. Its lifecycle state
is maintained separately from its content. `Memory.content` decodes a fresh copy;
mutating that value does not change persisted content.

A `fact` is an application-supplied observation. A `derived` memory declares one
or more nonempty support sets. Every premise must name an existing revision in
the same namespace. Premises are conjunctive within a set; sets are alternatives.
Duplicate premises and duplicate alternatives are rejected.

The store does not execute the named derivation rule or establish logical
entailment between its input and output content. A false rule can create a false
but graph-supported conclusion. A fact's provenance string is an audit label,
not authenticated evidence.

## State

| State | Meaning |
|---|---|
| `supported` | A live observation, or a derived revision with at least one complete declared support set. |
| `invalidated` | A derived revision has lost every complete support set. |
| `retracted` | The application explicitly withdrew the current revision. |
| `superseded` | A newer revision replaced this logical key. |

Historical snapshots do not update in place in the caller. Fetch again to learn
the current state. Supported memories are not automatically verified statements
about the external world.

## Guarded reuse of a saved revision

`recall(key)` returns the supported current revision. If an application needs to
reuse a previously retrieved revision rather than accept any current replacement,
call `recall(key, expected_ref=saved.ref)`. Reference equality and current support
are checked in the same read transaction. Identical replacement content is still
a different revision and does not satisfy this precondition.

A missing key raises `MemoryNotFound`. A different head raises `RevisionConflict`,
even if that head is unsupported. A matching but unsupported head raises
`UnsupportedMemory`. References must be typed `MemoryRef` values in the store's
namespace. Rejection does not change content, support, or audit history.
Inspection snapshots cannot authorize reuse, with or without `expected_ref`.

This is a point-in-time read precondition, not a lease or a lock spanning later
reasoning or an external action. A concurrent writer may commit after the read's
snapshot; applications still own the consequential action boundary.

## Read related memories together

Use `recall_many` when a decision needs several current memories:

```python
owner, plan = store.recall_many(("service-owner", "deployment-plan"))
```

The method checks current support and returns memories in input order from one
read transaction. Separate `recall` calls can observe a replacement between
reads; this batch instead uses one consistent SQLite view. A concurrent writer
may commit while that view is open, so the next read can see different evidence.

All keys must be nonempty strings in a sequence, not a bare string. Duplicates
are preserved; an empty sequence returns `()`, but still requires an open live
store. The first missing or unsupported key raises `MemoryNotFound` or
`UnsupportedMemory` without returning a partial result or changing audit history.
Unexpected storage errors propagate. Read-only live stores are supported;
inspection snapshots cannot authorize batch reuse, including empty batches.

This returns current heads, not necessarily previously saved references.
Compare returned references with the versions a saved plan requires. A shared
read view does not establish that the memories agree semantically, infer omitted
dependencies, or lock evidence across a subsequent external action.
Batch size is application-controlled; there is no record-count or time budget.

## Creation and replacement

`observe` creates a fact; `derive` creates a derived memory. An existing key
requires its current `MemoryRef` in `replaces`. The kind cannot change. A stale
reference fails rather than overwriting a concurrent writer's update.

Replacement creates a new revision, changes the key head, supersedes the old
revision, propagates lost support, and records events in one write transaction.
Every alternative premise must exist, and at least one complete proof must
survive at commit. Even a derivation that was supported before supersession
cannot commit if it depends solely on the version it replaces.

New proofs only reference already existing revisions. Proof edges are immutable,
so the stored graph is acyclic. There are no negative premises, recursive rules,
or late additions of justifications to existing revisions.

Replacement never substitutes the new reference into old proofs. Even identical
replacement content is a new evidence version; automatic equivalence and
recomputation are outside the contract. Explicitly derive a new conclusion with
new evidence and `replaces=old_conclusion.ref`.

A retracted current fact can receive a new revision. That observation does not
reactivate conclusions tied to the withdrawn reference. Retraction itself is
only allowed for a supported current revision.

## Selective invalidation

The invalidator follows reverse proof edges from the withdrawn or superseded
revision. It processes candidates in creation order, which is a topological
order. A shared descendant is checked once, after its affected premises.

If any full support set survives, the node stays supported and propagation stops
along that edge. Otherwise the node becomes invalidated and its children become
candidates. Unrelated memories are not checked. Event causes identify the
initiating withdrawn revision; the explanation retains all immediate proofs.

`Change.preserved` is not every unaffected memory. It includes only nodes actually
checked and retained. State events are stored only for state changes; loss of one
alternative without a state change is inspectable through its premises' histories.

## Storage and concurrency

SQLite stores records, key heads, support memberships, and append-only state
events. Foreign keys constrain namespace-local references. The database has an
application identifier and schema version; unsupported or unrelated databases
are rejected. No schema migration is performed in this alpha.

Write methods use `BEGIN IMMEDIATE`, write-ahead logging, and `synchronous=FULL`.
SQLite serializes writers. Reference preconditions are checked inside that write
transaction. Exceptions roll back the entire mutation, including cascading
changes and events. Unexpected storage errors propagate.

The new revision's creation event points to the revision it replaces. The old
revision's supersession event points forward to the replacement. These are audit
links, not new support edges.

Each read method holds one consistent transaction. A concurrent writer cannot
make an explanation combine a new premise state with an old conclusion state.
Use a local filesystem and one `MemoryStore` connection per thread. Different
connections can access the same file; no distributed-worker contract is implied.
The store checks both process and thread ownership, including on close.
`read_only=True` opens an existing file without initialization or WAL conversion;
each read still observes a fresh per-call transaction. Valid writes through this
mode raise `ReadOnlyStore`. `require_existing=True` rejects missing or
uninitialized writable state; default construction retains deliberate provisioning.

Namespace selection is trusted application configuration, not access control.
Content and provenance are plaintext. Protect the database and sidecars and do
not open untrusted database files.

`list_memories` and `list_events` provide bounded record-count pages with
sequence cursors; the existing convenience enumeration/explanation APIs remain
unbounded. Inspection snapshots capture all namespaces through SQLite backup,
are quarantined from writable stores, and cannot authorize `recall`, even when
their historical proof graph labels a revision supported. See
[operations](operations.md) for export privacy, diagnostics, publication failures,
and why old memory state cannot safely be promoted as current evidence.

## Boundaries

The graph cannot reveal an assumption that the application did not declare.
Nor can the store establish whether a declared proof is semantically adequate,
whether a source is fresh, or whether the world changed without an observation.

Revocation is not erasure. It preserves old content for audit, does not remove
knowledge from model weights or an existing prompt, and does not invalidate
external caches. A successful read is a snapshot, not a lock across later model
reasoning or external execution.
