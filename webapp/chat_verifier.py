"""Deterministic, fail-closed verification of answer claims."""
from __future__ import annotations

import re
from typing import Sequence

from webapp.chat_models import EvidenceArtifact, Fact, FactConflict, Scope, VerificationIssue, VerificationReport

_NUMBER_RE = re.compile(r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*(亿元|万元|元/股|百分比|倍|股|元)")
_MONEY_SCALES = {"元": 1e-8, "万元": 1e-4, "亿元": 1.0}


class ClaimVerifier:
    def verify(self, answer: str, scope: Scope, facts: Sequence[Fact], artifacts: Sequence[EvidenceArtifact], conflicts: Sequence[FactConflict]) -> VerificationReport:
        issues: list[VerificationIssue] = []
        supported: list[str] = []
        scoped_codes = {company.code for company in scope.companies}
        for fact in facts:
            if scope.mode != "whole_corpus" and fact.company_code not in scoped_codes:
                issues.append(VerificationIssue("scope_violation", "blocked", "事实超出本次问答范围。", fact.evidence_ids))
        for artifact in artifacts:
            if artifact.source == "pdf":
                invalid_url = artifact.pdf_url and not artifact.pdf_url.startswith("/api/history-pdf/")
                if artifact.page is None or artifact.page <= 0 or invalid_url:
                    issues.append(VerificationIssue("invalid_pdf_evidence", "blocked", "PDF 页码或链接不可核验。", artifact_ids=(artifact.report_id,)))
        for number, unit in _NUMBER_RE.findall(answer or ""):
            value = float(number) * _MONEY_SCALES.get(unit, 1.0)
            normalized_unit = "亿元" if unit in _MONEY_SCALES else unit
            matches = [fact for fact in facts if fact.unit == normalized_unit and abs(fact.value - value) <= 1e-6]
            if not matches:
                issues.append(VerificationIssue("unsupported_numeric_claim", "blocked", "回答中的数值没有可核验证据。"))
                continue
            for fact in matches:
                supported.extend(fact.evidence_ids)
                if fact.verification == "reference" and "外部参考" not in answer:
                    issues.append(VerificationIssue("external_reference_ambiguity", "partial", "外部数据必须标示为外部参考。", fact.evidence_ids))
                if fact.verification == "reference" and "已确认" in answer:
                    issues.append(VerificationIssue("reference_marked_verified", "partial", "外部参考不能表述为已确认事实。", fact.evidence_ids))
        for conflict in conflicts:
            if conflict.metric in answer and "存在口径/时间差异" not in answer:
                ids = tuple(evidence_id for fact in conflict.facts for evidence_id in fact.evidence_ids)
                issues.append(VerificationIssue("undisclosed_conflict", "partial", "相关事实存在口径/时间差异，回答必须披露。", ids))
        if any(issue.severity == "blocked" for issue in issues):
            status = "blocked"
        elif issues:
            status = "partial"
        else:
            status = "passed"
        return VerificationReport(status=status, issues=tuple(issues), supported_fact_ids=tuple(dict.fromkeys(supported)))
