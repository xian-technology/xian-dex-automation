# Strategy Vault Contract

`con_dex_strategy_vault.py` is an owner-funded, single-direction custody vault
for deterministic DEX automation. Each deployed instance fixes one canonical
`con_pairs` pair, one input token, one output token, and one keeper.

The contract starts paused and enforces all of these limits on-chain:

- only `swap_exact_in` through canonical `con_dex`
- fixed pair and trade direction
- maximum amount per trade and cumulative input-token spend cap
- maximum slippage against the router's current quote
- minimum interval between executions
- maximum transaction deadline horizon
- output retained by the vault; only the owner can withdraw either allowed token

The keeper can execute and can pause, but cannot unpause, withdraw, change the
strategy, increase limits, or redirect swap output. Changing the keeper or
tightening a limit pauses the vault and requires the owner to review and
unpause it.

## Deploy And Fund

Deploy one instance per strategy from a user-controlled wallet. The deployment
wallet becomes the immutable withdrawal owner. Constructor arguments are:

```python
args = {
    "keeper": "<automation-wallet-public-key>",
    "pair": 1,
    "src": "currency",
    "token_out": "con_token",
    "max_trade_size": 5,
    "total_spend_cap": 100,
    "max_slippage_bps": 100,
    "cooldown_seconds": 300,
    "max_deadline_seconds": 300,
}
```

With `xian-py`, deploy the source under a unique `con_...` name using
`client.deploy_contract(name, source, args=args, wait_for_tx=True)`. The owner
then approves that deployed contract on the input token and calls its
`deposit(token=..., amount=...)` entrypoint. Inspect `get_strategy()`, then call
`set_paused(False)` as the owner only after the keeper configuration and limits
match the service config.

The localnet helper never unpauses a reused paused vault unless the owner
explicitly adds `--unpause-existing`; this prevents a routine rerun from
undoing an emergency pause.

The automation service key must match `keeper`. Configure
`custody.mode: strategy_vault`, keep `wallet.execute: false` for dry runs, and
only enable execution after the contract is funded and unpaused.

## Deliberate Limits

- one pair and one direction per vault
- canonical plain-token `con_dex.swapExactTokenForToken` route only
- no fee-on-transfer or multi-hop routes
- cumulative spend cannot be replenished or increased; deploy a new vault when
  the strategy budget is exhausted
- owner key rotation is intentionally absent; use a deliberate owner wallet and
  withdraw before retiring the vault
