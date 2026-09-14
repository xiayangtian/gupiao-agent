from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/superpowers/DESIGN-REGISTRY.md"
SPECS = ROOT / "docs/superpowers/specs"


def _all_design_rows():
    text = REGISTRY.read_text(encoding="utf-8")
    section = text.split("## 全部设计", 1)[1]
    return [line for line in section.splitlines() if line.startswith("|") and "---" not in line]


def _spec_targets(row):
    return re.findall(r"\]\((specs/[^)]+\.md)\)", row)


def test_every_design_spec_has_one_primary_registry_row():
    rows = _all_design_rows()
    recorded = [target for row in rows for target in _spec_targets(row)]
    expected = sorted(path.relative_to(REGISTRY.parent).as_posix() for path in SPECS.glob("*.md"))
    assert sorted(recorded) == expected
    assert len(recorded) == len(set(recorded))


def test_registry_lists_unfinished_items_and_uses_only_allowed_values():
    text = REGISTRY.read_text(encoding="utf-8")
    assert text.index("## 未完成项目") < text.index("## 全部设计")
    assert "待实现" in text or "实施中" in text or "待核实" in text
    for row in _all_design_rows():
        assert sum(status in row for status in ("待实现", "实施中", "已实现", "待核实")) == 1
        assert sum(mode in row for mode in ("可直接执行", "先补计划", "—")) == 1


def test_project_workflow_defines_registry_updates_and_direct_execution():
    policy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for requirement in (
        "DESIGN-REGISTRY.md",
        "执行方案：<方案名称>",
        "实施中",
        "已实现",
        "验证",
    ):
        assert requirement in policy
