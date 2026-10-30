"""Select credential sources without reading or exposing private key material."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path

from yaml import YAMLError

from .config import AutomationConfig, load_config


def credential_source(
    config: AutomationConfig, env: Mapping[str, str] | None = None
) -> tuple[str | None, str | None]:
    environment = os.environ if env is None else env
    wallet = config.wallet
    if environment.get(wallet.private_key_env):
        return "environment", wallet.private_key_env
    key_file = environment.get(wallet.private_key_file_env) or wallet.private_key_file
    if key_file is not None:
        return "file", str(key_file)
    return None, None


def describe_credential_source(config_path: str | Path) -> dict[str, str | None]:
    kind, source = credential_source(load_config(config_path))
    if kind == "file":
        source = str(Path(source).resolve())
    return {"kind": kind, "source": source}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        description = describe_credential_source(args.config)
    except OSError, ValueError, TypeError, YAMLError:
        # Validation errors may contain supplied values. Never serialize them.
        print(json.dumps({"error": "configuration unavailable"}))
        return 1
    print(json.dumps(description))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
