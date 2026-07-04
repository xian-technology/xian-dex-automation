from __future__ import annotations

from fastapi.testclient import TestClient
from xian_py.wallet import Wallet

from xian_dex_automation.config import load_config
from xian_dex_automation.service import create_app

ADMIN_TOKEN = "test-admin-token"


def admin_headers(*, origin: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {ADMIN_TOKEN}"}
    if origin is not None:
        headers["origin"] = origin
    return headers


def make_client(
    tmp_path,
    *,
    admin_token: str | None = ADMIN_TOKEN,
) -> tuple[TestClient, object]:
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
    return (
        TestClient(
            create_app(
                config,
                config_path=config_path,
                admin_token=admin_token,
            )
        ),
        config_path,
    )


def test_index_and_config_yaml(tmp_path) -> None:
    client, _config_path = make_client(tmp_path)

    response = client.get("/")
    assert response.status_code == 200
    assert "xian-dex-automation" in response.text
    assert "Generate Wallet" in response.text
    assert "Import Key" in response.text

    yaml_response = client.get("/config.yaml", headers=admin_headers())
    assert yaml_response.status_code == 200
    assert "rules:" in yaml_response.text


def test_admin_api_requires_bearer_token(tmp_path) -> None:
    client, _config_path = make_client(tmp_path)

    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/rules").status_code == 401
    assert client.get(
        "/rules",
        headers={"authorization": "Bearer wrong-token"},
    ).status_code == 401


def test_admin_api_requires_configured_token(tmp_path) -> None:
    client, _config_path = make_client(tmp_path, admin_token="")

    assert client.get("/health").status_code == 200
    assert client.get(
        "/rules",
        headers=admin_headers(),
    ).status_code == 503


def test_admin_api_rejects_cross_origin_mutations(tmp_path) -> None:
    client, _config_path = make_client(tmp_path)

    response = client.patch(
        "/wallet",
        headers=admin_headers(origin="http://evil.example"),
        json={"execute": True},
    )

    assert response.status_code == 403


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

    response = client.put("/rules/r1", json=rule, headers=admin_headers())
    assert response.status_code == 200
    assert load_config(config_path).rules[0].id == "r1"

    delete_response = client.delete("/rules/r1", headers=admin_headers())
    assert delete_response.status_code == 200
    assert load_config(config_path).rules == []


def test_wallet_patch_persists(tmp_path) -> None:
    client, config_path = make_client(tmp_path)

    response = client.patch(
        "/wallet",
        headers=admin_headers(),
        json={"execute": True, "recipient": "abc"},
    )
    assert response.status_code == 200

    saved = load_config(config_path)
    assert saved.wallet.execute is True
    assert saved.wallet.recipient == "abc"


def test_wallet_patch_rejects_private_key_file_updates(tmp_path) -> None:
    client, config_path = make_client(tmp_path)
    key_file = tmp_path / "attacker-selected.key"

    response = client.patch(
        "/wallet",
        headers=admin_headers(),
        json={"private_key_file": str(key_file)},
    )

    assert response.status_code == 400
    assert load_config(config_path).wallet.private_key_file is None


def test_config_yaml_rejects_wallet_key_source_changes(tmp_path) -> None:
    client, config_path = make_client(tmp_path)
    headers = admin_headers()
    headers["content-type"] = "text/plain"

    valid_yaml = config_path.read_text(encoding="utf-8").replace(
        "execute: false",
        "execute: true",
    )
    valid_response = client.put(
        "/config.yaml",
        content=valid_yaml,
        headers=headers,
    )
    assert valid_response.status_code == 200
    assert load_config(config_path).wallet.execute is True

    unsafe_yaml = """
network:
  rpc_url: "http://127.0.0.1:26657"
wallet:
  private_key_file: "attacker-selected.key"
  execute: true
database_path: "automation.sqlite3"
rules: []
""".strip()
    unsafe_response = client.put(
        "/config.yaml",
        content=unsafe_yaml,
        headers=headers,
    )
    assert unsafe_response.status_code == 400
    assert load_config(config_path).wallet.private_key_file is None


def test_rule_and_wallet_settings_survive_app_restart(tmp_path) -> None:
    client, config_path = make_client(tmp_path)
    rule = {
        "id": "persisted-rule",
        "enabled": True,
        "trigger": {
            "type": "price_move",
            "pair_id": 1,
            "direction": "either",
            "threshold_bps": 100,
            "cooldown_seconds": 30,
        },
        "action": {
            "type": "swap_exact_in",
            "src": "currency",
            "amount_in": "2.5",
            "max_slippage_bps": 250,
            "deadline_seconds": 120,
        },
    }

    assert (
        client.put(
            "/rules/persisted-rule",
            json=rule,
            headers=admin_headers(),
        ).status_code
        == 200
    )
    assert (
        client.patch(
            "/wallet",
            json={"execute": True, "recipient": "abc"},
            headers=admin_headers(),
        ).status_code
        == 200
    )

    restarted_client = TestClient(
        create_app(
            load_config(config_path),
            config_path=config_path,
            admin_token=ADMIN_TOKEN,
        )
    )

    rules = restarted_client.get("/rules", headers=admin_headers()).json()
    wallet = restarted_client.get("/wallet", headers=admin_headers()).json()
    assert rules[0]["id"] == "persisted-rule"
    assert rules[0]["action"]["amount_in"] == "2.5"
    assert wallet["execute_enabled"] is True
    assert wallet["recipient"] == "abc"


def test_generate_wallet_key_persists_file_and_disables_execution(
    tmp_path,
) -> None:
    client, config_path = make_client(tmp_path)
    client.patch("/wallet", json={"execute": True}, headers=admin_headers())

    response = client.post(
        "/wallet/generate",
        json={"overwrite": False},
        headers=admin_headers(),
    )
    assert response.status_code == 200

    payload = response.json()
    key_file = tmp_path / "wallet.key"
    assert payload["address"]
    assert payload["execute_enabled"] is False
    assert payload["private_key_file"] == str(key_file)
    assert key_file.exists()
    assert load_config(config_path).wallet.execute is False


def test_generate_wallet_key_requires_overwrite_for_existing_key(
    tmp_path,
) -> None:
    client, _config_path = make_client(tmp_path)
    first = client.post(
        "/wallet/generate",
        json={"overwrite": False},
        headers=admin_headers(),
    )
    assert first.status_code == 200

    second = client.post(
        "/wallet/generate",
        json={"overwrite": False},
        headers=admin_headers(),
    )
    assert second.status_code == 409


def test_import_wallet_key_persists_file(tmp_path) -> None:
    client, config_path = make_client(tmp_path)
    wallet = Wallet()

    response = client.post(
        "/wallet/import",
        json={"private_key": wallet.private_key, "overwrite": True},
        headers=admin_headers(),
    )
    assert response.status_code == 200

    key_file = tmp_path / "wallet.key"
    assert key_file.read_text(encoding="utf-8").strip() == wallet.private_key
    assert response.json()["address"] == wallet.public_key
    assert wallet.private_key not in response.text
    assert load_config(config_path).wallet.private_key_file == key_file
