from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/probe_chat_live.py"
CASES = Path(__file__).resolve().parents[1] / "fixtures/chat_runtime_eval_cases.json"


def _module():
    spec = importlib.util.spec_from_file_location("probe_chat_live", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _valid_args():
    return [
        "--allow-live", "--cases", str(CASES), "--providers", "openai",
        "--max-calls", "2", "--max-cost", "1.00", "--timeout", "10",
        "--account-profile", "non-production",
    ]


def test_live_probe_requires_all_explicit_limits_and_ci_refuses(monkeypatch):
    module = _module()
    monkeypatch.setenv("CI", "true")

    with pytest.raises(SystemExit):
        module.main([])
    assert module.main(_valid_args()) != 0


def test_live_probe_rejects_production_profile_and_unapproved_provider(capsys):
    module = _module()
    production = _valid_args()
    production[-1] = "production"
    assert module.main(production) != 0

    provider = _valid_args()
    provider[provider.index("openai")] = "unlisted"
    assert module.main(provider) != 0
    assert "provider" in capsys.readouterr().err.lower()


def test_valid_live_probe_configuration_is_dry_run_only(capsys, monkeypatch):
    module = _module()
    monkeypatch.delenv("CI", raising=False)

    assert module.main(_valid_args()) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["validated_only"] is True
    assert result["executed"] is False
    assert result["provider_count"] == 1
    assert result["case_count"] > 0
