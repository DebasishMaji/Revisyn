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

Namespace selection is trusted application configuration, not access control.
Content and provenance are plaintext. Protect the database and sidecars and do
not open untrusted database files.

## Boundaries

The graph cannot reveal an assumption that the application did not declare.
Nor can the store establish whether a declared proof is semantically adequate,
whether a source is fresh, or whether the world changed without an observation.

Revocation is not erasure. It preserves old content for audit, does not remove
knowledge from model weights or an existing prompt, and does not invalidate
external caches. A successful read is a snapshot, not a lock across later model
reasoning or external execution.
