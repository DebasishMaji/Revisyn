# What would make this a research contribution

**Working question:** can explicit provenance reduce incorrect reuse of derived
agent memories after evidence changes, at comparable useful-plan availability
and resource budgets?

The initial artifact is a positive, acyclic justification-maintenance backend.
It is not a new belief-revision algorithm. Its contribution must be established
through agent-specific evaluation and usable integrations, not by renaming
established database or knowledge-representation mechanisms.

## Existing foundations

- Jon Doyle, [A Truth Maintenance System](https://doi.org/10.1016/0004-3702(79)90012-0),
  1979: explicit justifications and belief maintenance are established work.
- Gupta, Mumick, and Subrahmanian,
  [Maintaining Views Incrementally](https://doi.org/10.1145/170035.170066), 1993:
  incremental view maintenance and deletion/rederivation predate agent systems.
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/abs/2501.13956),
  2025: temporal agent memory, historical relationships, and dynamic knowledge
  integration are already studied.
- [LongMemEval](https://arxiv.org/abs/2410.10813), ICLR 2025: knowledge updates,
  temporal reasoning, and abstention are existing evaluation dimensions.

Revisyn does not implement Doyle's full system or the recursive DRed algorithm.
Nor has this repository evaluated Zep, Mem0, Letta, or any commercial memory
service. The local controls do not establish that another product fails.
Related-work coverage remains incomplete.

## What is executable now

The versioned [laboratory](laboratory.md) exposes stale plan reuse, unnecessary
invalidation, and missing dependency declarations under a known world change.
A full recomputation reference agrees with selective maintenance on the same
declared graph. Generated directed acyclic graphs also check this agreement under
successive withdrawals.

The demo preserves a plan's evidence versions across restart, explains why reuse
was blocked, retains an independently supported capability, and requires an
explicit new derivation before reuse resumes.

This is reference engineering and an experimental starting point. No paper
result, learned extraction method, model comparison, or scalability result is
claimed.

## The difficult experiment

Separate at least three sources of failure:

1. The world changed but no source update arrived.
2. The update arrived but the graph omitted a necessary premise.
3. The graph and update were adequate but maintenance or retrieval was incorrect.

Measure current-fact retrieval separately from reuse of derived summaries,
procedures, and plans. A system can retrieve the right owner while retaining the
wrong procedure.

Compare strong temporal-memory and provenance-aware baselines, full recomputation,
and competent domain-specific refresh. Use the same observed changes, source
authority, retrieval permissions, and regeneration budget. Include an existing
truth-maintenance implementation rather than treating the local weak controls
as sufficient related work.

Vary declared-edge coverage, soundness of derivation rules, alternative proofs,
change frequency, graph sharing, and consequence severity. Freeze held-out
scenarios before tuning. Include a second domain and actual sandbox tool
execution before making downstream-action claims.

Report incorrect actions, valid completions, unnecessary blocks, refresh cost,
source/derivation calls, token budgets, and latency distributions. Compare at
matched useful completion, not only matched query counts. Count unsuccessful
refreshes and failed extractions.

## Integrations and limits to resolve

An extraction adapter must record its assumptions and admit unknown provenance.
Declaring completeness is not proof that the graph is complete.
An embedding-store integration must prevent revoked vectors from being retrieved.
An agent-runtime integration must invalidate cached context or recheck revision
states before use; it cannot simply update the database and assume old prompts
have forgotten the content.

Source authentication, temporal validity intervals, recursive justifications,
conflict resolution, distributed coordination, and privacy erasure are outside
this alpha. Add them only when an experiment or independent integration shows
which contract is actually needed.
