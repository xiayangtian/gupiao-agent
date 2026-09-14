from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/superpowers/DESIGN-REGISTRY.md"
SPECS = ROOT / "docs/superpowers/specs"
STATUSES = {"待实现", "实施中", "已实现", "待核实"}
EXECUTABILITY = {"可直接执行", "先补计划", "—"}
UNFINISHED_STATUS_RANK = {"待实现": 0, "实施中": 1, "待核实": 2}
ANCHOR_RE = re.compile(r'<a id="([a-z0-9-]+)"></a>')
LINK_RE = re.compile(r"\]\(([^)]+)\)")


def _table_rows(section: str):
    return [line for line in section.splitlines() if line.startswith("|") and "---" not in line]


def _cells(row: str):
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _sections():
    text = REGISTRY.read_text(encoding="utf-8")
    unfinished, all_designs = text.split("## 全部设计", 1)
    return unfinished.split("## 未完成项目", 1)[1], all_designs


def _main_records():
    _, all_designs = _sections()
    records = []
    for row in _table_rows(all_designs):
        cells = _cells(row)
        assert len(cells) == 7, row
        anchor = ANCHOR_RE.match(cells[0])
        assert anchor and cells[0][anchor.end():], row
        records.append({
            "anchor": anchor.group(1),
            "title": cells[0][anchor.end():],
            "spec": cells[1],
            "plan": cells[2],
            "status": cells[3],
            "executability": cells[4],
            "evidence": cells[5],
        })
    return records


def _targets(cell: str, prefix: str):
    return [target for target in LINK_RE.findall(cell) if target.startswith(prefix)]


def test_every_design_spec_has_one_primary_registry_row_and_readable_links():
    records = _main_records()
    anchors = [record["anchor"] for record in records]
    assert len(anchors) == len(set(anchors))

    recorded_specs = [target for record in records for target in _targets(record["spec"], "specs/")]
    expected_specs = sorted(path.relative_to(REGISTRY.parent).as_posix() for path in SPECS.glob("*.md"))
    assert sorted(recorded_specs) == expected_specs
    assert len(recorded_specs) == len(set(recorded_specs))

    for record in records:
        for target in _targets(record["spec"], "specs/") + _targets(record["plan"], "plans/"):
            path = REGISTRY.parent / target
            assert path.is_file() and path.stat().st_size > 0, target
            assert path.open(encoding="utf-8").read(1), target


def test_unfinished_index_is_exact_projection_of_non_completed_main_records():
    unfinished, _ = _sections()
    index_records = []
    for row in _table_rows(unfinished):
        cells = _cells(row)
        assert len(cells) == 4, row
        match = re.fullmatch(r"\[主记录\]\(#([a-z0-9-]+)\)", cells[3])
        assert match, row
        index_records.append((cells[0], cells[1], cells[2], match.group(1)))

    main_records = _main_records()
    expected = [
        (record["title"], record["status"], record["executability"], record["anchor"])
        for record in main_records
        if record["status"] != "已实现"
    ]
    assert index_records == sorted(expected, key=lambda record: UNFINISHED_STATUS_RANK[record[1]])
    assert len(index_records) == len(set(index_records))
    assert all(anchor in {record["anchor"] for record in main_records} for *_, anchor in index_records)


def test_registry_enforces_status_executability_and_completion_evidence_contracts():
    for record in _main_records():
        assert record["status"] in STATUSES
        assert record["executability"] in EXECUTABILITY
        plan_targets = _targets(record["plan"], "plans/")

        if record["status"] == "待实现":
            assert record["executability"] == ("可直接执行" if plan_targets else "先补计划")
        else:
            assert record["executability"] == "—"

        if record["status"] == "已实现":
            merged_and_verified = (
                re.search(r"合并 [0-9a-f]{7,}", record["evidence"])
                and re.search(r"`python3 -m pytest [^`]+`[^|]*\b\d+ passed\b", record["evidence"])
            )
            user_confirmed_historical = (
                "用户确认 2026-09-14：历史需求已完成" in record["evidence"]
                and "原合并记录缺失" in record["evidence"]
            )
            assert merged_and_verified or user_confirmed_historical


def test_user_confirmed_historical_designs_are_completed_with_traceable_override():
    historical_titles = {
        "财报分析前端界面",
        "分析历史与图表",
        "财报分析智能体架构优化",
        "RAG 知识库与通用问答",
        "RAG 增强多维度分析",
        "RAG 查询重排序（rerank）",
        "问答接入 MCP 工具",
        "功能修复与 Git 隐私清理",
        "P0 可靠性修复",
        "动态证据化财报分析",
        "多轮问答与网页搜索",
        "渐进式财报报告阅读体验",
    }
    records = {record["title"]: record for record in _main_records()}
    assert {title for title in historical_titles if records[title]["status"] == "已实现"} == historical_titles
    for title in historical_titles:
        assert "用户确认 2026-09-14：历史需求已完成" in records[title]["evidence"]
        assert "原合并记录缺失" in records[title]["evidence"]


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
