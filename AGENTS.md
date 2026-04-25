# xian-dex-automation Agent Notes

- Keep the executor deterministic and explicit. Do not add AI decision-making to
  this repo.
- Default every trading path to dry-run unless configuration enables execution.
- Never log private keys or wallet seed material.
- Prefer `xian-py` for node reads, event watching, and transaction submission.
- Use the browser wallet only in future setup UI flows where a human signs the
  immediate transaction.

