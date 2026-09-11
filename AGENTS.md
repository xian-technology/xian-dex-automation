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
- Follow `../xian-meta/docs/CODE_GRAPH_WORKFLOW.md` for graph freshness and source verification. Before relying on graph results, run `python3 ../xian-meta/scripts/graphify_workspace.py status --repo xian-dex-automation` and refresh that repo if stale.
- For codebase questions, use the local graph first when `graphify-out/graph.json` exists: run `graphify query "<question>"`; use `graphify affected "<symbol>" --depth 1` for direct dependents, larger depths for transitive impact, and `graphify path "<A>" "<B>" --directed` for call paths and `graphify explain "<concept>"` for focused concepts.
- Dirty `graphify-out/` files are expected after hooks or incremental updates and are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- Use `graphify-out/wiki/index.md` for broad navigation when it exists. Read `graphify-out/GRAPH_REPORT.md` only for broad architecture review or when query/path/explain do not surface enough context.
- For any non-trivial code change, update the local graph before final verification when `graphify-out/graph.json` exists. Run `python3 ../xian-meta/scripts/graphify_workspace.py refresh --repo xian-dex-automation` from the repo root; use `--force` only for inspected, intentional shrinkage.
- After updating the graph, check cross-repo impact before finishing: use `graphify affected`, inspect returned source locations, and search affected sibling repos. A missing edge or truncated result does not prove there are no other consumers.
- If graphify or dependency analysis shows affected sibling repos, update those repos in the same change when the impact is real and the fix is in scope.
- Treat `graphify-out/` as a generated local artifact. Do not commit it.
