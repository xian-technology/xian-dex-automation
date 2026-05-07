from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class NetworkConfig(BaseModel):
    rpc_url: str = "http://127.0.0.1:26657"
    chain_id: str | None = None
    watcher_mode: Literal["auto", "poll", "websocket"] = "auto"
    poll_interval_seconds: float = Field(default=1.0, gt=0)


class DexConfig(BaseModel):
    router_contract: str = "con_dex"
    pairs_contract: str = "con_pairs"


class WalletConfig(BaseModel):
    private_key_env: str = "XIAN_DEX_AUTOMATION_PRIVATE_KEY"
    private_key_file: Path | None = None
    private_key_file_env: str = "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE"
    execute: bool = False
    recipient: str | None = None


class PriceMoveTriggerConfig(BaseModel):
    type: Literal["price_move"] = "price_move"
    pair_id: int = Field(gt=0)
    direction: Literal["up", "down", "either"] = "either"
    threshold_bps: int = Field(gt=0)
    cooldown_seconds: int = Field(default=300, ge=0)


class SwapExactInActionConfig(BaseModel):
    type: Literal["swap_exact_in"] = "swap_exact_in"
    src: str
    amount_in: Decimal = Field(gt=0)
    max_slippage_bps: int = Field(default=100, ge=0, lt=10_000)
    deadline_seconds: int = Field(default=300, gt=0)
    recipient: str | None = None

    @field_validator("amount_in", mode="before")
    @classmethod
    def coerce_amount_in(cls, value: object) -> object:
        if isinstance(value, float):
            return str(value)
        return value


class RuleConfig(BaseModel):
    id: str
    enabled: bool = True
    trigger: PriceMoveTriggerConfig
    action: SwapExactInActionConfig


class AutomationConfig(BaseModel):
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    dex: DexConfig = Field(default_factory=DexConfig)
    wallet: WalletConfig = Field(default_factory=WalletConfig)
    database_path: Path = Path("state/xian-dex-automation.sqlite3")
    rules: list[RuleConfig] = Field(default_factory=list)


def _resolve_relative_path(path: Path, *, base_dir: Path) -> Path:
    if path.is_absolute():
        return path
    return base_dir / path


def _resolve_optional_relative_path(
    path: Path | None,
    *,
    base_dir: Path,
) -> Path | None:
    if path is None:
        return None
    return _resolve_relative_path(path, base_dir=base_dir)


def normalize_config_paths(
    config: AutomationConfig,
    *,
    config_path: str | Path,
) -> AutomationConfig:
    resolved_config_path = Path(config_path).resolve()
    config_dir = resolved_config_path.parent
    normalized = config.model_copy(deep=True)
    normalized.database_path = _resolve_relative_path(
        normalized.database_path,
        base_dir=config_dir,
    )
    normalized.wallet.private_key_file = _resolve_optional_relative_path(
        normalized.wallet.private_key_file,
        base_dir=config_dir,
    )
    return normalized


def parse_config_text(
    text: str,
    *,
    config_path: str | Path,
) -> AutomationConfig:
    raw = yaml.safe_load(text) or {}
    return normalize_config_paths(
        AutomationConfig.model_validate(raw),
        config_path=config_path,
    )


def load_config(path: str | Path) -> AutomationConfig:
    config_path = Path(path).resolve()
    return parse_config_text(
        config_path.read_text(encoding="utf-8"),
        config_path=config_path,
    )


def render_config(config: AutomationConfig) -> str:
    payload = config.model_dump(mode="json")
    return yaml.safe_dump(payload, sort_keys=False)


def save_config(config: AutomationConfig, path: str | Path) -> None:
    config_path = Path(path).resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(render_config(config), encoding="utf-8")
