# Plan-reuse laboratory: `ownership-transfer/1`

This is a fixed, model-free comparison, not a statistical benchmark, a latency
benchmark, or evidence of a new truth-maintenance algorithm.

## The change

The synthetic service initially belongs to team A. The graph records:

```text
owner A AND team A runbook -> approval route A -> deployment plan A

(owner A AND team A runbook) OR public API contract
    -> health check -> diagnostic plan
```

The authoritative simulated world then changes ownership to B. The catalog
observation is replaced with a new evidence revision. The public health endpoint
does not change.

All four policies see the same immutable graph, the same changed source states,
the same stored plan content, and the same evaluation point. No policy receives
a freshly regenerated plan during this comparison.

## Policies

| Policy | Decision |
|---|---|
| `latest_only` | Reuse the latest stored plan; updating a source does not reconsider it. |
| `blanket_descendants` | Block everything reachable from the withdrawn source, ignoring alternative proofs. |
| `full_recomputation` | Recompute every derived memory from the declared graph in topological order, ignoring cached invalidation flags. |
| `revisyn_selective` | Use the actual store mutation and its guarded `recall` API. |

The first two are intentionally limited controls, not stand-ins for named
production memory systems. Full recomputation is the correctness reference for
the declared positive graph. Revisyn is expected to agree with it, not beat it
on correctness.

## Independent oracle

The evaluator compares returned plan payloads with the simulated world's
requirements: current owner, service, operation, and health endpoint. Graph support
flags do not establish whether a plan is correct. For a blocked plan, the same
world check determines whether the block was unnecessary.

Reported keys distinguish incorrect returns from unnecessary blocks. The
comparison does not execute deployments or invoke a language model. Reuse is a
proxy decision that a later agent experiment must connect to actual tool effects.

## Expected cases

With complete dependencies, the approval route and deployment plan lose support.
The health check retains its public-contract proof, and the diagnostic plan
remains supported. Revisyn and full recomputation block only the deployment plan.

The selective mutation checks three derived memories; full recomputation checks
four. Those are support-evaluation counts, not SQL query counts, elapsed time,
memory use, or evidence of a scalability advantage.
The fixture is prepared through Revisyn once; these counters describe each
policy's support-evaluation rule, not total process work including preparation.

In `omitted_dependency`, the approval route fails to declare its ownership
premise. After transfer it remains graph-supported, so both Revisyn and full
recomputation return the stale deployment plan. The external oracle marks that
return incorrect. Blanket invalidation misses it too, while unnecessarily
blocking the health and diagnostic plans.

Do not remove this case to improve the reported result. A dependency graph cannot
invalidate an assumption it never recorded.

## Running and changing the artifact

```sh
uv run revisyn lab > comparison.json
```

The CLI emits JSON on stdout. A completed comparison exits 0 even when a control
or the incomplete-provenance scenario returns an incorrect plan. This command is
not a safety gate. Operational errors exit 1 and argument errors exit 2, with
diagnostics on stderr.

Trials use temporary databases, which are closed and removed afterward.
Reports omit random memory IDs and wall-clock timestamps.

Changes to the fixture's world, proof assumptions, policy inputs, task unit, or
oracle require a new laboratory version. A different schedule must not be
presented as the same comparison with improved results.
