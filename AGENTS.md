# xian-dex-automation Agent Notes

- Keep the executor deterministic and explicit. Do not add AI decision-making to
  this repo.
- Default every trading path to dry-run unless configuration enables execution.
- Never log private keys or wallet seed material.
- Prefer `xian-py` for node reads, event watching, and transaction submission.
- Use the browser wallet only in future setup UI flows where a human signs the
  immediate transaction.

## Local Knowledge Graph
- If `graphify-out/graph.json` exists, prefer `graphify query`, `graphify path`, or `graphify explain` for broad architecture and impact questions before scanning files manually.
- Treat `graphify-out/` as a generated local artifact; it is intentionally ignored by Git.
- After structural code changes, refresh the local graph with `graphify update .` when useful.
