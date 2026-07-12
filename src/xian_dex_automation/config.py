from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


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


class StrategyVaultConfig(BaseModel):
    contract: str = Field(pattern=r"^con_[A-Za-z0-9_]+$")
    keeper_address: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    pair_id: int = Field(gt=0)
    src: str = Field(min_length=1)
    token_out: str = Field(min_length=1)
    max_trade_size: Decimal = Field(gt=0)
    total_spend_cap: Decimal = Field(gt=0)
    max_slippage_bps: int = Field(default=100, ge=0, lt=10_000)
    cooldown_seconds: int = Field(default=300, ge=0, le=2_592_000)
    max_deadline_seconds: int = Field(default=300, gt=0, le=3_600)

    @field_validator("max_trade_size", "total_spend_cap", mode="before")
    @classmethod
    def coerce_decimal(cls, value: object) -> object:
        if isinstance(value, float):
            return str(value)
        return value

    @model_validator(mode="after")
    def validate_budget(self) -> Self:
        if self.src == self.token_out:
            raise ValueError("strategy vault src and token_out must differ")
        if self.total_spend_cap < self.max_trade_size:
            raise ValueError(
                "strategy vault total_spend_cap must be at least max_trade_size"
            )
        return self


class CustodyConfig(BaseModel):
    mode: Literal["direct_wallet", "strategy_vault"] = "direct_wallet"
    strategy_vault: StrategyVaultConfig | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode == "strategy_vault" and self.strategy_vault is None:
            raise ValueError(
                "custody.strategy_vault is required in strategy_vault mode"
            )
        if self.mode == "direct_wallet" and self.strategy_vault is not None:
            raise ValueError(
                "custody.strategy_vault must be omitted in direct_wallet mode"
            )
        return self


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
    custody: CustodyConfig = Field(default_factory=CustodyConfig)
    database_path: Path = Path("state/xian-dex-automation.sqlite3")
    rules: list[RuleConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_strategy_rules(self) -> Self:
        if self.custody.mode != "strategy_vault":
            return self
        strategy = self.custody.strategy_vault
        if strategy is None:
            return self
        if self.wallet.recipient is not None:
            raise ValueError(
                "wallet.recipient must be null in strategy_vault mode; "
                "the vault retains all output"
            )
        for rule in self.rules:
            if rule.trigger.pair_id != strategy.pair_id:
                raise ValueError(
                    f"rule {rule.id!r} pair_id exceeds the strategy vault scope"
                )
            if rule.action.src != strategy.src:
                raise ValueError(
                    f"rule {rule.id!r} src exceeds the strategy vault scope"
                )
            if rule.action.recipient is not None:
                raise ValueError(
                    f"rule {rule.id!r} recipient must be null in strategy_vault mode"
                )
            if rule.action.amount_in > strategy.max_trade_size:
                raise ValueError(
                    f"rule {rule.id!r} amount_in exceeds strategy max_trade_size"
                )
            if rule.action.max_slippage_bps > strategy.max_slippage_bps:
                raise ValueError(
                    f"rule {rule.id!r} slippage exceeds strategy maximum"
                )
            if rule.action.deadline_seconds > strategy.max_deadline_seconds:
                raise ValueError(
                    f"rule {rule.id!r} deadline exceeds strategy maximum"
                )
            if rule.trigger.cooldown_seconds < strategy.cooldown_seconds:
                raise ValueError(
                    f"rule {rule.id!r} cooldown is shorter than strategy minimum"
                )
        return self


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
