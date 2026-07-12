DEX_CONTRACT = "con_dex"
DEX_PAIRS = "con_pairs"
MAX_DEADLINE_SECONDS = 3600
MAX_COOLDOWN_SECONDS = 2592000
BPS = 10000

pairs = ForeignHash(foreign_contract=DEX_PAIRS, foreign_name="pairs")
settings = Hash()
spent_total = Variable()
last_execution = Variable()
paused = Variable()

Deposited = LogEvent(
    "Deposited",
    {
        "token": {"type": str, "idx": True},
        "amount": {"type": (int, float, decimal)},
    },
)
Withdrawn = LogEvent(
    "Withdrawn",
    {
        "token": {"type": str, "idx": True},
        "amount": {"type": (int, float, decimal)},
    },
)
SwapExecuted = LogEvent(
    "SwapExecuted",
    {
        "keeper": {"type": str, "idx": True},
        "pair": {"type": int, "idx": True},
        "amount_in": {"type": (int, float, decimal)},
        "amount_out": {"type": (int, float, decimal)},
    },
)


@construct
def seed(
    keeper: str,
    pair: int,
    src: str,
    token_out: str,
    max_trade_size: float,
    total_spend_cap: float,
    max_slippage_bps: int = 100,
    cooldown_seconds: int = 300,
    max_deadline_seconds: int = 300,
):
    assert isinstance(keeper, str) and keeper != "", "VAULT: INVALID_KEEPER"
    assert isinstance(pair, int) and pair > 0, "VAULT: INVALID_PAIR"
    assert isinstance(src, str) and src != "", "VAULT: INVALID_SRC"
    assert isinstance(token_out, str) and token_out != src, "VAULT: INVALID_TOKEN_OUT"
    assert max_trade_size > 0, "VAULT: INVALID_MAX_TRADE_SIZE"
    assert total_spend_cap >= max_trade_size, "VAULT: INVALID_SPEND_CAP"
    assert 0 <= max_slippage_bps < BPS, "VAULT: INVALID_SLIPPAGE"
    assert 0 <= cooldown_seconds <= MAX_COOLDOWN_SECONDS, "VAULT: INVALID_COOLDOWN"
    assert 0 < max_deadline_seconds <= MAX_DEADLINE_SECONDS, "VAULT: INVALID_DEADLINE"
    token0 = pairs[pair, "token0"]
    token1 = pairs[pair, "token1"]
    assert token0 is not None and token1 is not None, "VAULT: PAIR_NOT_FOUND"
    assert (
        (src == token0 and token_out == token1)
        or (src == token1 and token_out == token0)
    ), "VAULT: TOKEN_PAIR_MISMATCH"

    settings["owner"] = ctx.caller
    settings["keeper"] = keeper
    settings["pair"] = pair
    settings["src"] = src
    settings["token_out"] = token_out
    settings["max_trade_size"] = max_trade_size
    settings["total_spend_cap"] = total_spend_cap
    settings["max_slippage_bps"] = max_slippage_bps
    settings["cooldown_seconds"] = cooldown_seconds
    settings["max_deadline_seconds"] = max_deadline_seconds
    spent_total.set(0)
    paused.set(True)


def require_owner():
    assert ctx.caller == settings["owner"], "VAULT: OWNER_ONLY"


def require_token(token: str):
    assert token == settings["src"] or token == settings["token_out"], (
        "VAULT: TOKEN_NOT_ALLOWED"
    )
    assert importlib.exists(token), "VAULT: TOKEN_NOT_FOUND"
    assert importlib.has_export(token, "balance_of"), "VAULT: INVALID_TOKEN"
    assert importlib.has_export(token, "transfer"), "VAULT: INVALID_TOKEN"
    assert importlib.has_export(token, "transfer_from"), "VAULT: INVALID_TOKEN"
    assert importlib.has_export(token, "approve"), "VAULT: INVALID_TOKEN"
    return importlib.import_module(token)


@export
def get_strategy():
    return {
        "owner": settings["owner"],
        "keeper": settings["keeper"],
        "pair": settings["pair"],
        "src": settings["src"],
        "token_out": settings["token_out"],
        "action": "swap_exact_in",
        "max_trade_size": settings["max_trade_size"],
        "total_spend_cap": settings["total_spend_cap"],
        "spent_total": spent_total.get(),
        "max_slippage_bps": settings["max_slippage_bps"],
        "cooldown_seconds": settings["cooldown_seconds"],
        "max_deadline_seconds": settings["max_deadline_seconds"],
        "last_execution": last_execution.get(),
        "paused": paused.get(),
    }


@export
def deposit(token: str, amount: float):
    require_owner()
    assert amount > 0, "VAULT: INVALID_AMOUNT"
    token_contract = require_token(token)
    balance_before = token_contract.balance_of(ctx.this)
    token_contract.transfer_from(amount, ctx.this, ctx.caller)
    received = token_contract.balance_of(ctx.this) - balance_before
    assert received > 0, "VAULT: NO_TOKENS_RECEIVED"
    Deposited({"token": token, "amount": received})
    return received


@export
def withdraw(token: str, amount: float):
    require_owner()
    assert amount > 0, "VAULT: INVALID_AMOUNT"
    token_contract = require_token(token)
    assert token_contract.balance_of(ctx.this) >= amount, "VAULT: INSUFFICIENT_BALANCE"
    token_contract.transfer(amount, settings["owner"])
    Withdrawn({"token": token, "amount": amount})
    return amount


@export
def set_keeper(keeper: str):
    require_owner()
    assert isinstance(keeper, str) and keeper != "", "VAULT: INVALID_KEEPER"
    settings["keeper"] = keeper
    paused.set(True)
    return keeper


@export
def set_paused(value: bool):
    assert isinstance(value, bool), "VAULT: INVALID_PAUSE_VALUE"
    if value:
        assert ctx.caller == settings["owner"] or ctx.caller == settings["keeper"], (
            "VAULT: PAUSE_FORBIDDEN"
        )
    else:
        require_owner()
    paused.set(value)
    return value


@export
def tighten_limits(
    max_trade_size: float,
    total_spend_cap: float,
    max_slippage_bps: int,
    cooldown_seconds: int,
    max_deadline_seconds: int,
):
    require_owner()
    assert max_trade_size > 0, "VAULT: INVALID_MAX_TRADE_SIZE"
    assert max_trade_size <= settings["max_trade_size"], "VAULT: LIMIT_INCREASE"
    assert total_spend_cap >= spent_total.get(), "VAULT: CAP_BELOW_SPENT"
    assert total_spend_cap <= settings["total_spend_cap"], "VAULT: LIMIT_INCREASE"
    assert max_trade_size <= total_spend_cap, "VAULT: INVALID_SPEND_CAP"
    assert 0 <= max_slippage_bps <= settings["max_slippage_bps"], (
        "VAULT: LIMIT_INCREASE"
    )
    assert settings["cooldown_seconds"] <= cooldown_seconds <= MAX_COOLDOWN_SECONDS, (
        "VAULT: LIMIT_INCREASE"
    )
    assert 0 < max_deadline_seconds <= settings["max_deadline_seconds"], (
        "VAULT: LIMIT_INCREASE"
    )
    settings["max_trade_size"] = max_trade_size
    settings["total_spend_cap"] = total_spend_cap
    settings["max_slippage_bps"] = max_slippage_bps
    settings["cooldown_seconds"] = cooldown_seconds
    settings["max_deadline_seconds"] = max_deadline_seconds
    paused.set(True)
    return get_strategy()


@export
def execute_swap(amount_in: float, amount_out_min: float, deadline: datetime.datetime):
    assert ctx.caller == settings["keeper"], "VAULT: KEEPER_ONLY"
    assert paused.get() is False, "VAULT: PAUSED"
    assert amount_in > 0, "VAULT: INVALID_AMOUNT"
    assert amount_in <= settings["max_trade_size"], "VAULT: TRADE_CAP_EXCEEDED"
    assert spent_total.get() + amount_in <= settings["total_spend_cap"], (
        "VAULT: SPEND_CAP_EXCEEDED"
    )
    assert amount_out_min > 0, "VAULT: INVALID_MIN_OUT"
    assert now < deadline, "VAULT: EXPIRED"
    assert deadline <= now + datetime.timedelta(
        seconds=settings["max_deadline_seconds"]
    ), "VAULT: DEADLINE_TOO_LONG"

    previous = last_execution.get()
    if previous is not None:
        next_execution = previous + datetime.timedelta(
            seconds=settings["cooldown_seconds"]
        )
        assert now >= next_execution, "VAULT: COOLDOWN"

    dex = importlib.import_module(DEX_CONTRACT)
    quote = dex.getAmountsOut(
        amountIn=amount_in,
        src=settings["src"],
        path=[settings["pair"]],
    )
    assert isinstance(quote, list) and len(quote) == 2, "VAULT: INVALID_QUOTE"
    minimum_allowed = quote[1] * ((BPS - settings["max_slippage_bps"]) / BPS)
    assert amount_out_min >= minimum_allowed, "VAULT: SLIPPAGE_EXCEEDED"

    source_token = require_token(settings["src"])
    output_token = require_token(settings["token_out"])
    assert source_token.balance_of(ctx.this) >= amount_in, "VAULT: INSUFFICIENT_BALANCE"
    output_before = output_token.balance_of(ctx.this)
    source_token.approve(amount_in, DEX_CONTRACT)
    dex.swapExactTokenForToken(
        amountIn=amount_in,
        amountOutMin=amount_out_min,
        pair=settings["pair"],
        src=settings["src"],
        to=ctx.this,
        deadline=deadline,
    )
    amount_out = output_token.balance_of(ctx.this) - output_before
    assert amount_out >= amount_out_min, "VAULT: INSUFFICIENT_OUTPUT"

    spent_total.set(spent_total.get() + amount_in)
    last_execution.set(now)
    SwapExecuted(
        {
            "keeper": ctx.caller,
            "pair": settings["pair"],
            "amount_in": amount_in,
            "amount_out": amount_out,
        }
    )
    return amount_out
