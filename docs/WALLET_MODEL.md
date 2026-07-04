# Wallet Model

## Why Not Use The Browser Wallet Directly?

Browser wallets are interactive. They are designed to ask a human to approve a
transaction now, not to sign a trade hours later because a DEX event happened.

Putting long-lived private keys into browser storage would make the system less
safe than a local service wallet and harder to operate reliably.

## Current Model: Dedicated Automation Wallet

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

## Future Model: Strategy Contract

For a safer public product, users should connect their wallet to a setup UI and
deposit funds into a strategy contract with hard limits:

- allowed pair
- allowed trade direction
- max trade size
- max slippage
- cooldown
- total budget
- emergency withdraw / disable

The keeper would trigger the contract, but the contract would enforce the
limits. That makes the keeper less trusted.
