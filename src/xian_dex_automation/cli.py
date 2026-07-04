from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from ipaddress import ip_address

from .config import load_config
from .service import ADMIN_TOKEN_ENV, configured_admin_token, create_app
from .storage import AutomationStore
from .worker import AutomationWorker


def is_loopback_host(host: str | None) -> bool:
    value = (host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def validate_serve_host(host: str) -> None:
    if not is_loopback_host(host) and configured_admin_token() is None:
        raise ValueError(
            f"non-loopback API binds require {ADMIN_TOKEN_ENV} to be set"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xian-dex-automation")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="path to config YAML",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate-config")

    worker = subparsers.add_parser("run-worker")
    worker.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--with-worker", action="store_true")

    return parser


async def run_worker(config_path: str) -> None:
    config = load_config(config_path)
    store = AutomationStore(config.database_path)
    worker = AutomationWorker(config, store)
    await worker.run_forever()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate-config":
        config = load_config(args.config)
        print(
            f"ok: {len(config.rules)} rule(s), "
            f"execute={config.wallet.execute}, db={config.database_path}"
        )
        return

    if args.command == "run-worker":
        logging.basicConfig(level=getattr(logging, args.log_level))
        asyncio.run(run_worker(args.config))
        return

    if args.command == "serve":
        import uvicorn

        try:
            validate_serve_host(args.host)
        except ValueError as exc:
            parser.error(str(exc))
        if configured_admin_token() is None:
            print(
                f"warning: {ADMIN_TOKEN_ENV} is not set; only / and /health "
                "will be usable until an admin token is configured",
                file=sys.stderr,
            )
        config = load_config(args.config)
        app = create_app(
            config,
            start_worker=args.with_worker,
            config_path=args.config,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
