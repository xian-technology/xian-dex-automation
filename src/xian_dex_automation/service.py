from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .config import (
    AutomationConfig,
    RuleConfig,
    WalletConfig,
    normalize_config_paths,
    parse_config_text,
    render_config,
    save_config,
)
from .dex import resolve_private_key, resolve_private_key_source
from .storage import AutomationStore
from .worker import AutomationWorker

ADMIN_TOKEN_ENV = "XIAN_DEX_AUTOMATION_ADMIN_TOKEN"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def configured_admin_token() -> str | None:
    token = os.environ.get(ADMIN_TOKEN_ENV)
    return _normalize_admin_token(token)


def _normalize_admin_token(token: str | None) -> str | None:
    if token is None:
        return None
    token = token.strip()
    return token or None


def _request_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != _request_origin(request).rstrip("/"):
        raise HTTPException(status_code=403, detail="cross-origin admin request rejected")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site.lower() == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site admin request rejected")


def _require_admin_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = getattr(request.app.state, "admin_token", None)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"{ADMIN_TOKEN_ENV} is required for the admin API",
        )
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(
            status_code=401,
            detail="admin bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization[len(prefix) :].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="invalid admin bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if request.method.upper() in UNSAFE_METHODS:
        _require_same_origin(request)


def _wallet_address(private_key: str | None) -> str | None:
    if not private_key:
        return None
    from xian_py.wallet import Wallet

    return Wallet(private_key).public_key


def _generate_private_key() -> str:
    from xian_py.wallet import Wallet

    return Wallet().private_key


def _validate_private_key(private_key: str) -> str:
    from xian_py.wallet import Wallet

    value = private_key.strip()
    if not value:
        raise HTTPException(status_code=400, detail="private key is required")
    try:
        Wallet(value)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="invalid private key",
        ) from exc
    return value


def _write_private_key_file(
    path: Path, private_key: str, *, overwrite: bool
) -> None:
    if (
        path.exists()
        and path.read_text(encoding="utf-8").strip()
        and not overwrite
    ):
        raise HTTPException(
            status_code=409,
            detail="wallet key file already exists",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{private_key.strip()}\n", encoding="utf-8")
    with suppress(OSError):
        path.chmod(0o600)


def _wallet_key_source_changed(
    current: AutomationConfig,
    new_config: AutomationConfig,
) -> bool:
    return (
        current.wallet.private_key_env != new_config.wallet.private_key_env
        or current.wallet.private_key_file != new_config.wallet.private_key_file
        or current.wallet.private_key_file_env != new_config.wallet.private_key_file_env
    )


def _index_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>xian-dex-automation</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dde5;
      --text: #16202a;
      --muted: #66717f;
      --accent: #1967d2;
      --danger: #b3261e;
      --ok: #0b8043;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 18px 24px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0 0 4px; font-size: 22px; font-weight: 650; }
    h2 { margin: 0 0 12px; font-size: 15px; font-weight: 650; }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 0.9fr) minmax(420px, 1.1fr);
      gap: 16px;
      padding: 16px 24px 28px;
      max-width: 1440px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }
    .stack { display: grid; gap: 16px; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      background: #fff;
      color: var(--text);
    }
    textarea {
      min-height: 360px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
    }
    button {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      border-radius: 6px;
      padding: 9px 12px;
      font: inherit;
      cursor: pointer;
    }
    button.secondary {
      background: #fff;
      color: var(--accent);
    }
    button.danger {
      border-color: var(--danger);
      background: #fff;
      color: var(--danger);
    }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .status {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      min-width: 0;
    }
    .metric span { display: block; color: var(--muted); font-size: 11px; }
    .metric strong { display: block; margin-top: 4px; overflow-wrap: anywhere; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 8px 6px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 11px; font-weight: 600; }
    pre {
      max-height: 320px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: #fbfcfe;
    }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .muted { color: var(--muted); }
    .ok { color: var(--ok); }
    .dangerText { color: var(--danger); }
    .wide { grid-column: 1 / -1; }
    .small { font-size: 12px; }
    .empty { color: var(--muted); padding: 10px 0; }
    #message { min-height: 20px; font-size: 13px; }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; padding: 12px; }
      header { padding: 16px 12px 10px; }
      .status { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>xian-dex-automation</h1>
    <div id="message" class="muted"></div>
  </header>
  <main>
    <div class="stack">
      <section>
        <h2>Status</h2>
        <div class="status">
          <div class="metric"><span>Mode</span><strong id="mode" data-testid="mode">...</strong></div>
          <div class="metric"><span>Rules</span><strong id="ruleCount" data-testid="rule-count">...</strong></div>
          <div class="metric"><span>Wallet</span><strong id="walletAddress" data-testid="wallet-address">...</strong></div>
        </div>
      </section>
      <section>
        <h2>Admin Access</h2>
        <label>Admin token
          <input id="adminToken" data-testid="admin-token" type="password" autocomplete="off" placeholder="XIAN_DEX_AUTOMATION_ADMIN_TOKEN">
        </label>
        <div class="row" style="margin-top: 10px">
          <button id="saveAdminToken" data-testid="save-admin-token">Unlock</button>
          <button id="clearAdminToken" data-testid="clear-admin-token" class="secondary">Lock</button>
        </div>
      </section>
      <section>
        <h2>Wallet</h2>
        <div class="grid">
          <label>Execute trades
            <select id="walletExecute" data-testid="wallet-execute">
              <option value="false">Dry-run only</option>
              <option value="true">Submit transactions</option>
            </select>
          </label>
          <label>Recipient override
            <input id="walletRecipient" data-testid="wallet-recipient" placeholder="defaults to automation wallet">
          </label>
          <label class="wide">Import service wallet private key
            <input id="importPrivateKey" data-testid="import-private-key" type="password" autocomplete="off" placeholder="dedicated automation wallet only">
          </label>
        </div>
        <div class="row" style="margin-top: 10px">
          <button id="saveWallet" data-testid="save-wallet">Save Wallet Settings</button>
          <button id="generateWallet" data-testid="generate-wallet" class="secondary">Generate Wallet</button>
          <button id="rotateWallet" data-testid="rotate-wallet" class="danger">Rotate Wallet</button>
          <button id="importWallet" data-testid="import-wallet" class="secondary">Import Key</button>
          <span id="walletSource" class="muted mono"></span>
        </div>
      </section>
      <section>
        <h2>Rule</h2>
        <div class="grid">
          <label>Rule ID <input id="ruleId" data-testid="rule-id" value="demo-price-move"></label>
          <label>Enabled
            <select id="ruleEnabled" data-testid="rule-enabled">
              <option value="true">Enabled</option>
              <option value="false">Disabled</option>
            </select>
          </label>
          <label>Pair ID <input id="pairId" data-testid="pair-id" type="number" min="1" value="1"></label>
          <label>Direction
            <select id="direction" data-testid="direction">
              <option value="either">Either</option>
              <option value="up">Up</option>
              <option value="down">Down</option>
            </select>
          </label>
          <label>Threshold bps <input id="thresholdBps" data-testid="threshold-bps" type="number" min="1" value="100"></label>
          <label>Cooldown seconds <input id="cooldownSeconds" data-testid="cooldown-seconds" type="number" min="0" value="300"></label>
          <label>Source token <input id="srcToken" data-testid="src-token" value="currency"></label>
          <label>Amount in <input id="amountIn" data-testid="amount-in" value="1"></label>
          <label>Max slippage bps <input id="slippageBps" data-testid="slippage-bps" type="number" min="0" max="9999" value="100"></label>
          <label>Deadline seconds <input id="deadlineSeconds" data-testid="deadline-seconds" type="number" min="1" value="300"></label>
        </div>
        <div class="row" style="margin-top: 10px">
          <button id="saveRule" data-testid="save-rule">Save Rule</button>
          <button id="resetRule" data-testid="reset-rule" class="secondary">Clear</button>
        </div>
      </section>
      <section>
        <h2>Manual Evaluation</h2>
        <div class="row">
          <input id="evaluatePairId" data-testid="evaluate-pair-id" type="number" min="1" value="1" style="max-width: 140px">
          <button id="evaluatePair" data-testid="evaluate-pair">Evaluate Pair</button>
        </div>
        <pre id="evaluation" data-testid="evaluation" class="mono"></pre>
      </section>
    </div>
    <div class="stack">
      <section>
        <h2>Rules</h2>
        <table>
          <thead><tr><th>ID</th><th>Trigger</th><th>Action</th><th></th></tr></thead>
          <tbody id="rulesTable" data-testid="rules-table"></tbody>
        </table>
      </section>
      <section>
        <h2>Runs</h2>
        <table>
          <thead><tr><th>Time</th><th>Rule</th><th>Status</th><th>Reason</th></tr></thead>
          <tbody id="runsTable" data-testid="runs-table"></tbody>
        </table>
      </section>
      <section>
        <h2>YAML Config</h2>
        <textarea id="yamlConfig" data-testid="yaml-config" spellcheck="false"></textarea>
        <div class="row" style="margin-top: 10px">
          <button id="saveYaml" data-testid="save-yaml">Save YAML</button>
          <button id="reloadYaml" data-testid="reload-yaml" class="secondary">Reload</button>
        </div>
      </section>
    </div>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const ADMIN_TOKEN_STORAGE_KEY = "xianDexAutomationAdminToken";
    const adminToken = () => sessionStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
    const message = (text, cls = "muted") => {
      $("message").className = cls;
      $("message").textContent = text;
    };
    const escapeHtml = (value) => String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
    const runAction = async (fn) => {
      try {
        await fn();
      } catch (error) {
        message(error.message, "dangerText");
      }
    };
    const api = async (path, options = {}) => {
      const { skipAuth = false, ...fetchOptions } = options;
      const headers = new Headers(fetchOptions.headers || {});
      const token = adminToken();
      if (!skipAuth && token) headers.set("authorization", `Bearer ${token}`);
      fetchOptions.headers = headers;
      const response = await fetch(path, fetchOptions);
      const contentType = response.headers.get("content-type") || "";
      const payload = contentType.includes("application/json")
        ? await response.json()
        : await response.text();
      if (!response.ok) {
        const detail = payload && payload.detail ? payload.detail : payload;
        throw new Error(detail || response.statusText);
      }
      return payload;
    };
    const rulePayload = () => ({
      id: $("ruleId").value.trim(),
      enabled: $("ruleEnabled").value === "true",
      trigger: {
        type: "price_move",
        pair_id: Number($("pairId").value),
        direction: $("direction").value,
        threshold_bps: Number($("thresholdBps").value),
        cooldown_seconds: Number($("cooldownSeconds").value)
      },
      action: {
        type: "swap_exact_in",
        src: $("srcToken").value.trim(),
        amount_in: $("amountIn").value.trim(),
        max_slippage_bps: Number($("slippageBps").value),
        deadline_seconds: Number($("deadlineSeconds").value)
      }
    });
    const fillRule = (rule) => {
      $("ruleId").value = rule.id || "";
      $("ruleEnabled").value = String(rule.enabled !== false);
      $("pairId").value = rule.trigger.pair_id;
      $("direction").value = rule.trigger.direction;
      $("thresholdBps").value = rule.trigger.threshold_bps;
      $("cooldownSeconds").value = rule.trigger.cooldown_seconds;
      $("srcToken").value = rule.action.src;
      $("amountIn").value = rule.action.amount_in;
      $("slippageBps").value = rule.action.max_slippage_bps;
      $("deadlineSeconds").value = rule.action.deadline_seconds;
    };
    const refresh = async () => {
      const health = await api("/health", { skipAuth: true });
      $("mode").textContent = health.execute_enabled ? "execute" : "dry-run";
      $("mode").className = health.execute_enabled ? "dangerText" : "ok";
      $("ruleCount").textContent = health.rules;
      if (!adminToken()) {
        $("walletAddress").textContent = "locked";
        $("walletSource").textContent = "";
        $("rulesTable").innerHTML = `<tr><td colspan="4" class="empty">Enter the admin token to manage rules</td></tr>`;
        $("runsTable").innerHTML = `<tr><td colspan="4" class="empty">Enter the admin token to inspect runs</td></tr>`;
        $("yamlConfig").value = "";
        $("evaluation").textContent = "";
        message("Enter the admin token to unlock the admin API.", "muted");
        return;
      }
      const [wallet, rules, runs, yaml] = await Promise.all([
        api("/wallet"),
        api("/rules"),
        api("/runs"),
        api("/config.yaml")
      ]);
      $("walletAddress").textContent = wallet.address || "not configured";
      $("walletExecute").value = String(wallet.execute_enabled);
      $("walletRecipient").value = wallet.recipient || "";
      $("walletSource").textContent = wallet.private_key_source || "no key";
      $("yamlConfig").value = yaml;
      $("rulesTable").innerHTML = rules.length ? rules.map((rule) => `
        <tr>
          <td class="mono">${escapeHtml(rule.id)}</td>
          <td>pair ${escapeHtml(rule.trigger.pair_id)}, ${escapeHtml(rule.trigger.direction)}, ${escapeHtml(rule.trigger.threshold_bps)} bps</td>
          <td>${escapeHtml(rule.action.amount_in)} ${escapeHtml(rule.action.src)}, slippage ${escapeHtml(rule.action.max_slippage_bps)} bps</td>
          <td class="row">
            <button class="secondary" data-edit="${escapeHtml(rule.id)}">Edit</button>
            <button class="danger" data-delete="${escapeHtml(rule.id)}">Delete</button>
          </td>
        </tr>`).join("") : `<tr><td colspan="4" class="empty">No rules configured</td></tr>`;
      $("runsTable").innerHTML = runs.length ? runs.map((run) => `
        <tr>
          <td class="mono">${escapeHtml(run.created_at || "")}</td>
          <td class="mono">${escapeHtml(run.rule_id)}</td>
          <td>${escapeHtml(run.status)}</td>
          <td>${escapeHtml(run.reason)}</td>
        </tr>`).join("") : `<tr><td colspan="4" class="empty">No runs recorded</td></tr>`;
      $("rulesTable").querySelectorAll("[data-edit]").forEach((button) => {
        button.addEventListener("click", () => {
          const rule = rules.find((item) => item.id === button.dataset.edit);
          if (rule) fillRule(rule);
        });
      });
      $("rulesTable").querySelectorAll("[data-delete]").forEach((button) => {
        button.addEventListener("click", async () => {
          await runAction(async () => {
            await api(`/rules/${encodeURIComponent(button.dataset.delete)}`, { method: "DELETE" });
            message("Rule deleted", "ok");
            await refresh();
          });
        });
      });
    };
    $("saveRule").addEventListener("click", () => runAction(async () => {
        const rule = rulePayload();
        if (!rule.id) throw new Error("Rule ID is required");
        await api(`/rules/${encodeURIComponent(rule.id)}`, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(rule)
        });
        message("Rule saved", "ok");
        await refresh();
      })
    );
    $("resetRule").addEventListener("click", () => {
      fillRule({
        id: "",
        enabled: true,
        trigger: { pair_id: 1, direction: "either", threshold_bps: 100, cooldown_seconds: 300 },
        action: { src: "currency", amount_in: "1", max_slippage_bps: 100, deadline_seconds: 300 }
      });
    });
    $("saveWallet").addEventListener("click", () => runAction(async () => {
        await api("/wallet", {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            execute: $("walletExecute").value === "true",
            recipient: $("walletRecipient").value.trim() || null
          })
        });
        message("Wallet settings saved", "ok");
        await refresh();
      })
    );
    $("generateWallet").addEventListener("click", () => runAction(async () => {
        await api("/wallet/generate", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ overwrite: false })
        });
        message("Service wallet generated", "ok");
        await refresh();
      })
    );
    $("rotateWallet").addEventListener("click", () => runAction(async () => {
        if (!confirm("Rotate the service wallet key file?")) return;
        await api("/wallet/generate", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ overwrite: true })
        });
        message("Service wallet rotated; execution is disabled", "ok");
        await refresh();
      })
    );
    $("importWallet").addEventListener("click", () => runAction(async () => {
        const privateKey = $("importPrivateKey").value.trim();
        if (!privateKey) throw new Error("Private key is required");
        if (!confirm("Import this key into the service wallet key file?")) return;
        await api("/wallet/import", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ private_key: privateKey, overwrite: true })
        });
        $("importPrivateKey").value = "";
        message("Service wallet imported; execution is disabled", "ok");
        await refresh();
      })
    );
    $("evaluatePair").addEventListener("click", () => runAction(async () => {
        const pairId = $("evaluatePairId").value;
        const result = await api(`/evaluate/${pairId}`, { method: "POST" });
        $("evaluation").textContent = JSON.stringify(result, null, 2);
        await refresh();
      })
    );
    $("saveYaml").addEventListener("click", () => runAction(async () => {
        await api("/config.yaml", {
          method: "PUT",
          headers: { "content-type": "text/plain" },
          body: $("yamlConfig").value
        });
        message("YAML saved", "ok");
        await refresh();
      })
    );
    $("reloadYaml").addEventListener("click", () => runAction(refresh));
    $("adminToken").value = adminToken();
    $("saveAdminToken").addEventListener("click", () => runAction(async () => {
        const token = $("adminToken").value.trim();
        if (!token) throw new Error("Admin token is required");
        sessionStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, token);
        message("Admin API unlocked for this browser tab", "ok");
        await refresh();
      })
    );
    $("clearAdminToken").addEventListener("click", () => {
      sessionStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
      $("adminToken").value = "";
      message("Admin API locked", "muted");
      refresh().catch((error) => message(error.message, "dangerText"));
    });
    refresh().catch((error) => message(error.message, "dangerText"));
  </script>
</body>
</html>
"""


def create_app(
    config: AutomationConfig,
    *,
    start_worker: bool = False,
    config_path: str | Path | None = None,
    admin_token: str | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            app.state.worker_task = asyncio.create_task(
                app.state.worker.run_forever()
            )
        try:
            yield
        finally:
            task = app.state.worker_task
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="xian-dex-automation",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.config_path = Path(config_path).resolve() if config_path else None
    app.state.admin_token = (
        _normalize_admin_token(admin_token)
        if admin_token is not None
        else configured_admin_token()
    )
    app.state.store = AutomationStore(config.database_path)
    app.state.worker = AutomationWorker(config, app.state.store)
    app.state.worker_task = None

    def apply_config(new_config: AutomationConfig) -> None:
        app.state.config = new_config
        if app.state.store.path != new_config.database_path:
            app.state.store = AutomationStore(new_config.database_path)
            app.state.worker.store = app.state.store
        app.state.worker.config = new_config

    def persist_config(new_config: AutomationConfig) -> None:
        if app.state.config_path is None:
            raise HTTPException(
                status_code=409,
                detail="config writes require a config path",
            )
        save_config(new_config, app.state.config_path)
        apply_config(new_config)

    def normalized_for_persist(
        new_config: AutomationConfig,
    ) -> AutomationConfig:
        if app.state.config_path is None:
            raise HTTPException(
                status_code=409,
                detail="config writes require a config path",
            )
        return normalize_config_paths(
            new_config,
            config_path=app.state.config_path,
        )

    def wallet_key_path(current: AutomationConfig) -> Path:
        if os.environ.get(current.wallet.private_key_env):
            raise HTTPException(
                status_code=409,
                detail=(
                    "active wallet key comes from an environment variable; "
                    "unset it before managing a key file"
                ),
            )
        key_path = os.environ.get(current.wallet.private_key_file_env)
        if key_path:
            return Path(key_path).expanduser().resolve()
        if current.wallet.private_key_file is not None:
            return current.wallet.private_key_file
        if app.state.config_path is None:
            raise HTTPException(
                status_code=409,
                detail="wallet key file requires a config path",
            )
        return app.state.config_path.parent / "wallet.key"

    def wallet_payload() -> dict[str, Any]:
        current = app.state.config
        private_key = resolve_private_key(current)
        private_key_source = (
            resolve_private_key_source(current) if private_key else None
        )
        return {
            "execute_enabled": current.wallet.execute,
            "private_key_env": current.wallet.private_key_env,
            "private_key_file": (
                str(current.wallet.private_key_file)
                if current.wallet.private_key_file is not None
                else None
            ),
            "private_key_file_env": current.wallet.private_key_file_env,
            "private_key_source": private_key_source,
            "address": _wallet_address(private_key),
            "recipient": current.wallet.recipient,
        }

    def persist_wallet_key(
        private_key: str, *, overwrite: bool
    ) -> dict[str, Any]:
        if app.state.config_path is None:
            raise HTTPException(
                status_code=409,
                detail="wallet key writes require a config path",
            )
        current = app.state.config
        path = wallet_key_path(current)
        _write_private_key_file(path, private_key, overwrite=overwrite)

        wallet_update = current.wallet.model_dump()
        if os.environ.get(current.wallet.private_key_file_env) is None:
            wallet_update["private_key_file"] = path
        wallet_update["execute"] = False
        new_wallet = WalletConfig.model_validate(wallet_update)
        new_config = normalized_for_persist(
            current.model_copy(update={"wallet": new_wallet}, deep=True)
        )
        persist_config(new_config)
        return wallet_payload()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _index_html()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        current = app.state.config
        return {
            "status": "ok",
            "execute_enabled": current.wallet.execute,
            "rules": len(current.rules),
        }

    @app.get("/rules")
    async def rules(
        _admin: None = Depends(_require_admin_token),
    ) -> list[dict[str, Any]]:
        return [rule.model_dump(mode="json") for rule in app.state.config.rules]

    @app.put("/rules/{rule_id}")
    async def upsert_rule(
        rule_id: str,
        rule: RuleConfig,
        _admin: None = Depends(_require_admin_token),
    ) -> dict[str, Any]:
        if rule.id != rule_id:
            raise HTTPException(
                status_code=400,
                detail="rule id in path and body must match",
            )
        current = app.state.config
        rules_by_id = {item.id: item for item in current.rules}
        rules_by_id[rule_id] = rule
        new_config = current.model_copy(
            update={"rules": list(rules_by_id.values())},
            deep=True,
        )
        persist_config(new_config)
        return rule.model_dump(mode="json")

    @app.delete("/rules/{rule_id}")
    async def delete_rule(
        rule_id: str,
        _admin: None = Depends(_require_admin_token),
    ) -> dict[str, Any]:
        current = app.state.config
        rules = [rule for rule in current.rules if rule.id != rule_id]
        if len(rules) == len(current.rules):
            raise HTTPException(status_code=404, detail="rule not found")
        new_config = current.model_copy(update={"rules": rules}, deep=True)
        persist_config(new_config)
        return {"deleted": rule_id}

    @app.get("/runs")
    async def runs(
        limit: int = 100,
        _admin: None = Depends(_require_admin_token),
    ) -> list[dict[str, Any]]:
        return app.state.store.list_runs(limit=max(1, min(limit, 500)))

    @app.get("/wallet")
    async def wallet(
        _admin: None = Depends(_require_admin_token),
    ) -> dict[str, Any]:
        return wallet_payload()

    @app.patch("/wallet")
    async def update_wallet(
        payload: dict[str, Any],
        _admin: None = Depends(_require_admin_token),
    ) -> dict[str, Any]:
        if "private_key_file" in payload:
            raise HTTPException(
                status_code=400,
                detail="wallet private_key_file must be changed through config or environment",
            )
        current = app.state.config
        wallet_update = current.wallet.model_dump()
        for key in ("execute", "recipient"):
            if key in payload:
                wallet_update[key] = payload[key]
        new_wallet = WalletConfig.model_validate(wallet_update)
        new_config = normalized_for_persist(
            current.model_copy(update={"wallet": new_wallet}, deep=True)
        )
        persist_config(new_config)
        return wallet_payload()

    @app.post("/wallet/generate")
    async def generate_wallet_key(
        payload: dict[str, Any],
        _admin: None = Depends(_require_admin_token),
    ) -> dict[str, Any]:
        return persist_wallet_key(
            _generate_private_key(),
            overwrite=bool(payload.get("overwrite")),
        )

    @app.post("/wallet/import")
    async def import_wallet_key(
        payload: dict[str, Any],
        _admin: None = Depends(_require_admin_token),
    ) -> dict[str, Any]:
        return persist_wallet_key(
            _validate_private_key(str(payload.get("private_key", ""))),
            overwrite=bool(payload.get("overwrite")),
        )

    @app.get("/config.yaml", response_class=PlainTextResponse)
    async def get_config_yaml(
        _admin: None = Depends(_require_admin_token),
    ) -> str:
        return render_config(app.state.config)

    @app.put("/config.yaml")
    async def put_config_yaml(
        body: str = Body(media_type="text/plain"),
        _admin: None = Depends(_require_admin_token),
    ) -> dict[str, Any]:
        config_path_value = app.state.config_path
        if config_path_value is None:
            raise HTTPException(
                status_code=409,
                detail="config writes require a config path",
            )
        try:
            new_config = parse_config_text(body, config_path=config_path_value)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if _wallet_key_source_changed(app.state.config, new_config):
            raise HTTPException(
                status_code=400,
                detail=(
                    "wallet key source fields must be changed through local "
                    "config or environment, not the admin API"
                ),
            )
        persist_config(new_config)
        return {"saved": True}

    @app.post("/evaluate/{pair_id}")
    async def evaluate(
        pair_id: int,
        _admin: None = Depends(_require_admin_token),
    ) -> list[dict[str, Any]]:
        try:
            return await app.state.worker.evaluate_pair_once(pair_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app
