# xian-dex-automation Agent Notes

- Keep the executor deterministic and explicit. Do not add AI decision-making to
  this repo.
- Default every trading path to dry-run unless configuration enables execution.
- Never log private keys or wallet seed material.
- Prefer `xian-py` for node reads, event watching, and transaction submission.
- Use the browser wallet only in future setup UI flows where a human signs the
  immediate transaction.

## Shared Agent Practices
- Keep changes clean, modular, and professional. Prefer small, cohesive modules, clear naming, explicit boundaries, and tests over quick patches.
- When code behavior, public APIs, user workflows, operator workflows, or configuration semantics change, check whether `../xian-docs-web` needs corresponding documentation updates. If this repo is `xian-docs-web`, update the relevant published docs in place. Write durable user/developer documentation, not a changelog entry.
- For code changes, use graphify when available to check cross-repo impact before finishing: query the local `graphify-out/graph.json`, inspect paths with `graphify path` or `graphify explain`, and refresh with `graphify update .` after structural changes when useful.
- If graphify or dependency analysis shows affected sibling repos, update those repos in the same change when the impact is real and the fix is in scope.
- Treat `graphify-out/` as a generated local artifact. Do not commit it.
