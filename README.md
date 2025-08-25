# Revisyn

Changing a fact should change which memories an agent is allowed to reuse.

Revisyn tracks explicit dependencies between source evidence, derived memories,
and reusable plans. Replacing or retracting a premise invalidates conclusions
that have lost every declared proof. A conclusion with another complete proof
stays supported.

**Research alpha.** Python 3.11+, macOS and Linux. Standard library only at runtime.
This is a declared-dependency memory backend, not a vector database, an agent
framework, or a model-unlearning system.

## The failure

An agent learns that team A owns a service, derives an escalation procedure, and
caches a deployment plan. Ownership changes to team B. Updating the ownership
record alone does not retire the procedure or plan.

Revisyn binds those derivations to immutable evidence versions. A new owner does
not silently become a justification for a plan written for the old owner.

## A small API

```sh
uv sync --locked
```

```python
from revisyn import MemoryStore, UnsupportedMemory

with MemoryStore("memory.sqlite3", namespace="orders-agent") as memory:
    owner = memory.observe(
        "service-owner", {"team": "A"}, source="service-catalog/revision-1"
    ).memory
    plan = memory.derive(
        "deployment-plan",
        {"request_approval_from": "A"},
        supports=((owner.ref,),),
        rule="deployment-routing/1",
    ).memory

    change = memory.observe(
        "service-owner",
        {"team": "B"},
        source="service-catalog/revision-2",
        replaces=owner.ref,
        reason="Ownership transferred.",
    )
    assert plan.ref in change.invalidated

    try:
        memory.recall("deployment-plan")
    except UnsupportedMemory:
        print("The old plan needs a new derivation, not another retrieval.")

    explanation = memory.explain(plan.ref)
    print(explanation.target.state)
```

The example expects a new database or unused keys. Existing keys require the
current reference in `replaces`; stale replacements raise `RevisionConflict`.

## Conjunction and alternatives

```python
supports = ((catalog.ref, runbook.ref), (public_contract.ref,))
```

This means **(catalog AND runbook) OR public contract**. Revoking the catalog does
not invalidate the conclusion if the public contract still supports it. Descendants
of an unchanged conclusion are not reconsidered.

`Change.rechecked` lists support checks, `invalidated` lists newly invalidated
descendants, and `preserved` lists checked conclusions retaining a complete proof.
These are semantic work counters, not measurements of SQL queries or latency.

## Inspecting memory

`recall(key)` returns only a supported current revision and raises explicitly
otherwise. `get(ref)` is a historical/audit lookup that can return an unsupported
record. `memories()` includes current unsupported records for inspection;
`include_history=True` also includes older revisions.

`history(ref)` retains state transitions and their initiating cause.
`explain(ref)` returns a consistent snapshot of the target, all declared
ancestors, and their event histories. Evidence and derived content survive restart.

See [the semantics](docs/semantics.md) for the transaction and revision contract.

## What supported does not mean

It means supported by the **declared graph**, not verified true in the world.
Revisyn does not extract missing dependencies, prove that a derivation rule is
sound, verify a source, or infer that two keys mean the same thing.

An omitted dependency can leave a stale plan supported. Revocation cannot erase
tokens already in a model's context, invalidate an unrelated cache, cancel an
agent already acting, or undo an external effect. Applications must gate retrieval
and recheck their evidence before consequential use.

The database stores content and provenance in plaintext. New database files are
owner-only; existing permissions are not changed. Do not store credentials.
Use local storage and one connection per thread. SQLite serializes writes;
this is not a distributed memory service or an authorization boundary.

Truth maintenance and incremental view maintenance predate agent memory.
The research question is whether explicit dependencies improve downstream action
correctness under controlled changes and incomplete provenance. No algorithmic
novelty or comparative model result is claimed.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

Licensed under [Apache 2.0](LICENSE). See [NOTICE](NOTICE).