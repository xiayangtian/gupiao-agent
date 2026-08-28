"""预提交敏感信息扫描器的回归测试。"""

import importlib.machinery
import importlib.util
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = ROOT / ".githooks" / "pre-commit"


@pytest.fixture()
def hook():
    loader = importlib.machinery.SourceFileLoader("gp_agent_pre_commit", str(HOOK_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_secret_inside_mapping_is_not_exempted(hook) -> None:
    secret = "sk-" + "A" * 24
    problems = hook.scan_lines([f'config = {{"api_key": "{secret}"}}'])

    assert any("API Key" in desc for _, desc, _ in problems)


def test_placeholder_secret_is_allowed(hook) -> None:
    problems = hook.scan_lines(['ai_api_key: "sk-' + "x" * 24 + '"'])

    assert problems == []


def test_real_email_and_local_user_paths_are_blocked(hook) -> None:
    email = "developer" + "@" + "private-domain.com"
    mac_path = "/" + "Users" + "/alice/project"
    linux_path = "/" + "home" + "/alice/project"

    problems = hook.scan_lines([email, mac_path, linux_path])
    descriptions = [desc for _, desc, _ in problems]

    assert any("邮箱" in desc for desc in descriptions)
    assert sum("本地用户目录" in desc for desc in descriptions) == 2


def test_example_email_is_allowed(hook) -> None:
    email = "developer" + "@" + "example.com"

    assert hook.scan_lines([email]) == []


def test_javascript_string_concatenation_is_not_a_hardcoded_token(hook) -> None:
    line = "html += '<span>Token: ' + meta.total_tokens + '</span>';"

    assert hook.scan_lines([line]) == []


def test_git_diff_failure_blocks_commit(hook, monkeypatch) -> None:
    result = SimpleNamespace(returncode=1, stdout="", stderr="git diff failed")
    monkeypatch.setattr(hook.subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(RuntimeError, match="git diff failed"):
        hook.staged_lines()


def test_tracked_text_files_do_not_contain_machine_specific_user_paths() -> None:
    """已跟踪文本不得携带具体的 macOS/Linux 用户目录。"""
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    local_path = re.compile(r"/(?:Users|home)/([^/\s`'\"]+)")
    allowed_placeholders = {"<name>", "..."}
    hits = []

    for raw_path in proc.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8")
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for match in local_path.finditer(line):
                if match.group(1) not in allowed_placeholders:
                    hits.append(f"{relative}:{line_no}")

    assert hits == []
