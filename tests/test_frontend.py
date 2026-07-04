from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from xian_dex_automation.config import load_config
from xian_dex_automation.service import create_app


class AttributeCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.test_ids: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if values.get("data-testid"):
            self.test_ids.append(str(values["data-testid"]))


def make_client(tmp_path) -> TestClient:
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
    return TestClient(
        create_app(load_config(config_path), config_path=config_path)
    )


def test_frontend_has_stable_control_contract(tmp_path) -> None:
    response = make_client(tmp_path).get("/")
    assert response.status_code == 200

    collector = AttributeCollector()
    collector.feed(response.text)

    duplicate_ids = {
        value: count
        for value, count in Counter(collector.ids).items()
        if count > 1
    }
    assert duplicate_ids == {}

    required_test_ids = {
        "admin-token",
        "save-admin-token",
        "clear-admin-token",
        "mode",
        "rule-count",
        "wallet-address",
        "wallet-execute",
        "import-private-key",
        "generate-wallet",
        "rotate-wallet",
        "import-wallet",
        "save-rule",
        "rules-table",
        "runs-table",
        "evaluate-pair",
        "evaluation",
        "yaml-config",
    }
    assert required_test_ids.issubset(set(collector.test_ids))


def test_frontend_wires_wallet_and_rule_endpoints(tmp_path) -> None:
    html = make_client(tmp_path).get("/").text

    for endpoint in (
        "/health",
        "/wallet",
        "/wallet/generate",
        "/wallet/import",
        "/rules",
        "/runs",
        "/config.yaml",
        "/evaluate/",
    ):
        assert endpoint in html
    assert "authorization" in html
    assert "sessionStorage" in html
