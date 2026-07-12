# Scripts

`bootstrap_strategy_vault.py` is the localnet setup and end-to-end exercise
entrypoint for the strategy-vault custody model. It reads the disposable
founder key and RPC address from `xian-stack/.localnet/network.json`.

The helper uses 100 currency as a minimum keeper-balance floor, not a guessed
final budget. After the exact quote and deadline kwargs exist, it estimates that
same `execute_swap` call as the keeper with 250 chi of explicit headroom, reads
`chi_cost.current_value()`, and calculates
`ceil(supplied_chi / chi_cost)`. The funding target is the greater of that live
requirement and `--keeper-gas-funding`. The unchanged call plan and preflight
`supplied_chi` are then used for submission so estimation cannot drift from the
executed kwargs.

The default invocation is a read-only plan. Chain writes, keeper-key creation,
and config-file creation require `--execute`; a real keeper-triggered swap
requires the additional `--execute-swap` flag. Safe JSON includes the estimate,
headroom, supplied chi, chi cost, required currency, funding target, and top-up.
A swap is only reported as
successful after submission acceptance, finalization, and a successful receipt.

See `docs/WALLET_MODEL.md` for the exact localnet commands and security model.
