# xian-dex-automation

`xian-dex-automation` is the deterministic event-driven automation service
for the Xian DEX. It watches DEX pair events, evaluates explicit rules, and
optionally executes swaps from a dedicated automation wallet, with an
admin API and a built-in admin UI.

It is deliberately separate from neighbouring projects:

- `xian-dex-automation` — predictable, rule-driven execution.
- `xian-intentkit` — agent workflows where an AI model decides what to do.
- `xian-dex` (the DEX website) — the human trading and liquidity UI.

## Quick Start

Set up a local environment:

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

Run the API (admin UI at `http://127.0.0.1:8787`):

```bash
xian-dex-automation serve --config config.yaml --host 127.0.0.1 --port 8787
```

Run the worker:

```bash
xian-dex-automation run-worker --config config.yaml
```

For local operator testing, run API + worker in one process:

```bash
xian-dex-automation serve --config config.yaml --host 127.0.0.1 --port 8787 --with-worker
```

### Enabling Execution

Provide a private key and opt in:

```bash
export XIAN_DEX_AUTOMATION_PRIVATE_KEY=...
# or set wallet.private_key_file in config.yaml
# or set XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE to a key file
```

Then set `wallet.execute: true` in `config.yaml` (or via the admin UI). The
admin UI can also generate, rotate, or import a dedicated service wallet.
Any key change forces `wallet.execute: false`, so a new or imported wallet
always starts in dry-run mode.

### Stack-Managed Sidecar

When this repo lives next to `xian-stack`, the backend runs it as an
optional sidecar:

```bash
cd ../xian-stack
python3 ./scripts/backend.py start     --no-service-node --dex-automation
python3 ./scripts/backend.py endpoints --no-service-node --dex-automation
```

Default URL: `http://127.0.0.1:38280`. The stack-managed path creates a
dedicated local key file at
`xian-stack/.artifacts/dex-automation/wallet.key` and keeps the service in
dry-run mode until execution is explicitly enabled.

## Principles

- **Deterministic, rule-driven execution.** The service evaluates explicit
  rules; no learned policies, no agent reasoning loop. Use
  `xian-intentkit` if that is what you want.
- **Browser wallets cannot drive automation.** Browser wallets are
  interactive and require user presence per transaction. Unattended
  automation uses a dedicated automation wallet (current model) or, in the
  future, an on-chain strategy / vault that constrains what an off-chain
  keeper can trigger.
- **Bounded by wallet balance.** The service can only trade funds held by
  its own wallet. Fund it with a deliberately limited budget.
- **Default to dry-run.** Execution is opt-in. Generated, rotated, or
  imported wallets always start with `wallet.execute: false`.
- **Local-only by default.** API and admin UI bind to `127.0.0.1`. The UI
  talks to the same local API and never returns private key material.
- **Independent of the DEX repo.** This service is event-driven and lives
  outside `xian-dex`; the DEX repo owns contracts and frontend, this repo
  owns automation.

## Wallet Model

1. **Dedicated automation wallet (available now).** Generate a new wallet,
   fund it with a limited budget, run this service with that private key.
2. **On-chain strategy / vault (future hardening).** A user connects a
   browser wallet, deposits a bounded budget into a strategy contract,
   and the off-chain keeper can only trigger actions allowed by the
   contract.

The current implementation uses model 1 and defaults to dry-run.

## Rule Shape

- **Trigger** `price_move` — stores the first observed pair price as a
  baseline, fires when the pair price moves by the configured basis points
  from that baseline. After a dry-run or executed action, the current
  price becomes the new baseline.
- **Action** `swap_exact_in` — quotes the configured input amount, applies
  `max_slippage_bps`, and calls `con_dex.swapExactTokenForToken` when
  execution is enabled.

See [config.example.yaml](config.example.yaml) for the full shape.

## API Surface

When the API is running:

- `GET    /health`
- `GET    /rules`
- `PUT    /rules/{rule_id}`
- `DELETE /rules/{rule_id}`
- `GET    /runs`
- `GET    /wallet`
- `PATCH  /wallet`
- `POST   /wallet/generate`
- `POST   /wallet/import`
- `GET    /config.yaml`
- `PUT    /config.yaml`
- `POST   /evaluate/{pair_id}`

`POST /evaluate/{pair_id}` evaluates matching rules once. In dry-run mode
it records what would have happened without submitting a transaction.

## Key Directories

- `src/xian_dex_automation/` — service code:
  - `cli.py` — `xian-dex-automation` console entrypoint (`validate-config`,
    `serve`, `run-worker`).
  - `service.py` — FastAPI service and admin API.
  - `worker.py` — event watcher and rule-evaluation loop.
  - `rules.py` — trigger and action implementations.
  - `dex.py` — DEX-side reads, quotes, and submission.
  - `storage.py` — SQLite-backed persistence (rules, runs, baselines,
    wallet metadata).
  - `config.py` — typed config schema.
- `web/` — built-in admin UI assets.
- `state/` — local SQLite state.
- `tests/` — unit, frontend, and opt-in live-node coverage.
- `docs/` — architecture and wallet-model notes.

## Validation

```bash
uv run --extra dev ruff check .
uv run --extra dev pytest
uv run --extra dev python -m compileall src tests
```

Optional live-node test against a local node that already has the DEX
contracts and a liquid pair:

```bash
XIAN_DEX_AUTOMATION_LIVE_RPC_URL=http://127.0.0.1:26657 \
XIAN_DEX_AUTOMATION_LIVE_PAIR_ID=1 \
uv run --extra dev pytest tests/test_live_node.py -q
```

GitHub Actions runs lint, unit / frontend tests, and compile checks on
pushes and pull requests to `main`. The live-node test is skipped in CI
unless the environment variables above are provided.

## Related Docs

- [AGENTS.md](AGENTS.md) — repo-specific guidance for AI agents and contributors
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — major components and dependency direction
- [docs/WALLET_MODEL.md](docs/WALLET_MODEL.md) — wallet model and security boundary
- [config.example.yaml](config.example.yaml) — annotated example config
