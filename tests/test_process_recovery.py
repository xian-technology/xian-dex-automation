"""Exercise OS lock release and SQLite durability across a killed writer."""

import multiprocessing

import pytest
from test_execution_journal import begin
from test_reconciliation import Network, recover
from test_submission_recovery import receipt

from xian_dex_automation.storage import AutomationStore


def hold_execution(path, attempted, pipe):
    store = AutomationStore(path)
    with store.execution_lock() as acquired:
        assert acquired
        pending = begin(store)
        if attempted:
            store.mark_broadcast_attempted(pending.run_id)
        pipe.send(True)
        pipe.recv()  # Parent terminates us while the execution lock is held.


@pytest.mark.parametrize("attempted", [False, True])
def test_killed_writer_releases_lock_and_preserves_submission(tmp_path, monkeypatch, attempted):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    path = tmp_path / "state.sqlite3"
    process = context.Process(target=hold_execution, args=(path, attempted, child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "writer failed to journal the transaction"
        assert parent.recv()
        store = AutomationStore(path)
        with store.execution_lock() as acquired:
            assert not acquired
        pending = store.pending_execution()
        assert pending.broadcast_attempted is attempted
    finally:
        process.terminate()
        process.join(10)
        parent.close()
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode != 0
    network = Network()
    if attempted:
        network.receipt = receipt(pending.prepared)
    else:
        network.on_broadcast = lambda tx: setattr(network, "receipt", receipt(tx))
    assert recover(AutomationStore(path), monkeypatch, network)["status"] == "submitted"
    expected = [] if attempted else [pending.prepared.transaction.json_bytes]
    assert network.broadcasts == expected
    assert len(store.list_runs()) == 1
    assert store.pending_execution() is None
