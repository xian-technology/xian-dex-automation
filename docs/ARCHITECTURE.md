# Architecture

`xian-dex-automation` is split into three layers.

## Executor

The executor is a Python worker built on `xian-py`. It:

- watches `con_pairs` events, starting with `Sync`
- reads canonical pair reserves from chain state before every decision
- evaluates local, deterministic rules
- records decisions in SQLite
- optionally signs and submits transactions with a configured automation wallet
- dispatches either directly to `con_dex` or to an on-chain strategy vault;
  rule evaluation remains identical and deterministic in both modes

This is the only component that should run unattended.

### Submission Ownership and Recovery

`storage.AutomationStore.execution_lock` uses a nonblocking POSIX advisory lock
on a stable sidecar file beside the resolved SQLite path. Every worker and API
evaluation holds it across snapshot, decision, signing, and reconciliation.
Kernel lock release handles process death; there is no expiring lease that
could permit a second writer while the first is still submitting.

`dex.DexClient.prepare_swap` uses `xian-py` to prepare an immutable signed
transaction without broadcasting. A transaction in SQLite creates its `pending`
run and singleton `pending_execution` journal together. The journal contains
signed bytes, hash, original RPC/chain, target baseline, and a durable
`broadcast_attempted` marker. No private key is stored.

`execution.reconcile_execution` runs under the same execution lock. It marks an
attempt before sending any bytes through `submission.SwapTransport`. Recovery
first looks up an attempted transaction; any rebroadcast preserves its identity.
Only a receipt or a definitive first-attempt CheckTx rejection can finalize it.
An ambiguous attempt cannot be discarded based on a later rejection. Completion
updates the run and, on success, rule state, then removes the journal in one
SQLite transaction. A failed commit leaves the original pending record intact.

Startup recovery precedes event watching. A background recovery task also runs
without new events. Pending submissions block fresh signing; event-triggered
submissions wait for reconciliation without repeatedly evaluating their trigger.
Busy or deferred event evaluations retry before cursor advancement, and cursor
writes cannot move backward. Event delivery itself is not exactly-once.

This coordination is local to one database on one host. Runtime database
switches are rejected by the API so they cannot strand a journal. Other live
configuration changes still require an operator restart to recreate the worker's
RPC client and signer; the journal always retains the original submission network.

## API

The FastAPI service exposes status, configured rules, recorded runs, wallet
metadata, rule/config write endpoints, and a manual pair evaluation endpoint.
It does not expose private key material and does not make hidden trading
decisions. `GET /` and `GET /health` are public; every other API endpoint
requires the bearer token from `XIAN_DEX_AUTOMATION_ADMIN_TOKEN`. Unsafe
methods also reject cross-origin browser requests.

## Admin UI

The current service includes a local admin UI served by the FastAPI process. It
is for operator setup and inspection:

- view dry-run/execute status
- see the automation wallet address
- generate, rotate, or import a dedicated service wallet key file
- edit deterministic rules
- inspect recent runs
- manually evaluate a pair
- edit the YAML config

The admin UI does not connect to the user's browser wallet, does not return
private key material through the API, and should stay loopback-bound unless an
operator deliberately protects and exposes it. Generating, rotating, or
importing a key writes only to the configured service key path and disables
execution so the new wallet starts in dry-run mode.

## Browser Setup Direction

A future consumer setup UI should use `xian-js` and the injected browser wallet
for human-approved setup actions:

- connect wallet
- show the automation wallet address
- help fund the automation wallet
- later, create or fund an on-chain strategy contract

The browser wallet should not be treated as the unattended executor. It cannot
reliably sign future event-triggered trades after the user leaves the page.

## Custody Models

The current implementation is a local automation wallet. It is practical for
operators and power users because risk is bounded by the funds sent to that
wallet.

For less-trusted automation, `contracts/con_dex_strategy_vault.py` fixes one
pair, direction, action, keeper, and cumulative budget per deployment. The
contract enforces trade size, slippage, cooldown, deadline, authority, and
withdrawal controls. The Python worker only quotes and triggers the constrained
entrypoint; it cannot redirect vault output or loosen limits.

`custody.strategy_vault` mirrors the intended on-chain envelope so invalid
rules fail during config/API validation. This duplication is an operator
guardrail, not the custody boundary: the contract remains authoritative.
