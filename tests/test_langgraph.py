import asyncio
import json
import runpy
from functools import partial
from pathlib import Path

import pytest

from revisyn import MemoryStore, SnapshotUsageError, StoreClosed, snapshot_database

prebuilt = pytest.importorskip("langgraph.prebuilt")
tool_nodes = pytest.importorskip("langgraph.prebuilt.tool_node")
graphs = pytest.importorskip("langgraph.graph")
messages = pytest.importorskip("langchain_core.messages")
integration = pytest.importorskip("revisyn.integrations.langgraph")
pydantic = pytest.importorskip("pydantic")
function_calling = pytest.importorskip("langchain_core.utils.function_calling")
checkpoints = pytest.importorskip("langgraph.checkpoint.memory")


def invoke(tool, arguments):
    async def execute():
        builder = graphs.StateGraph(graphs.MessagesState)
        builder.add_node("tools", prebuilt.ToolNode([tool], handle_tool_errors=False))
        builder.add_edge(graphs.START, "tools")
        builder.add_edge("tools", graphs.END)
        result = await builder.compile().ainvoke(
            {
                "messages": [
                    messages.AIMessage(
                        content="",
                        tool_calls=[{"id": "call", "name": tool.name, "args": arguments}],
                    )
                ]
            }
        )
        message = result["messages"][-1]
        assert isinstance(message, messages.ToolMessage)
        assert message.status == "success"
        return json.loads(message.content)

    return asyncio.run(execute())


def test_real_tool_node_blocks_stale_content_after_an_evidence_change(tmp_path: Path):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        owner = store.observe("owner", "A", source="catalog/1").memory
        plan = store.derive(
            "plan", {"approval_team": "A"}, supports=((owner.ref,),), rule="route/1"
        ).memory
        tool = integration.recall_tool(store, keys=("plan",))
        first = invoke(tool, {"key": "plan"})
        assert first == {
            "status": "supported",
            "key": "plan",
            "reference": {"namespace": "orders", "id": plan.ref.id},
            "content": {"approval_team": "A"},
        }
        store.observe("owner", "B", source="catalog/2", replaces=owner.ref)
        assert invoke(tool, {"key": "plan"}) == {"status": "blocked", "key": "plan"}
        assert store.get(plan.ref).content == {"approval_team": "A"}


def test_retracted_content_is_not_exposed_and_missing_keys_are_explicit(tmp_path: Path):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        fact = store.observe("fact", "withdrawn content", source="source/1").memory
        tool = integration.recall_tool(store, keys=("fact", "missing"))
        store.retract(fact.ref, reason="Source withdrawn.")
        assert invoke(tool, {"key": "fact"}) == {"status": "blocked", "key": "fact"}
        assert invoke(tool, {"key": "missing"}) == {"status": "missing", "key": "missing"}


def test_read_only_live_tool_is_fresh_but_snapshot_tool_cannot_authorize_reuse(tmp_path: Path):
    path, archive = tmp_path / "live.sqlite3", tmp_path / "archive.sqlite3"
    with MemoryStore(path, namespace="orders") as writer:
        owner = writer.observe("owner", "A", source="catalog").memory
        writer.derive("plan", "private plan A", supports=((owner.ref,),), rule="plan/1")
        snapshot_database(path, archive)
        with MemoryStore(path, namespace="orders", read_only=True) as reader:
            tool = integration.recall_tool(reader, keys=("plan",))
            assert invoke(tool, {"key": "plan"})["content"] == "private plan A"
            writer.observe("owner", "B", source="catalog/2", replaces=owner.ref)
            assert invoke(tool, {"key": "plan"}) == {"key": "plan", "status": "blocked"}
        with MemoryStore(archive, namespace="orders", read_only=True) as audit:
            tool = integration.recall_tool(audit, keys=("plan",))
            with pytest.raises(SnapshotUsageError):
                invoke(tool, {"key": "plan"})


def test_exposed_keys_are_snapshotted_and_namespace_is_application_owned(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path, namespace="other") as other:
        other.observe("public", "other namespace", source="other/1")
    with MemoryStore(path, namespace="orders") as store:
        store.observe("public", "allowed", source="source/1")
        store.observe("private", "not exposed", source="source/1")
        keys = ["public"]
        tool = integration.recall_tool(store, keys=keys)
        keys.append("private")
        schema = function_calling.convert_to_openai_tool(tool)["function"]["parameters"]
        assert schema["properties"]["key"]["enum"] == ["public"]
        assert invoke(tool, {"key": "public"})["content"] == "allowed"
        with pytest.raises(ValueError, match="not exposed"):
            invoke(tool, {"key": "private"})
        with pytest.raises(tool_nodes.ToolInvocationError) as error:
            invoke(tool, {"key": "public", "namespace": "other"})
        assert isinstance(error.value.__cause__, pydantic.ValidationError)


@pytest.mark.parametrize("arguments", [{"key": 1}, {"key": True}, {"key": ""}, {}])
def test_invalid_tool_arguments_fail_explicitly(tmp_path: Path, arguments):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        tool = integration.recall_tool(store, keys=("plan",))
        with pytest.raises(tool_nodes.ToolInvocationError) as error:
            invoke(tool, arguments)
        assert isinstance(error.value.__cause__, pydantic.ValidationError)


@pytest.mark.parametrize("keys", [(), [], "plan", ("",), (" ",)])
def test_invalid_exposure_configuration_is_rejected(tmp_path: Path, keys):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        with pytest.raises(ValueError):
            integration.recall_tool(store, keys=keys)


def test_closed_store_is_not_converted_to_a_missing_memory(tmp_path: Path):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        tool = integration.recall_tool(store, keys=("plan",))
    with pytest.raises(StoreClosed):
        invoke(tool, {"key": "plan"})


def test_parallel_tool_calls_keep_sqlite_in_the_owning_thread(tmp_path: Path):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        for key in ("first", "second"):
            store.observe(key, key, source="source/1")
        tool = integration.recall_tool(store, keys=("first", "second"))

        async def execute():
            builder = graphs.StateGraph(graphs.MessagesState)
            builder.add_node("tools", prebuilt.ToolNode([tool], handle_tool_errors=False))
            builder.add_edge(graphs.START, "tools")
            builder.add_edge("tools", graphs.END)
            return await builder.compile().ainvoke(
                {
                    "messages": [
                        messages.AIMessage(
                            content="",
                            tool_calls=[
                                {"id": key, "name": tool.name, "args": {"key": key}}
                                for key in ("first", "second")
                            ],
                        )
                    ]
                }
            )

        result = asyncio.run(execute())
        payloads = [
            json.loads(message.content)
            for message in result["messages"]
            if isinstance(message, messages.ToolMessage)
        ]
        assert [payload["content"] for payload in payloads] == ["first", "second"]
        assert all(payload["status"] == "supported" for payload in payloads)


def test_queued_checkpoint_retrieval_uses_evidence_at_resume_time(tmp_path: Path):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        owner = store.observe("owner", "A", source="catalog/1").memory
        store.derive("plan", "private plan A", supports=((owner.ref,),), rule="plan/1")
        tool = integration.recall_tool(store, keys=("plan",))
        builder = graphs.StateGraph(graphs.MessagesState)
        builder.add_node("tools", prebuilt.ToolNode([tool], handle_tool_errors=False))
        builder.add_edge(graphs.START, "tools")
        builder.add_edge("tools", graphs.END)
        graph = builder.compile(
            checkpointer=checkpoints.InMemorySaver(), interrupt_before=["tools"]
        )
        config = {"configurable": {"thread_id": "queued-retrieval"}}

        async def execute():
            paused = await graph.ainvoke(
                {
                    "messages": [
                        messages.AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "queued-call",
                                    "name": tool.name,
                                    "args": {"key": "plan"},
                                }
                            ],
                        )
                    ]
                },
                config,
            )
            assert not any(isinstance(m, messages.ToolMessage) for m in paused["messages"])
            store.observe("owner", "B", source="catalog/2", replaces=owner.ref)
            return await graph.ainvoke(None, config)

        resumed = asyncio.run(execute())
        response = resumed["messages"][-1]
        assert isinstance(response, messages.ToolMessage)
        assert response.tool_call_id == "queued-call"
        assert json.loads(response.content) == {"status": "blocked", "key": "plan"}
        assert "private plan A" not in response.content


@pytest.mark.parametrize("change", ["none", "replacement", "invalidation"])
def test_example_checkpoint_gate_requires_the_same_supported_revision(tmp_path: Path, change):
    example = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples" / "langgraph_memory.py")
    )
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        owner = store.observe("owner", "A", source="catalog/1").memory
        plan = store.derive(
            "plan", "private plan A", supports=((owner.ref,),), rule="plan/1"
        ).memory
        tool = integration.recall_tool(store, keys=("plan",))
        builder = graphs.StateGraph(example["RecallState"])
        builder.add_node("tools", prebuilt.ToolNode([tool], handle_tool_errors=False))
        builder.add_node("gate", partial(example["gate_reuse"], store=store))
        builder.add_edge(graphs.START, "tools")
        builder.add_edge("tools", "gate")
        builder.add_edge("gate", graphs.END)
        graph = builder.compile(checkpointer=checkpoints.InMemorySaver(), interrupt_after=["tools"])
        config = {"configurable": {"thread_id": "completed-retrieval"}}

        async def execute():
            paused = await graph.ainvoke(
                {"messages": [example["request"]("plan", "saved")], "reuse_allowed": False},
                config,
            )
            response = json.loads(paused["messages"][-1].content)
            assert response["status"] == "supported"
            assert response["reference"]["id"] == plan.ref.id
            if change == "replacement":
                store.derive(
                    "plan",
                    "private plan B",
                    supports=((owner.ref,),),
                    rule="plan/2",
                    replaces=plan.ref,
                )
                assert store.recall("plan").content == "private plan B"
            elif change == "invalidation":
                store.retract(owner.ref, reason="Evidence withdrawn.")
            return await graph.ainvoke(None, config)

        resumed = asyncio.run(execute())
        assert resumed["reuse_allowed"] is (change == "none")
        assert json.loads(resumed["messages"][-1].content)["content"] == "private plan A"


def test_synchronous_invocation_is_explicitly_unsupported(tmp_path: Path):
    with MemoryStore(tmp_path / "memory.sqlite3", namespace="orders") as store:
        tool = integration.recall_tool(store, keys=("plan",))
        with pytest.raises(NotImplementedError):
            tool.invoke({"key": "plan"})
