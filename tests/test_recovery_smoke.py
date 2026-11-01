import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/recovery_smoke.py"
SPEC = importlib.util.spec_from_file_location("recovery_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_submission_diagnostic_distinguishes_rejection_without_dumping_journal():
    run = {
        "status": "failed",
        "reason": "submission_failed",
        "tx_hash": "not-needed",
        "details": {
            "error": "TransactionException: Transaction sender has too few chi for this transaction",
            "private_key": "secret-wallet-key",
            "tx_json": '{"payload":"secret-signed-payload"}',
        },
    }
    with pytest.raises(RuntimeError) as failure:
        smoke.require_submitted(run, "independent rule did not submit")
    message = str(failure.value)
    summary = json.loads(message.split(": ", 1)[1])
    assert summary == {
        "status": "failed",
        "reason": "submission_failed",
        "error": run["details"]["error"],
    }
    assert "secret-wallet-key" not in message
    assert "secret-signed-payload" not in message


def test_submission_diagnostic_redacts_long_hex_and_accepts_success():
    key = "ab" * 32
    with pytest.raises(RuntimeError) as failure:
        smoke.require_submitted(
            {"status": "failed", "reason": "submission_failed", "details": {"error": key}},
            "follow-up failed",
        )
    assert key not in str(failure.value)
    assert "redacted" in str(failure.value)
    smoke.require_submitted({"status": "submitted"}, "must not fail")
