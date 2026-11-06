# Contributing

Start with a stale-memory trace and the expected external-world decision.
The declared graph is not itself the correctness oracle.

```sh
uv sync --locked --extra langgraph
uv run --extra langgraph pytest
uv run --extra langgraph ruff check .
uv run --extra langgraph ruff format --check .
uv run --extra langgraph mypy
uv run --extra langgraph python examples/memory_retraction.py
uv run --extra langgraph python examples/langgraph_memory.py
uv build
```

Proof changes should cover conjunction, alternatives, shared descendants, and
an unaffected branch. Preserve immutable evidence versions and transaction-wide
rollback. Do not silently rebind an old proof to a replacement fact.

Retrieval must not return an invalidated current revision as supported.
Audit APIs deliberately expose historical content and must stay distinguishable
from guarded retrieval. Namespace selection is application-owned, not model-owned.

Optional integrations must not enter the core import path. Exercise guarded
tools inside a compiled graph. Unsupported results must contain no historical
payload, exposed keys must be application-controlled, and unexpected store errors
must propagate rather than turning into missing memories. Keep synchronous
SQLite work in the connection's owning thread.

Never open and close an existing database file outside SQLite: POSIX close can
release other connections' locks in the same process. Provision only a closed
private file, and preserve the child-process lock-retention regression.

Keep the independent oracle and missing-dependency failure in the laboratory.
A strong recomputation baseline matching Revisyn is the expected result.
Do not relabel plan reuse as executed-agent accuracy or support-check counts as
latency improvements. Contract changes require a new laboratory version.

Contributions are licensed under Apache 2.0. Keep credentials and personal data
out of fixtures, provenance labels, and screenshots.
