import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from xian_dex_automation.config import load_config
from xian_dex_automation.dex import resolve_private_key_source


@pytest.mark.parametrize(
    ("env", "kind", "source"),
    [
        ({}, "file", "config/keeper.key"),
        ({"CUSTOM_FILE": "override.key"}, "file", "override.key"),
        (
            {"CUSTOM_KEY": "secret-sentinel", "CUSTOM_FILE": "override.key"},
            "environment",
            "CUSTOM_KEY",
        ),
        ({"CUSTOM_KEY": "", "CUSTOM_FILE": ""}, "file", "config/keeper.key"),
    ],
)
def test_cli_reports_runtime_credential_precedence_without_reading_keys(
    tmp_path, monkeypatch, env, kind, source
):
    config_path = tmp_path / "config/automation.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        "wallet:\n  private_key_file: keeper.key\n"
        "  private_key_env: CUSTOM_KEY\n  private_key_file_env: CUSTOM_FILE\n"
    )
    runtime_env = {k: v for k, v in os.environ.items() if not k.startswith("CUSTOM_")}
    runtime_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "xian_dex_automation.credentials", "--config", str(config_path)],
        cwd=tmp_path,
        env=runtime_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    expected_source = str(tmp_path / source) if kind == "file" else source
    assert json.loads(result.stdout) == {"kind": kind, "source": expected_source}
    assert "secret-sentinel" not in result.stdout + result.stderr
    # The CLI succeeds before any key file exists. Runtime selects the same source.
    if kind == "file":
        Path(expected_source).write_text("secret-sentinel")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", runtime_env)
    actual = resolve_private_key_source(load_config(config_path))
    assert (str(Path(actual).resolve()) if kind == "file" else actual) == expected_source


@pytest.mark.parametrize(
    "contents", ["{}", "wallet: [secret-sentinel]", "wallet: [secret-sentinel", None]
)
def test_cli_handles_unset_or_invalid_config_without_leaking_values(tmp_path, contents):
    path = tmp_path / "config.yaml"
    if contents is not None:
        path.write_text(contents)
    env = {k: v for k, v in os.environ.items() if not k.startswith("XIAN_DEX_AUTOMATION_")}
    result = subprocess.run(
        [sys.executable, "-m", "xian_dex_automation.credentials", "--config", str(path)],
        env=env,
        capture_output=True,
        text=True,
    )
    if contents == "{}":
        assert result.returncode == 0
        assert json.loads(result.stdout) == {"kind": None, "source": None}
    else:
        assert result.returncode != 0
        assert "secret-sentinel" not in result.stdout + result.stderr
        assert "Traceback" not in result.stderr


def test_default_environment_overrides_config_file_without_reading_it(tmp_path, monkeypatch):
    from xian_dex_automation.credentials import describe_credential_source

    path = tmp_path / "config.yaml"
    path.write_text("wallet:\n  private_key_file: configured.key\n")
    monkeypatch.setenv("XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE", "override.key")
    monkeypatch.delenv("XIAN_DEX_AUTOMATION_PRIVATE_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert describe_credential_source(path) == {
        "kind": "file",
        "source": str(tmp_path / "override.key"),
    }
    monkeypatch.setenv("XIAN_DEX_AUTOMATION_PRIVATE_KEY", "secret-sentinel")
    assert describe_credential_source(path) == {
        "kind": "environment",
        "source": "XIAN_DEX_AUTOMATION_PRIVATE_KEY",
    }


@pytest.mark.parametrize("contents", ["{}", "wallet: [secret-sentinel"])
def test_cli_exit_status_and_sanitized_output(tmp_path, monkeypatch, capsys, contents):
    from xian_dex_automation.credentials import main

    path = tmp_path / "config.yaml"
    path.write_text(contents)
    monkeypatch.delenv("XIAN_DEX_AUTOMATION_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE", raising=False)
    result = main(["--config", str(path)])
    output = capsys.readouterr().out
    assert result == (0 if contents == "{}" else 1)
    assert "secret-sentinel" not in output
    assert json.loads(output) == (
        {"kind": None, "source": None} if result == 0 else {"error": "configuration unavailable"}
    )


def test_cli_requires_explicit_config():
    from xian_dex_automation.credentials import main

    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
