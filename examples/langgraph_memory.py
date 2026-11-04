"""Run with: uv run --extra langgraph python examples/langgraph_memory.py"""

import asyncio
import json
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from revisyn import MemoryNotFound, MemoryStore, UnsupportedMemory
from revisyn.integrations.langgraph import recall_tool
from revisyn.lab import seed_deployment


class RecallState(MessagesState):
    reuse_allowed: bool


async def gate_reuse(state: RecallState, *, store: MemoryStore) -> dict[str, bool]:
    message = state["messages"][-1]
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        raise TypeError("The reuse gate requires the latest retrieval result.")
    result = json.loads(message.content)
    if result["status"] != "supported":
        return {"reuse_allowed": False}
    try:
        current = store.recall(result["key"])
    except (MemoryNotFound, UnsupportedMemory):
        return {"reuse_allowed": False}
    return {
        "reuse_allowed": result["reference"]
        == {
            "namespace": current.ref.namespace,
            "id": current.ref.id,
        }
    }


def request(key: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "recall_supported_memory", "args": {"key": key}}],
    )


async def demonstrate(directory: Path) -> None:
    with MemoryStore(directory / "memory.sqlite3", namespace="orders") as store:
        seed = seed_deployment(store)
        tool = recall_tool(store, keys=("deploy-plan", "health-check"))
        builder = StateGraph(RecallState)
        builder.add_node("tools", ToolNode([tool], handle_tool_errors=False))
        builder.add_node("gate_reuse", partial(gate_reuse, store=store))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", "gate_reuse")
        builder.add_edge("gate_reuse", END)
        graph = builder.compile()
        before = await graph.ainvoke(
            {"messages": [request("deploy-plan", "before")], "reuse_allowed": False}
        )
        if not before["reuse_allowed"]:
            raise RuntimeError("The initial supported plan was unexpectedly blocked.")
        print("Before ownership change: reuse allowed")
        paused_graph = builder.compile(checkpointer=InMemorySaver(), interrupt_after=["tools"])
        config: RunnableConfig = {"configurable": {"thread_id": "paused-retrieval"}}
        paused = await paused_graph.ainvoke(
            {"messages": [request("deploy-plan", "paused")], "reuse_allowed": False},
            config,
        )
        paused_message = paused["messages"][-1]
        if not isinstance(paused_message.content, str):
            raise TypeError("Expected the checkpointed retrieval response.")
        if json.loads(paused_message.content)["status"] != "supported":
            raise RuntimeError("The paused graph did not capture a supported result.")
        store.observe(
            "service-owner",
            {"team": "B"},
            source="catalog/2",
            replaces=seed.owner.ref,
            reason="Ownership transferred.",
        )
        resumed = await paused_graph.ainvoke(None, config)
        if resumed["reuse_allowed"]:
            raise RuntimeError("A checkpointed response bypassed the current-evidence gate.")
        print("Checkpoint resumed after ownership change: reuse blocked by a fresh check")
        after = await graph.ainvoke(
            {
                "messages": [*before["messages"], request("deploy-plan", "after")],
                "reuse_allowed": False,
            }
        )
        if after["reuse_allowed"]:
            raise RuntimeError("The stale plan passed the fresh-retrieval gate.")
        print(f"After ownership change: {after['messages'][-1].content}")
        print("Old conversation content is still present; fresh retrieval blocks reuse.")
        health = await graph.ainvoke(
            {"messages": [request("health-check", "health")], "reuse_allowed": False}
        )
        if not health["reuse_allowed"]:
            raise RuntimeError("The independently supported health check was blocked.")
        print("Independent health check: reuse allowed")
        print("Real LangGraph execution; scripted tool choices; no model or deployed action.")
        print("Retrieval is not a lock across later reasoning or external effects.")


async def main() -> None:
    with TemporaryDirectory(prefix="revisyn-langgraph-") as directory:
        await demonstrate(Path(directory))


if __name__ == "__main__":
    asyncio.run(main())
