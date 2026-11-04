# Guarded memory tools in LangGraph

`revisyn.integrations.langgraph.recall_tool` exposes selected memory keys as an
async LangChain `StructuredTool`. Every call uses `MemoryStore.recall`: historical
inspection is not available through this tool.

## Install and run

From the checkout:

```sh
uv sync --locked --extra langgraph
uv run --extra langgraph python examples/langgraph_memory.py
```

For an existing local Python environment, `python -m pip install '.[langgraph]'`
installs the checkout with the extra. No public package release is implied.
Core imports do not load LangGraph, LangChain, or Pydantic.

The example uses a real compiled graph with scripted tool choices, not a model.
It retrieves a supported deployment plan, updates its ownership evidence, and
retrieves again while retaining the previous messages. The fresh-retrieval gate
blocks the old plan and permits a health check with an independent proof.

## Choose the retrieval surface in application code

Inside the store's owning async session:

```python
from langgraph.prebuilt import ToolNode
from revisyn.integrations.langgraph import recall_tool

tool = recall_tool(
    store,
    keys=("deployment-plan", "health-check"),
)
tools = ToolNode([tool], handle_tool_errors=False)
```

`store` is an application-opened `MemoryStore`, with an application-selected
namespace. The exposed-key collection is copied at binding time. Mutating the
original collection does not expose additional memories.
The model-facing argument schema lists these keys as an enum, so the model does
not have to guess the available names. Runtime membership is checked separately.

Add `tools` to a compiled graph and use `ainvoke`; the
[complete example](../examples/langgraph_memory.py) also includes the reuse gate.
A bare `ToolNode.ainvoke` can lack runtime context provided by the graph.

The default name is `recall_supported_memory`. Its only argument is a strict,
nonempty string `key`. Unknown fields, namespace overrides, and undeclared keys
are rejected. This narrows the tool surface; it is not user authentication or
an authorization service.

## Response contract

The tool returns a JSON string:

| `status` | Fields | Meaning |
|---|---|---|
| `supported` | `key`, `reference: {namespace, id}`, `content` | The current revision was supported when recalled. |
| `blocked` | `key` | The current revision is unsupported. No historical content is returned. |
| `missing` | `key` | The exposed key does not exist in this namespace. |

Expected unsupported and missing outcomes are explicit, not empty successful
retrievals. Closed-store, schema, SQLite, and other unexpected errors propagate.
With `handle_tool_errors=False`, LangGraph may wrap argument-validation errors
in `ToolInvocationError`, preserving the validation error as the cause.

A framework-level `ToolMessage.status == "success"` only means the tool completed
its retrieval decision. It does not mean the requested memory is supported.
Read the JSON `status` and fail closed at the application's reuse gate.
Supported responses contain memory content. Application-configured tracing or
callbacks may export that content; the binding does not manage or redact those
exports.

## What this does not solve

Keep the store open while its graph runs and use it in the connection's owning
thread. The tool is async-only; synchronous `invoke` is unsupported. The SQLite
read runs synchronously in that thread, rather than moving the connection into
a worker. This is a local integration, not an asynchronous database service.

Support is still relative to declared dependencies. The tool does not discover
omitted premises, authenticate sources, or prove a derivation sound.

It also cannot remove an earlier response from the model's context. A model
could ignore a blocked result and repeat old content. The example's
application-owned gate demonstrates one controlled reuse path, not a general
guarantee over all subsequent reasoning or tool calls.

A supported reference can become invalid after retrieval. There is no lock
across later model reasoning or external effects; recheck evidence before
consequential use and enforce the action boundary separately.

## Resume checkpoints without trusting old retrieval results

A checkpoint can contain either a queued tool call or an already completed,
formerly supported response. A queued call uses current evidence when resumed,
but replaying an old response does not itself call `recall` again.

The example's application-owned reuse gate therefore recalls the key again and
requires the supported current revision to match the response's exact reference.
It also pauses a compiled graph after retrieval, changes the ownership evidence,
and resumes: the old `supported` message remains, but reuse is blocked. A newly
supported replacement must be retrieved explicitly rather than silently
substituted into the old response.

The in-memory checkpointer demonstrates pause/resume in one process, not durable
crash recovery. Configure durable checkpoint storage separately in an application.
Even the fresh gate is not an atomic lock spanning a subsequent external action.
