"""Run with: uv run python examples/memory_retraction.py"""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from revisyn import MemoryStore, UnsupportedMemory
from revisyn.lab import run_lab, seed_deployment


def require_blocked(store: MemoryStore, key: str) -> None:
    try:
        store.recall(key)
    except UnsupportedMemory as exc:
        print(f"Blocked reuse: {exc}")
    else:
        raise RuntimeError(f"The stale {key} was unexpectedly available.")


def demonstrate(directory: Path) -> None:
    path = directory / "memory.sqlite3"
    print("Revisyn: change the evidence, reconsider the plan.")
    print(
        "\nowner A + team A runbook -> approval route A -> deployment plan A\n"
        "owner A + team A runbook OR public API contract -> health check -> diagnostic plan"
    )
    with MemoryStore(path, namespace="orders") as store:
        seed = seed_deployment(store)
        change = store.observe(
            "service-owner",
            {"team": "B"},
            source="catalog/2",
            replaces=seed.owner.ref,
            reason="Ownership transferred from A to B.",
        )
        print("\nThe service catalog now says team B owns the service.")
        print("Invalidated:", ", ".join(store.get(ref).key for ref in change.invalidated))
        print(
            f"Support checks: {len(change.rechecked)}; "
            f"checked memories retaining another proof: {len(change.preserved)}"
        )
        require_blocked(store, "deploy-plan")
        health = store.recall("health-check")
        explanation = store.explain(health.ref)
        proof = explanation.satisfied_supports
        print(
            "Health check remains supported by:",
            ", ".join(store.get(ref).key for group in proof for ref in group),
        )
        store.recall("diagnostic-plan")
        if seed.diagnose.ref in change.rechecked:
            raise RuntimeError("An unchanged health check should not recheck its descendants.")
        print("The downstream diagnostic plan was not rechecked.")

    with MemoryStore(path, namespace="orders") as restarted:
        print("\nReopen the store: the old deployment plan must still be blocked.")
        require_blocked(restarted, "deploy-plan")
        owner = restarted.recall("service-owner")
        runbook = restarted.observe(
            "team-b-runbook", {"approval_team": "B"}, source="team-b/runbook/1"
        ).memory
        route = restarted.derive(
            "approval-route",
            {"team": "B"},
            supports=((owner.ref, runbook.ref),),
            rule="choose-owner-team/1",
            replaces=seed.route.ref,
            reason="Recomputed using the new owner's evidence.",
        ).memory
        restarted.derive(
            "deploy-plan",
            {"action": "deploy", "service": "orders", "approval_team": "B"},
            supports=((route.ref,),),
            rule="deployment-plan/1",
            replaces=seed.deploy.ref,
            reason="Explicitly rebuilt the deployment plan.",
        )
        print("A separate, explicit derivation creates:", restarted.recall("deploy-plan").content)

    print("\nFixed plan-reuse laboratory (not a model benchmark):")
    print(f"{'provenance':<20} {'policy':<23} {'incorrect':>9} {'false blocks':>12} {'checks':>7}")
    for row in run_lab().results:
        checks = str(row.support_checks) if row.support_checks is not None else "-"
        print(
            f"{row.scenario.value:<20} {row.policy.value:<23} {len(row.incorrect):>9} "
            f"{len(row.unnecessary_blocks):>12} {checks:>7}"
        )
    print(
        "\nThe missing-edge case stays wrong even with full proof recomputation.\n"
        "Supported means supported by declared evidence, not proven true in the world."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, help="retain the demo in a new directory")
    arguments = parser.parse_args()
    if arguments.directory is not None:
        try:
            arguments.directory.mkdir(mode=0o700)
        except OSError as exc:
            parser.error(f"Cannot create demo directory: {exc}")
        demonstrate(arguments.directory)
        print(f"\nMemory retained in {arguments.directory}")
    else:
        with TemporaryDirectory(prefix="revisyn-demo-") as directory:
            demonstrate(Path(directory))


if __name__ == "__main__":
    main()
