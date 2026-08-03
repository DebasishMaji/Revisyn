import json
import subprocess
import sys
from dataclasses import replace
from importlib.metadata import version

import pytest

from revisyn import MemoryStore
from revisyn.lab import Policy, Scenario, _correct_in_world, run_lab, seed_deployment


@pytest.fixture(scope="module")
def report():
    return run_lab()


def row(report, scenario, policy):
    return next(
        result
        for result in report.results
        if result.scenario is scenario and result.policy is policy
    )


def test_complete_provenance_matches_the_strong_full_recomputation_baseline(report):
    selective = row(report, Scenario.COMPLETE, Policy.SELECTIVE)
    baseline = row(report, Scenario.COMPLETE, Policy.FULL_RECOMPUTATION)
    assert selective.returned == baseline.returned == ("health-check", "diagnostic-plan")
    assert selective.incorrect == baseline.incorrect == ()
    assert selective.unnecessary_blocks == baseline.unnecessary_blocks == ()
    assert selective.blocked == baseline.blocked == ("deploy-plan",)
    assert selective.support_checks == 3
    assert baseline.support_checks == 4


def test_weak_controls_show_staleness_and_over_invalidation_separately(report):
    latest = row(report, Scenario.COMPLETE, Policy.LATEST_ONLY)
    blanket = row(report, Scenario.COMPLETE, Policy.BLANKET_DESCENDANTS)
    assert latest.incorrect == ("deploy-plan",)
    assert latest.unnecessary_blocks == ()
    assert blanket.incorrect == ()
    assert blanket.unnecessary_blocks == ("health-check", "diagnostic-plan")


def test_omitted_dependency_remains_a_real_failure_for_both_proof_aware_methods(report):
    selective = row(report, Scenario.OMITTED_DEPENDENCY, Policy.SELECTIVE)
    baseline = row(report, Scenario.OMITTED_DEPENDENCY, Policy.FULL_RECOMPUTATION)
    assert selective.incorrect == baseline.incorrect == ("deploy-plan",)
    assert selective.returned == baseline.returned
    assert selective.unnecessary_blocks == baseline.unnecessary_blocks == ()


def test_reports_are_deterministic_and_do_not_claim_executed_actions(report):
    assert len(report.results) == 8
    encoded = report.as_dict()
    assert encoded == run_lab().as_dict()
    assert encoded["unit"] == "stored_plan_reuse_not_executed_actions"
    assert encoded["laboratory"] == "ownership-transfer/1"


def test_world_oracle_checks_payload_not_memory_support_flags(tmp_path):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="test") as store:
        seed = seed_deployment(store)
        assert not _correct_in_world(seed.deploy, owner="B")
        correct = replace(
            seed.deploy,
            content_json=json.dumps(
                {"action": "deploy", "service": "orders", "approval_team": "B"}
            ),
        )
        assert _correct_in_world(correct, owner="B")
        wrong_resource = replace(
            correct,
            content_json=json.dumps(
                {"action": "deploy", "service": "billing", "approval_team": "B"}
            ),
        )
        assert not _correct_in_world(wrong_resource, owner="B")


def test_oracle_scores_the_actual_returned_payload_not_the_seeded_plan(monkeypatch):
    original = MemoryStore.recall

    def altered(store, key):
        memory = original(store, key)
        if key == "health-check":
            return replace(memory, content_json='{"action":"check","path":"/wrong"}')
        return memory

    monkeypatch.setattr(MemoryStore, "recall", altered)
    result = row(run_lab(), Scenario.COMPLETE, Policy.SELECTIVE)
    assert result.incorrect == ("health-check",)


def test_cli_outputs_json_and_installed_entrypoint_matches():
    module = subprocess.run(
        [sys.executable, "-m", "revisyn", "lab"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert module.returncode == 0, module.stderr
    assert module.stderr == ""
    assert json.loads(module.stdout) == run_lab().as_dict()
    installed = subprocess.run(
        ["revisyn", "--version"], capture_output=True, text=True, timeout=10, check=False
    )
    assert installed.returncode == 0
    assert installed.stdout == f"revisyn {version('revisyn')}\n"


@pytest.mark.parametrize("arguments", [[], ["inspect"], ["lab", "--target", "production"]])
def test_cli_argument_errors_are_nonzero(arguments):
    result = subprocess.run(
        [sys.executable, "-m", "revisyn", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr
