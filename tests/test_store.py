from pathlib import Path

import pytest

from revisyn import (
    MemoryStore,
    State,
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
