from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_config
from .service import create_app
from .storage import AutomationStore
from .worker import AutomationWorker


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
