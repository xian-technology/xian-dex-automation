from __future__ import annotations

from fastapi.testclient import TestClient
from xian_py.wallet import Wallet

from xian_dex_automation.config import load_config
from xian_dex_automation.service import create_app


def make_client(tmp_path) -> tuple[TestClient, object]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
network:
  rpc_url: "http://127.0.0.1:26657"
wallet:
  execute: false
database_path: "automation.sqlite3"
rules: []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    return TestClient(create_app(config, config_path=config_path)), config_path


def test_index_and_config_yaml(tmp_path) -> None:
    client, _config_path = make_client(tmp_path)

    response = client.get("/")
    assert response.status_code == 200
    assert "xian-dex-automation" in response.text
    assert "Generate Wallet" in response.text
    assert "Import Key" in response.text

    yaml_response = client.get("/config.yaml")
    assert yaml_response.status_code == 200
    assert "rules:" in yaml_response.text


def test_upsert_and_delete_rule_persists(tmp_path) -> None:
    client, config_path = make_client(tmp_path)
    rule = {
        "id": "r1",
        "enabled": True,
        "trigger": {
            "type": "price_move",
            "pair_id": 1,
            "direction": "either",
            "threshold_bps": 100,
            "cooldown_seconds": 300,
        },
        "action": {
            "type": "swap_exact_in",
            "src": "currency",
            "amount_in": "1",
            "max_slippage_bps": 100,
            "deadline_seconds": 300,
        },
    }

    response = client.put("/rules/r1", json=rule)
    assert response.status_code == 200
    assert load_config(config_path).rules[0].id == "r1"

    delete_response = client.delete("/rules/r1")
    assert delete_response.status_code == 200
    assert load_config(config_path).rules == []


def test_wallet_patch_persists(tmp_path) -> None:
    client, config_path = make_client(tmp_path)

    response = client.patch(
        "/wallet",
        json={"execute": True, "recipient": "abc"},
    )
    assert response.status_code == 200

    saved = load_config(config_path)
    assert saved.wallet.execute is True
    assert saved.wallet.recipient == "abc"


def test_generate_wallet_key_persists_file_and_disables_execution(tmp_path) -> None:
    client, config_path = make_client(tmp_path)
    client.patch("/wallet", json={"execute": True})

    response = client.post("/wallet/generate", json={"overwrite": False})
    assert response.status_code == 200

    payload = response.json()
    key_file = tmp_path / "wallet.key"
    assert payload["address"]
    assert payload["execute_enabled"] is False
    assert payload["private_key_file"] == str(key_file)
    assert key_file.exists()
    assert load_config(config_path).wallet.execute is False


def test_generate_wallet_key_requires_overwrite_for_existing_key(tmp_path) -> None:
    client, _config_path = make_client(tmp_path)
    first = client.post("/wallet/generate", json={"overwrite": False})
    assert first.status_code == 200

    second = client.post("/wallet/generate", json={"overwrite": False})
    assert second.status_code == 409


def test_import_wallet_key_persists_file(tmp_path) -> None:
    client, config_path = make_client(tmp_path)
    wallet = Wallet()

    response = client.post(
        "/wallet/import",
        json={"private_key": wallet.private_key, "overwrite": True},
    )
    assert response.status_code == 200

    key_file = tmp_path / "wallet.key"
    assert key_file.read_text(encoding="utf-8").strip() == wallet.private_key
    assert response.json()["address"] == wallet.public_key
    assert wallet.private_key not in response.text
    assert load_config(config_path).wallet.private_key_file == key_file
