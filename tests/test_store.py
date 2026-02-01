from pathlib import Path

import pytest

from revisyn import (
    InvalidContent,
    InvalidSupport,
    KeyConflict,
    MemoryNotFound,
    MemoryRef,
    MemoryStore,
    NamespaceMismatch,
    RevisionConflict,
    State,
    StoreClosed,
    UnsupportedMemory,
)


@pytest.fixture
def store(tmp_path: Path):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="deployment") as memory:
        yield memory


def fact(store: MemoryStore, key: str):
    return store.observe(key, {"value": key}, source=f"source/{key}").memory


def test_replacement_invalidates_the_transitive_plan_without_rewriting_its_proof(store):
    owner = fact(store, "owner")
    route = store.derive("route", "team-a", supports=((owner.ref,),), rule="routing/1").memory
    plan = store.derive("plan", "deploy-via-a", supports=((route.ref,),), rule="deploy/1").memory
    replacement = store.observe(
        "owner", "team-b", source="catalog/2", replaces=owner.ref, reason="Service transferred."
    )
    assert replacement.invalidated == (route.ref, plan.ref)
    assert store.recall("owner").ref == replacement.memory.ref
    assert store.get(owner.ref).state is State.SUPERSEDED
    assert store.get(owner.ref).superseded_by == replacement.memory.ref
    assert store.get(route.ref).supports == ((owner.ref,),)
    assert store.get(route.ref).content == "team-a"
    with pytest.raises(UnsupportedMemory):
        store.recall("plan")


def test_alternative_proof_survives_and_stops_propagation(store):
    first, second = fact(store, "first"), fact(store, "second")
    shared = store.derive(
        "shared", "healthz", supports=((first.ref,), (second.ref,)), rule="endpoint/1"
    ).memory
    child = store.derive("child", "diagnose", supports=((shared.ref,),), rule="diagnose/1").memory
    change = store.retract(first.ref, reason="First source withdrawn.")
    assert change.invalidated == ()
    assert change.rechecked == change.preserved == (shared.ref,)
    assert child.ref not in change.rechecked
    assert store.recall("child").state is State.SUPPORTED
    explanation = store.explain(shared.ref)
    assert explanation.satisfied_supports == ((second.ref,),)
    final = store.retract(second.ref, reason="Last source withdrawn.")
    assert final.invalidated == (shared.ref, child.ref)


def test_conjunction_needs_every_premise(store):
    first, second = fact(store, "first"), fact(store, "second")
    child = store.derive(
        "child", "both-needed", supports=((first.ref, second.ref),), rule="both/1"
    ).memory
    assert store.retract(first.ref, reason="No longer true.").invalidated == (child.ref,)


def test_a_live_alternative_can_support_a_new_derivation_with_a_dead_alternative(store):
    old, live = fact(store, "old"), fact(store, "live")
    store.retract(old.ref, reason="Withdrawn evidence.")
    derived = store.derive("choice", 1, supports=((old.ref,), (live.ref,)), rule="choice/1").memory
    assert store.recall("choice") == derived
    assert store.explain(derived.ref).satisfied_supports == ((live.ref,),)


def test_diamond_is_rechecked_once_in_topological_order(store):
    root = fact(store, "root")
    left = store.derive("left", 1, supports=((root.ref,),), rule="left/1").memory
    right = store.derive("right", 2, supports=((root.ref,),), rule="right/1").memory
    bottom = store.derive("bottom", 3, supports=((left.ref, right.ref),), rule="join/1").memory
    change = store.retract(root.ref, reason="Retract shared premise.")
    assert change.invalidated == change.rechecked == (left.ref, right.ref, bottom.ref)


def test_recomputation_requires_a_new_derived_revision(store):
    root = fact(store, "root")
    old = store.derive("summary", "old", supports=((root.ref,),), rule="summarize/1").memory
    new_root = store.observe("root", "new", source="source/2", replaces=root.ref).memory
    assert store.get(old.ref).state is State.INVALIDATED
    new = store.derive(
        "summary", "new", supports=((new_root.ref,),), rule="summarize/2", replaces=old.ref
    ).memory
    assert store.recall("summary") == new
    assert store.get(old.ref).state is State.SUPERSEDED
    assert store.get(old.ref).superseded_by == new.ref


def test_new_observation_does_not_reactivate_old_derivations(store):
    root = fact(store, "root")
    derived = store.derive("plan", "old", supports=((root.ref,),), rule="plan/1").memory
    store.retract(root.ref, reason="Source withdrawn.")
    store.observe("root", root.content, source="source/2", replaces=root.ref)
    assert store.get(derived.ref).state is State.INVALIDATED
    with pytest.raises(UnsupportedMemory):
        store.recall("plan")


def test_revision_cannot_be_justified_by_the_version_it_supersedes(store):
    root = fact(store, "root")
    summary = store.derive("summary", 1, supports=((root.ref,),), rule="summary/1").memory
    before = store.memories(include_history=True)
    with pytest.raises(UnsupportedMemory, match="survives"):
        store.derive(
            "summary", 2, supports=((summary.ref,),), rule="summary/2", replaces=summary.ref
        )
    assert store.memories(include_history=True) == before
    assert store.recall("summary") == summary


def test_invalid_derived_creation_rolls_back_head_payload_and_events(store):
    root = fact(store, "root")
    store.retract(root.ref, reason="Unavailable evidence.")
    before = store.memories(include_history=True)
    with pytest.raises(UnsupportedMemory):
        store.derive("bad", "stale", supports=((root.ref,),), rule="bad/1")
    assert store.memories(include_history=True) == before
    with pytest.raises(MemoryNotFound):
        store.recall("bad")


def test_cascade_failure_rolls_back_the_entire_replacement(store, monkeypatch):
    root = fact(store, "root")
    child = store.derive("child", 1, supports=((root.ref,),), rule="child/1").memory
    before = store.memories(include_history=True)
    events = store.history(root.ref)
    original = store._event

    def injected(ref, state, *args, **kwargs):
        original(ref, state, *args, **kwargs)
        if state is State.INVALIDATED:
            raise RuntimeError("Injected cascade failure")

    monkeypatch.setattr(store, "_event", injected)
    with pytest.raises(RuntimeError, match="Injected cascade"):
        store.observe("root", 2, source="source/2", replaces=root.ref)
    assert store.memories(include_history=True) == before
    assert store.history(root.ref) == events
    assert store.recall("child").ref == child.ref


def test_revision_preconditions_prevent_lost_updates_and_kind_changes(store):
    root = fact(store, "root")
    other = fact(store, "other")
    with pytest.raises(KeyConflict):
        fact(store, "root")
    with pytest.raises(RevisionConflict):
        store.observe("root", 2, source="v2", replaces=other.ref)
    with pytest.raises(RevisionConflict):
        store.derive("root", 2, supports=((other.ref,),), rule="v2", replaces=root.ref)
    store.observe("root", 2, source="v2", replaces=root.ref)
    with pytest.raises(RevisionConflict):
        store.observe("root", 3, source="v3", replaces=root.ref)
    with pytest.raises(RevisionConflict):
        store.retract(root.ref, reason="Stale writer.")
    assert store.recall("root").content == 2


def test_scoped_references_and_queries_do_not_cross_namespaces(tmp_path):
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path, namespace="a") as first, MemoryStore(path, namespace="b") as second:
        root = fact(first, "same-key")
        fact(second, "same-key")
        with pytest.raises(NamespaceMismatch):
            second.get(root.ref)
        with pytest.raises(NamespaceMismatch):
            second.derive("bad", 1, supports=((root.ref,),), rule="v1")
        with pytest.raises(MemoryNotFound):
            second.get(MemoryRef("b", root.ref.id))
        assert len(first.memories()) == len(second.memories()) == 1
        with pytest.raises(AttributeError):
            first.namespace = "b"


def test_references_from_another_database_do_not_alias_local_sequence_numbers(tmp_path):
    with MemoryStore(tmp_path / "a.sqlite3", namespace="same") as first:
        foreign = fact(first, "same")
    with MemoryStore(tmp_path / "b.sqlite3", namespace="same") as second:
        local = fact(second, "same")
        assert local.sequence == foreign.sequence
        with pytest.raises(MemoryNotFound):
            second.get(foreign.ref)


def test_snapshots_and_json_payloads_are_detached_from_callers(store):
    content = {"items": [1, 0.125, {"active": True}]}
    root = store.observe("json", content, source="json/1").memory
    content["items"].append("caller mutation")
    read = root.content
    read["items"].append("snapshot mutation")
    assert store.recall("json").content == {"items": [1, 0.125, {"active": True}]}


@pytest.mark.parametrize(
    "content", [float("nan"), float("inf"), {"bad": object()}, {1: "coerced"}, (1, 2), b"binary"]
)
def test_invalid_content_is_rejected_without_creating_memory(store, content):
    with pytest.raises(InvalidContent):
        store.observe("bad", content, source="input")
    assert store.memories() == ()


def test_cycles_in_content_fail_explicitly_but_shared_json_containers_are_valid(store):
    cycle = []
    cycle.append(cycle)
    with pytest.raises(InvalidContent):
        store.observe("cycle", cycle, source="input")
    shared = [1, 2]
    assert store.observe("shared", [shared, shared], source="input").memory.content == [
        [1, 2],
        [1, 2],
    ]


def test_support_sets_reject_empty_duplicate_missing_and_untyped_premises(store):
    root = fact(store, "root")
    for proofs in ((), ((),), ((root.ref, root.ref),), ((root.ref,), (root.ref,))):
        with pytest.raises(InvalidSupport):
            store.derive("bad", 1, supports=proofs, rule="input")
    with pytest.raises(TypeError):
        store.derive("bad", 1, supports=(("bare-id",),), rule="input")
    with pytest.raises(MemoryNotFound):
        store.derive("bad", 1, supports=((MemoryRef(store.namespace, "unknown"),),), rule="input")
    assert store.memories() == (root,)


def test_retraction_and_explanation_survive_reopening(tmp_path):
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path, namespace="demo") as store:
        root = fact(store, "root")
        child = store.derive("child", 1, supports=((root.ref,),), rule="child/1").memory
        store.retract(root.ref, reason="Catalog correction.")
    with MemoryStore(path, namespace="demo") as restarted:
        explanation = restarted.explain(child.ref)
        assert explanation.target.state is State.INVALIDATED
        assert explanation.satisfied_supports == ()
        assert [event.state for event in explanation.events] == [
            State.SUPPORTED,
            State.SUPPORTED,
            State.RETRACTED,
            State.INVALIDATED,
        ]
        assert explanation.events[-1].caused_by == root.ref
        assert restarted.history(root.ref)[-1].reason == "Catalog correction."
        with pytest.raises(UnsupportedMemory):
            restarted.recall("child")


def test_closed_stores_and_missing_entries_fail_explicitly(store):
    with pytest.raises(MemoryNotFound):
        store.history(MemoryRef(store.namespace, "missing"))
    with pytest.raises(MemoryNotFound):
        store.recall("missing")
    store.close()
    store.close()
    with pytest.raises(StoreClosed):
        store.memories()
