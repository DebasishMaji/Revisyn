import subprocess
import sys

import pytest

from revisyn import MemoryStore, State, UnsupportedMemory


@pytest.mark.parametrize("crash_point", ["during_cascade", "after_commit"])
def test_process_exit_never_exposes_a_partial_replacement(tmp_path, crash_point):
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path, namespace="crash") as store:
        root = store.observe("root", "old", source="source/1").memory
        child = store.derive("child", "old", supports=((root.ref,),), rule="child/1").memory
        final = store.derive("final", "old", supports=((child.ref,),), rule="final/1").memory
    script = """
import os
import sys
from revisyn import MemoryStore, State

store = MemoryStore(sys.argv[1], namespace="crash")
store._connection.execute("PRAGMA cache_size = 1")
original = store._event

def interrupted(ref, state, *args, **kwargs):
    original(ref, state, *args, **kwargs)
    if state is State.INVALIDATED and sys.argv[2] == "during_cascade":
        os._exit(23)

store._event = interrupted
old = store.recall("root")
store.observe("root", "new", source="source/2", replaces=old.ref)
os._exit(24)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), crash_point],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    expected_exit = 23 if crash_point == "during_cascade" else 24
    assert result.returncode == expected_exit, result.stderr
    with MemoryStore(path, namespace="crash") as recovered:
        if crash_point == "during_cascade":
            assert recovered.recall("root") == root
            assert recovered.recall("child") == child
            assert recovered.recall("final") == final
            assert len(recovered.memories(include_history=True)) == 3
            assert len(recovered.history(root.ref)) == 1
        else:
            assert recovered.recall("root").content == "new"
            assert recovered.get(root.ref).state is State.SUPERSEDED
            assert recovered.get(child.ref).state is State.INVALIDATED
            assert recovered.get(final.ref).state is State.INVALIDATED
            assert len(recovered.memories(include_history=True)) == 4
            with pytest.raises(UnsupportedMemory):
                recovered.recall("final")
