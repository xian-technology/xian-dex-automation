# Wallet Model

## Why Not Use The Browser Wallet Directly?

Browser wallets are interactive. They are designed to ask a human to approve a
transaction now, not to sign a trade hours later because a DEX event happened.

Putting long-lived private keys into browser storage would make the system less
safe than a local service wallet and harder to operate reliably.

## Model 1: Dedicated Automation Wallet

1. Create a separate Xian wallet for automation.
2. Fund it with only the amount you are willing to let the service trade.
3. Put its private key in `XIAN_DEX_AUTOMATION_PRIVATE_KEY`, set
   `XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE`, or set `wallet.private_key_file` in
   the config.
4. Keep `wallet.execute: false` until dry-run output looks correct.
5. Set `wallet.execute: true` when you are ready to let the service submit
   transactions.

The user's normal wallet never leaves the wallet extension or mobile wallet.

When `xian-stack` starts the service with `--dex-automation`, it creates a
dedicated local key file under `.artifacts/dex-automation/wallet.key`. That key
is a service wallet, not the user's browser wallet. Operators should fund it
with a bounded amount and keep the generated file private.

The local admin UI can generate, rotate, or import the configured service
wallet key file after it is unlocked with `XIAN_DEX_AUTOMATION_ADMIN_TOKEN`.
It never returns the private key after writing it, cannot change the key-file
path over HTTP, and key changes force `wallet.execute: false` so operators can
inspect dry-run output before allowing submissions.

## Model 2: Strategy Vault

`contracts/con_dex_strategy_vault.py` is the first-class, less-trusted keeper
model. A user deploys one vault per strategy, deposits a bounded source-token
budget, and grants an automation wallet permission to call one constrained
entrypoint. The vault enforces:

- canonical `con_dex` and one allowed pair
- one allowed source token, output token, direction, and `swap_exact_in` action
- maximum trade size and cumulative input-token spend cap
- maximum slippage checked against the current on-chain router quote
- cooldown and maximum deadline horizon
- keeper-only execution with output retained in the vault
- owner-only withdrawal and unpause
- owner-or-keeper emergency pause
- owner-controlled keeper rotation and tightening-only limit changes, both of
  which automatically pause the vault

The deployment owner remains the withdrawal authority. The keeper never gains
custody and cannot redirect output. Compromise of the keeper is bounded by the
on-chain pair, direction, action, trade size, cumulative budget, slippage,
cooldown, and deadline limits.

The vault starts paused. After deployment, the owner approves the vault on the
source token, calls `deposit`, reviews `get_strategy()`, and explicitly calls
`set_paused(False)`. Vault limits can only become stricter; increasing or
replenishing the cumulative budget requires a separately reviewed deployment.

## Service Configuration

The off-chain service duplicates the intended vault envelope in
`custody.strategy_vault`. It rejects rules whose pair, source token, recipient,
trade size, slippage, cooldown, or deadline would exceed that envelope. At
execution time `DexClient` submits `execute_swap` to the configured vault
instead of calling the router directly. The contract remains the final security
boundary if the service or its config is compromised.

The configured keeper address must match the public key derived from the
service wallet key. Output recipient overrides are forbidden because all swap
output stays in the vault.

Vault transaction deadlines are based on the node's latest block timestamp,
not an assumed synchronized wall clock. The client subtracts an explicit
five-second safety margin from the configured maximum; for configurations of
five seconds or less it clamps the margin so the encoded deadline remains at
least one second after the observed chain time. If node status has no usable
timestamp, the same margin is applied to a wall-clock fallback. The vault's
on-chain maximum deadline remains authoritative.

`wallet.execute` remains `false` by default in both custody modes. Generating,
rotating, or importing a service key also forces execution back to dry-run.

## Localnet End-To-End Exercise

Start a localnet and install the demo DEX pool:

```bash
cd ../xian-stack
make localnet-init
make localnet-up
cd ../xian-dex
uv run python scripts/bootstrap_dex.py --recipe local-demo
```

Preview the vault deployment without writing files or submitting transactions:

```bash
cd ../xian-dex-automation
uv run --extra dev python scripts/bootstrap_strategy_vault.py
```

Deploy, create a dedicated keeper key file, fund the keeper and vault, unpause,
write a service config that remains dry-run, and obtain a live quote:

```bash
uv run --extra dev python scripts/bootstrap_strategy_vault.py --execute
```

The helper treats 100 currency as a minimum floor. Once the exact keeper call
kwargs are known, it estimates that same call with 250 chi of explicit
headroom, reads `chi_cost.current_value()`, and raises the target to
`ceil(supplied_chi / chi_cost)` when necessary. `--keeper-gas-funding` remains
an operator-specified minimum override. The prepared call plan and supplied chi
are reused unchanged for submission, and the safe JSON output includes all
funding inputs and the resulting top-up.

Also submit one real keeper-triggered vault swap:

```bash
uv run --extra dev python scripts/bootstrap_strategy_vault.py \
  --execute \
  --execute-swap
```

The helper reads the disposable founder key and RPC address from
`../xian-stack/.localnet/network.json`. Override `--network`, `--rpc-url`,
`--owner-private-key`, pair/token names, budgets, or keeper/config paths when
using a non-default local harness. Never use the localnet founder or generated
local keeper keys on a public network.

The helper verifies the exact deployed source before reusing a contract name
and does not unpause an existing paused vault unless the owner explicitly adds
`--unpause-existing` after reviewing `get_strategy()`.
It reports a submitted swap only after the transaction is accepted, finalized,
and has a successful receipt; a returned transaction hash alone is not treated
as execution success.

## Deliberate First-Version Limits

- one pair and one direction per deployed vault
- plain single-pair exact-input swaps only
- no multi-hop or fee-on-transfer route
- no owner rotation
- no cumulative budget increase or replenishment
- deployment/setup is a script and contract workflow; a browser-wallet setup UI
  remains future product work
