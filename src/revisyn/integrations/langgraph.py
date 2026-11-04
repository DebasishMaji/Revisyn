"""Guarded, application-scoped memory retrieval for async LangGraph tools."""

from __future__ import annotations

import json
from collections.abc import Collection

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field
from pydantic import JsonValue as SchemaValue

from revisyn.errors import MemoryNotFound, UnsupportedMemory
from revisyn.models import JSONValue, text
from revisyn.store import MemoryStore


def recall_tool(
    store: MemoryStore,
    *,
    keys: Collection[str],
    name: str = "recall_supported_memory",
    description: str = "Read a supported memory. A blocked or missing result contains no content.",
) -> StructuredTool:
    """Expose selected keys through recall, never through historical inspection."""
    text(name, "name")
    text(description, "description")
    if isinstance(keys, (str, bytes)) or not keys:
        raise ValueError("keys must be a nonempty collection of memory keys.")
    allowed = frozenset(text(key, "key") for key in keys)
    choices: list[SchemaValue] = [key for key in sorted(allowed)]

    class RecallArguments(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        key: str = Field(
            min_length=1,
            description="A memory key exposed by the application.",
            json_schema_extra={"enum": choices},
        )

    async def recall(**arguments: object) -> str:
        key = RecallArguments.model_validate(arguments).key
        if key not in allowed:
            raise ValueError("This key is not exposed by the memory tool.")
        try:
            memory = store.recall(key)
        except UnsupportedMemory:
            result: dict[str, JSONValue] = {"status": "blocked", "key": key}
        except MemoryNotFound:
            result = {"status": "missing", "key": key}
        else:
            result = {
                "status": "supported",
                "key": key,
                "reference": {"namespace": memory.ref.namespace, "id": memory.ref.id},
                "content": memory.content,
            }
        return json.dumps(result, sort_keys=True, allow_nan=False)

    return StructuredTool.from_function(
        coroutine=recall,
        name=name,
        description=description,
        # Preserve field schema extras when LangChain exports the tool to a model.
        args_schema=RecallArguments.model_json_schema(),
    )
