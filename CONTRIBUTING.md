# Contributing

Start with a stale-memory trace and the expected external-world decision.
The declared graph is not itself the correctness oracle.

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python examples/memory_retraction.py
uv build
```

Proof changes should cover conjunction, alternatives, shared descendants, and
an unaffected branch. Preserve immutable evidence versions and transaction-wide
rollback. Do not silently rebind an old proof to a replacement fact.

Retrieval must not return an invalidated current revision as supported.
Audit APIs deliberately expose historical content and must stay distinguishable
from guarded retrieval. Namespace selection is application-owned, not model-owned.

Keep the independent oracle and missing-dependency failure in the laboratory.
A strong recomputation baseline matching Revisyn is the expected result.
Do not relabel plan reuse as executed-agent accuracy or support-check counts as
latency improvements. Contract changes require a new laboratory version.

Contributions are licensed under Apache 2.0. Keep credentials and personal data
out of fixtures, provenance labels, and screenshots.
