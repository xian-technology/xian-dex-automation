# xian-dex-automation

Deterministic automation for Xian DEX events.

The service watches DEX pair events, evaluates explicit rules, and optionally
executes swaps from a dedicated automation wallet. It is intentionally separate
from the DEX frontend and from xian-intentkit:

- `xian-dex-automation` is for predictable rule execution.
- `xian-intentkit` is for agent workflows where an AI model decides what to do.
- the DEX website remains the human trading and liquidity UI.

## Wallet Model

The service cannot use a user's browser wallet in the background. Browser
wallets are interactive: the user must be present to approve each transaction.

For unattended automation, use one of these models:

1. **Dedicated automation wallet, available now.** Generate a new wallet, fund it
   with a limited budget, and run this service with that private key in an
   environment variable or a local key file. The service can only trade funds
   held by that wallet.
2. **On-chain strategy/vault, future hardening.** A user connects a browser
   wallet, deposits a bounded budget into a strategy contract, and the Python
   keeper can only trigger actions allowed by the contract constraints.

The current implementation uses model 1 and defaults to dry-run.

## Quick Start

```bash
cd xian-dex-automation
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp config.example.yaml config.yaml
```

Validate the config:

```bash
xian-dex-automation validate-config --config config.yaml
```

Run the API:

```bash
xian-dex-automation serve --config config.yaml --host 127.0.0.1 --port 8787
```

Open `http://127.0.0.1:8787` for the built-in admin UI. It shows status,
wallet metadata, service-wallet key-file controls, rule editing, recent runs,
manual pair evaluation, and the raw YAML config. The UI talks to the same local
API and never returns private key material.

Run the worker:

```bash
xian-dex-automation run-worker --config config.yaml
```

For local operator testing, the API and worker can run in one process:

```bash
xian-dex-automation serve --config config.yaml --host 127.0.0.1 --port 8787 --with-worker
```

To execute trades, set an automation wallet private key and opt in:

```bash
export XIAN_DEX_AUTOMATION_PRIVATE_KEY=...
```

You can also set `wallet.private_key_file` in `config.yaml`, or set
`XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE` to point at a local key file. Then set
`wallet.execute: true` in `config.yaml` or through the admin UI.

The admin UI can generate, rotate, or import a dedicated service wallet into
the configured key file. Key changes force `wallet.execute: false`, so a new or
imported wallet starts in dry-run mode.

The stack-managed path creates a dedicated local key file automatically under
`xian-stack/.artifacts/dex-automation/wallet.key` and keeps the service in
dry-run mode until you enable execution.

## Rule Shape

The first trigger type is `price_move`: it stores the first observed pair price
as a baseline, then fires when the pair price moves by the configured basis
points from that baseline. After a dry-run or executed action, the current price
becomes the new baseline.

The first action type is `swap_exact_in`: it quotes the configured input amount,
applies `max_slippage_bps`, and calls `con_dex.swapExactTokenForToken` when
execution is enabled.

See [config.example.yaml](config.example.yaml).

## API

When the API is running:

- `GET /health`
- `GET /rules`
- `PUT /rules/{rule_id}`
- `DELETE /rules/{rule_id}`
- `GET /runs`
- `GET /wallet`
- `PATCH /wallet`
- `POST /wallet/generate`
- `POST /wallet/import`
- `GET /config.yaml`
- `PUT /config.yaml`
- `POST /evaluate/{pair_id}`

`POST /evaluate/{pair_id}` evaluates matching rules once. In dry-run mode it
records what would have happened without submitting a transaction.

## Stack-Managed Node Extension

When this repo lives next to `xian-stack`, the stack backend can run it as an
optional sidecar:

```bash
cd ../xian-stack
python3 ./scripts/backend.py start --no-service-node --dex-automation
python3 ./scripts/backend.py endpoints --no-service-node --dex-automation
```

The default URL is `http://127.0.0.1:38280`. The sidecar watches the configured
node RPC, stores state in SQLite, and uses the local service wallet only if
`wallet.execute` is enabled.

## Validation

Run the CI-equivalent checks locally:

```bash
uv run --extra dev ruff check .
uv run --extra dev pytest
uv run --extra dev python -m compileall src tests
```

Run the opt-in live-node test against a local node that already has the DEX
contracts and a liquid pair:

```bash
XIAN_DEX_AUTOMATION_LIVE_RPC_URL=http://127.0.0.1:26657 \
XIAN_DEX_AUTOMATION_LIVE_PAIR_ID=1 \
uv run --extra dev pytest tests/test_live_node.py -q
```

GitHub Actions runs the lint, unit/frontend tests, and compile checks on pushes
and pull requests to `main`. The live-node test is skipped in CI unless the
environment variables above are provided.
