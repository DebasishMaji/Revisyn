"""A fixed plan-reuse experiment, with declared proofs and an independent world oracle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import UnsupportedMemory
from .models import JSONValue, Kind, Memory, MemoryRef, State
from .store import MemoryStore

LABORATORY = "ownership-transfer/1"


class Scenario(StrEnum):
    COMPLETE = "complete_provenance"
    OMITTED_DEPENDENCY = "omitted_dependency"


class Policy(StrEnum):
    LATEST_ONLY = "latest_only"
    BLANKET_DESCENDANTS = "blanket_descendants"
    FULL_RECOMPUTATION = "full_recomputation"
    SELECTIVE = "revisyn_selective"


@dataclass(frozen=True, slots=True)
class DeploymentMemory:
    owner: Memory
    route: Memory
    deploy: Memory
    health: Memory
    diagnose: Memory

    @property
    def plans(self) -> tuple[Memory, ...]:
        return self.deploy, self.health, self.diagnose


def seed_deployment(
    store: MemoryStore, *, scenario: Scenario = Scenario.COMPLETE
) -> DeploymentMemory:
    if not isinstance(scenario, Scenario):
        raise TypeError("scenario must be a Scenario value.")
    owner = store.observe("service-owner", {"team": "A"}, source="catalog/1").memory
    runbook = store.observe(
        "team-a-runbook",
        {"approval_team": "A", "health_path": "/healthz"},
        source="team-a/runbook/1",
    ).memory
    public = store.observe(
        "public-health-contract", {"path": "/healthz"}, source="public-api/1"
    ).memory
    route_supports = (
        ((owner.ref, runbook.ref),) if scenario is Scenario.COMPLETE else ((runbook.ref,),)
    )
    route = store.derive(
        "approval-route", {"team": "A"}, supports=route_supports, rule="choose-owner-team/1"
    ).memory
    deploy = store.derive(
        "deploy-plan",
        {"action": "deploy", "service": "orders", "approval_team": "A"},
        supports=((route.ref,),),
        rule="deployment-plan/1",
    ).memory
    health = store.derive(
        "health-check",
        {"action": "check", "service": "orders", "path": "/healthz"},
        supports=((owner.ref, runbook.ref), (public.ref,)),
        rule="health-endpoint/1",
    ).memory
    diagnose = store.derive(
        "diagnostic-plan",
        {"action": "diagnose", "service": "orders", "path": "/healthz"},
        supports=((health.ref,),),
        rule="diagnostic-plan/1",
    ).memory
    return DeploymentMemory(owner, route, deploy, health, diagnose)


def _correct_in_world(memory: Memory, *, owner: str) -> bool:
    expected: dict[str, JSONValue] = {
        "deploy-plan": {"action": "deploy", "service": "orders", "approval_team": owner},
        "health-check": {"action": "check", "service": "orders", "path": "/healthz"},
        "diagnostic-plan": {"action": "diagnose", "service": "orders", "path": "/healthz"},
    }
    return memory.content == expected[memory.key]


def _recompute(nodes: tuple[Memory, ...]) -> tuple[set[MemoryRef], int]:
    supported: set[MemoryRef] = set()
    checks = 0
    for memory in nodes:
        if memory.state in (State.RETRACTED, State.SUPERSEDED):
            continue
        if memory.kind is Kind.FACT:
            supported.add(memory.ref)
            continue
        checks += 1
        if any(all(parent in supported for parent in group) for group in memory.supports):
            supported.add(memory.ref)
    return supported, checks


def _descendants(nodes: tuple[Memory, ...], root: MemoryRef) -> set[MemoryRef]:
    affected = {root}
    for memory in nodes:
        if any(parent in affected for group in memory.supports for parent in group):
            affected.add(memory.ref)
    return affected


@dataclass(frozen=True, slots=True)
class Result:
    scenario: Scenario
    policy: Policy
    returned: tuple[str, ...]
    incorrect: tuple[str, ...]
    blocked: tuple[str, ...]
    unnecessary_blocks: tuple[str, ...]
    support_checks: int | None

    def as_dict(self) -> dict[str, JSONValue]:
        returned: list[JSONValue] = list(self.returned)
        incorrect: list[JSONValue] = list(self.incorrect)
        blocked: list[JSONValue] = list(self.blocked)
        unnecessary: list[JSONValue] = list(self.unnecessary_blocks)
        return {
            "scenario": self.scenario.value,
            "policy": self.policy.value,
            "returned_keys": returned,
            "incorrect_keys": incorrect,
            "blocked_keys": blocked,
            "unnecessarily_blocked_keys": unnecessary,
            "support_checks": self.support_checks,
        }


@dataclass(frozen=True, slots=True)
class LabReport:
    results: tuple[Result, ...]

    def as_dict(self) -> dict[str, JSONValue]:
        rows: list[JSONValue] = [result.as_dict() for result in self.results]
        return {
            "format_version": 1,
            "kind": "declared_dependency_laboratory",
            "laboratory": LABORATORY,
            "unit": "stored_plan_reuse_not_executed_actions",
            "results": rows,
        }


def run_lab() -> LabReport:
    results: list[Result] = []
    with TemporaryDirectory(prefix="revisyn-lab-") as directory:
        for scenario in Scenario:
            with MemoryStore(
                Path(directory) / f"{scenario.value}.sqlite3", namespace="orders"
            ) as store:
                seed = seed_deployment(store, scenario=scenario)
                world_owner = "B"
                change = store.observe(
                    "service-owner",
                    {"team": world_owner},
                    source="catalog/2",
                    replaces=seed.owner.ref,
                    reason="Ownership transferred from A to B.",
                )
                nodes = store.memories(include_history=True)
                full, full_checks = _recompute(nodes)
                blanket = _descendants(nodes, seed.owner.ref)
                for policy in Policy:
                    returned: list[str] = []
                    incorrect: list[str] = []
                    blocked: list[str] = []
                    unnecessary: list[str] = []
                    for plan in seed.plans:
                        candidate = plan
                        if policy is Policy.SELECTIVE:
                            try:
                                candidate = store.recall(plan.key)
                            except UnsupportedMemory:
                                allowed = False
                            else:
                                allowed = True
                        elif policy is Policy.FULL_RECOMPUTATION:
                            allowed = plan.ref in full
                        elif policy is Policy.BLANKET_DESCENDANTS:
                            allowed = plan.ref not in blanket
                        else:
                            allowed = True
                        correct = _correct_in_world(candidate, owner=world_owner)
                        if allowed:
                            returned.append(plan.key)
                            if not correct:
                                incorrect.append(plan.key)
                        else:
                            blocked.append(plan.key)
                            if correct:
                                unnecessary.append(plan.key)
                    checks = (
                        len(change.rechecked)
                        if policy is Policy.SELECTIVE
                        else full_checks
                        if policy is Policy.FULL_RECOMPUTATION
                        else None
                    )
                    results.append(
                        Result(
                            scenario,
                            policy,
                            tuple(returned),
                            tuple(incorrect),
                            tuple(blocked),
                            tuple(unnecessary),
                            checks,
                        )
                    )
    return LabReport(tuple(results))
