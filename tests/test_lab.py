import pytest

from revisyn.lab import Policy, Scenario, run_lab


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
