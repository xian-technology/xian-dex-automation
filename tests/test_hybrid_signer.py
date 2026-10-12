import pytest
from xian_py.hybrid import AccountTransitionDraft
from xian_py.wallet import Wallet


@pytest.fixture
def custody(tmp_path):
    legacy = Wallet()
    draft = AccountTransitionDraft("local-test", legacy.public_key, 0)
    path = tmp_path / "active.json"
    path.write_text(draft.export_backup("active", "backup test password"))
    return legacy, draft, path


def test_service_custody_uses_original_address(custody, monkeypatch):
    from xian_dex_automation.config import AutomationConfig
    from xian_dex_automation.dex import resolve_wallet

    legacy, draft, path = custody
    monkeypatch.setenv("TEST_BACKUP_PASSWORD", "backup test password")
    config = AutomationConfig.model_validate(
        {
            "network": {"chain_id": "local-test"},
            "wallet": {"active_backup_file": path, "backup_password_env": "TEST_BACKUP_PASSWORD"},
        }
    )
    assert resolve_wallet(config).public_key == legacy.public_key
    monkeypatch.setenv(config.wallet.private_key_env, legacy.private_key)
    with pytest.raises(ValueError, match="exactly one"):
        resolve_wallet(config)
