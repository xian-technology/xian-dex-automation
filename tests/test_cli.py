from __future__ import annotations

import pytest

from xian_dex_automation.cli import is_loopback_host, validate_serve_host
from xian_dex_automation.service import ADMIN_TOKEN_ENV


def test_loopback_host_detection() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.10")


def test_non_loopback_serve_host_requires_admin_token(monkeypatch) -> None:
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)

    with pytest.raises(ValueError, match=ADMIN_TOKEN_ENV):
        validate_serve_host("0.0.0.0")


def test_non_loopback_serve_host_accepts_admin_token(monkeypatch) -> None:
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "test-token")

    validate_serve_host("0.0.0.0")

