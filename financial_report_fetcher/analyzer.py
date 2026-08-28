"""
analyzer — 财报分析编排层
=========================

基于 ai_client 构建，提供：
1. 配置驱动的分析维度模板（不改代码，增删维度）
2. 批量自动化分析 → 生成 Markdown + JSON 报告
3. 交互式问答

使用方式（CLI 入口）：
    python -m financial_report_fetcher analyze --pdf reports/xxx.pdf
    python -m financial_report_fetcher analyze --all
    python -m financial_report_fetcher chat --pdf reports/xxx.pdf

设计理念：
--------
每条分析维度 = 一个 Prompt 提示词 + 结构化输出约束（可选）
新增维度只需往 `ANALYSIS_TEMPLATES` 加配置，不需要改代码逻辑。
"""

import datetime
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Tuple

from financial_report_fetcher.ai_client import AIClient
from financial_report_fetcher.exceptions import AnalysisCancelledError

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# 预设分析维度模板
# ════════════════════════════════════════════════════════════════════

ANALYSIS_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "financial_summary": {
        "name": "财务摘要",
        "description": "提取营业收入、净利润、毛利率、净利率、ROE、每股收益等核心财务指标",
        "prompt": """请从以下财报内容中提取关键财务数据，以表格形式呈现。需要提取的指标包括：
1. 营业收入及同比变化
2. 归母净利润及同比变化
3. 扣非净利润及同比变化
4. 基本每股收益
5. 加权平均净资产收益率（ROE）
6. 经营活动现金流量净额及同比变化
7. 总资产和净资产
8. 毛利率和净利率

请逐项列出，逐项对齐数据。若某项数据在报告中未提供，请标明"数据未披露"。

输出格式要求（重要）：
- 请使用标准 Markdown 表格语法输出，形如：
  | 指标 | 数值 | 同比变化 |
  | --- | --- | --- |
  | 营业收入 | ... | ... |
- 关键变化数据用 **加粗** 标注，不要输出表格以外的冗余说明。
""",
        "retrieval": {
            "queries": [
                "营业收入 净利润 毛利率 ROE 每股收益 主要会计数据",
                "财务指标 同比 变化 经营现金流",
            ],
            "sections": ["财务报告", "指标"],
            "top_k": 8,
        },
        "schema": None,  # 可用 JSON Schema 约束输出
    },
    "risk_warning": {
        "name": "风险识别",
        "description": "识别报告中的风险信号：业绩下滑、现金流恶化、高负债、应收账款暴增等",
        "prompt": """请从以下财报内容中识别并分析潜在风险信号。重点关注：
1. 业绩下滑风险（收入/利润同比负增长）
2. 现金流风险（经营性现金流净额大幅下降）
3. 偿债风险（资产负债率过高、短期债务压力）
4. 营运风险（存货/应收账款/预付款项异常变化）
5. 业务集中度风险（过度依赖单一产品或地区）
6. 政策或合规风险

对于每个风险：
- 用 **严重** / **中等** / **较低** 标注风险等级
- 说明具体数据和判断依据
- 如有必要，提出建议关注的问题

输出格式要求（重要）：
- 每个风险使用 Markdown 三级标题（### 开头）作为小节标题，例如「### 1. 业绩下滑风险」
- 标题下使用无序列表（每行以 "- " 开头）逐项列出：风险等级、具体数据与依据、建议关注问题
- 避免大段连续段落文字，关键数据用 **加粗** 标注
""",
        "retrieval": {
            "queries": [
                "风险 下降 负债 现金流 存货 应收 减值",
                "偿债 营运 业绩下滑 风险因素",
            ],
            "sections": ["管理层讨论与分析", "财务报告", "风险"],
            "top_k": 8,
        },
        "schema": None,
    },
    "business_highlights": {
        "name": "经营亮点",
        "description": "寻找企业经营亮点：增长点、战略布局、核心竞争力变化",
        "prompt": """请从以下财报中提炼企业的核心经营亮点：
1. 业绩增长亮点（收入、利润、市占率等积极变化）
2. 业务与多元化布局（新业务进展、新产品、新市场）
3. 研发与创新（研发投入变化、资本化与费用化处理、专利等）
4. 股东回报（分红、回购）与估值
5. 核心竞争力（品牌、渠道、谁壁垒）
6. 管理层对未来的判断（行业趋势、公司发展战略层面的展望）

除正面亮点外，也对方方面面的趋势性变化保持敏感。

输出格式要求（重要）：
- 每个亮点使用 Markdown 三级标题（### 开头）作为小节标题，例如「### 1. 业绩增长亮点」
- 标题下使用无序列表（每行以 "- " 开头）逐项列出：亮点表现、具体数据、驱动因素
- 避免大段连续段落文字，关键数据用 **加粗** 标注
""",
        "retrieval": {
            "queries": [
                "增长 亮点 战略 研发 分红 新业务",
                "核心竞争力 经营情况 展望",
            ],
            "sections": ["管理层讨论与分析", "经营情况"],
            "top_k": 8,
        },
        "schema": None,
    },
    "profit_quality": {
        "name": "盈利质量",
        "description": "判断利润含金量：经营现金流匹配度、应收账款/存货变化、毛利率趋势",
        "prompt": """请从以下财报内容中分析企业的盈利质量（利润含金量）：
1. 净利润与经营现金流净额的匹配度（收现比 / 净利润现金含量）
2. 应收账款规模与增速是否异常（相对营收增速）
3. 存货变化趋势及跌价风险
4. 毛利率 / 净利率的变动趋势及驱动因素
5. 非经常性损益对利润的影响程度
6. 是否存在收入确认激进或利润操纵的信号

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题，例如「### 1. 现金流匹配度」
- 标题下使用无序列表（每行以 "- " 开头）逐项列出：现状、具体数据、风险判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "应收账款 存货 毛利率 经营现金流 利润质量",
                "收现比 经营现金流 净利润 含金量",
            ],
            "sections": ["财务报告", "附注", "指标"],
            "top_k": 8,
        },
        "schema": None,
    },
    "cashflow": {
        "name": "现金流分析",
        "description": "分析经营/投资/筹资现金流结构、造血能力与资金链风险",
        "prompt": """请从以下财报内容中分析企业的现金流量状况：
1. 经营活动现金流量净额及同比变化（造血能力）
2. 投资活动现金流：资本开支、购建长期资产、对外投资方向
3. 筹资活动现金流：借款、偿债、分红、股权融资结构
4. 现金及现金等价物净增加额与期末余额
5. 现金流结构是否健康（经营造血 vs 融资输血）
6. 资金链潜在风险（短债长投、大额投资缺口等）

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题
- 标题下使用无序列表逐项列出：金额、同比变化、判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "经营活动现金流量 投资活动 筹资活动 现金流量",
                "现金及现金等价物 净增加额",
            ],
            "sections": ["现金流量表", "财务报告"],
            "top_k": 8,
        },
        "schema": None,
    },
    "growth": {
        "name": "成长性",
        "description": "营收/利润增速、分板块增长、新市场与新业务拓展",
        "prompt": """请从以下财报内容中分析企业的成长性：
1. 营业收入 / 归母净利润的同比增速及趋势（近三年）
2. 分行业 / 分产品 / 分区域的增长结构
3. 新市场、新业务、新产品的拓展进展与贡献
4. 产能扩张、客户拓展、订单储备等前瞻性增长信号
5. 增长驱动因素与可持续性判断
6. 潜在增长瓶颈（市场空间、竞争加剧等）

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题
- 标题下使用无序列表逐项列出：增速数据、驱动因素、判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "同比增长 增速 新市场 新业务 分行业",
                "营业收入 净利润 增长 驱动",
            ],
            "sections": ["管理层讨论与分析", "经营情况"],
            "top_k": 8,
        },
        "schema": None,
    },
    "solvency": {
        "name": "偿债与资本结构",
        "description": "资产负债率、有息负债、流动比率等偿债能力与资本结构",
        "prompt": """请从以下财报内容中分析企业的偿债能力与资本结构：
1. 资产负债率及变动趋势
2. 有息负债规模与结构（短期/长期借款、应付债券等）
3. 流动比率 / 速动比率与短期偿债压力
4. 利息保障程度（息税前利润 / 利息费用）
5. 资本结构是否稳健（杠杆水平、股权融资 vs 债务融资）
6. 潜在偿债风险信号（债务集中到期、现金覆盖不足等）

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题
- 标题下使用无序列表逐项列出：指标数值、变动、风险判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "资产负债率 有息负债 流动比率 短期借款 长期借款",
                "偿债 资本结构 杠杆 债务",
            ],
            "sections": ["财务报告", "资产负债表"],
            "top_k": 8,
        },
        "schema": None,
    },
    "operation": {
        "name": "营运能力",
        "description": "应收/存货/总资产周转率等营运效率与变化",
        "prompt": """请从以下财报内容中分析企业的营运能力：
1. 应收账款周转率 / 周转天数及趋势
2. 存货周转率 / 周转天数及趋势
3. 总资产周转率及变动
4. 经营性资产（应收/存货/预付/应付）的占用效率
5. 营运效率变化反映的经营质量信号
6. 与同行业对比或历史趋势的偏离判断

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题
- 标题下使用无序列表逐项列出：指标数值、趋势、判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "周转率 应收账款周转 存货周转 总资产周转",
                "营运能力 应收账款 存货 周转天数",
            ],
            "sections": ["财务报告", "附注"],
            "top_k": 8,
        },
        "schema": None,
    },
    "governance": {
        "name": "公司治理",
        "description": "股权结构、董事会与高管、关联交易、内部控制",
        "prompt": """请从以下财报内容中分析公司的治理状况：
1. 股权结构：控股股东、实际控制人、股权集中度与质押情况
2. 董事会 / 监事会构成：独立董事比例、专业委员会设置
3. 高管变动与激励安排（股权激励、薪酬结构）
4. 关联交易规模、定价公允性与合规性
5. 内部控制评价与审计意见
6. 治理风险信号（一股独大、信息披露问题、违规处罚等）

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题
- 标题下使用无序列表逐项列出：事实、数据、风险判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "股权 董事会 高管 关联交易 内部控制",
                "治理 股东 监事 独立董事 信息披露",
            ],
            "sections": ["公司治理"],
            "top_k": 8,
        },
        "schema": None,
    },
    "shareholder_return": {
        "name": "股东回报",
        "description": "分红率、每股股利、回购等股东回报措施",
        "prompt": """请从以下财报内容中分析企业的股东回报：
1. 现金分红方案：每股股利、分红总额、分红率（股利支付率）
2. 股息率估算（相对当前股价如需可说明假设）
3. 股票回购计划与执行情况
4. 股东回报的历史稳定性与趋势
5. 回报水平与盈利能力的匹配度
6. 未来回报展望（留存收益投向、承诺等）

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题
- 标题下使用无序列表逐项列出：方案、数据、判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "分红 股利 回购 每股派息",
                "股东回报 现金分红 股息率",
            ],
            "sections": ["重要事项", "财务报告"],
            "top_k": 8,
        },
        "schema": None,
    },
    "rnd_innovation": {
        "name": "研发与创新",
        "description": "研发投入规模、资本化率、专利与技术创新",
        "prompt": """请从以下财报内容中分析企业的研发与创新能力：
1. 研发投入总额、占营收比例及同比变化
2. 研发费用资本化率及会计处理合理性
3. 研发人员数量与占比
4. 专利、核心技术、在研项目进展
5. 研发成果的产业化与商业化情况
6. 创新能力对竞争力的支撑判断

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题
- 标题下使用无序列表逐项列出：投入数据、进展、判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "研发投入 资本化 专利 研发费用",
                "研发人员 技术创新 核心专利",
            ],
            "sections": ["管理层讨论与分析"],
            "top_k": 8,
        },
        "schema": None,
    },
    "industry_competition": {
        "name": "行业与竞争",
        "description": "行业地位、市场份额、竞争格局与核心竞争力",
        "prompt": """请从以下财报内容中分析企业所处的行业与竞争格局：
1. 行业规模、景气度与政策环境
2. 公司行业地位与市场份额
3. 主要竞争对手与竞争格局变化
4. 核心竞争力（技术、品牌、渠道、成本、资源等）
5. 行业壁垒与潜在进入者威胁
6. 竞争态势对公司战略与业绩的影响

输出格式要求（重要）：
- 每个维度使用 Markdown 三级标题（### 开头）作为小节标题
- 标题下使用无序列表逐项列出：事实、数据、判断
- 数据缺失时明确标注"数据未披露"，不得编造
- 关键数据用 **加粗** 标注""",
        "retrieval": {
            "queries": [
                "行业 市场份额 竞争 地位 市占率",
                "竞争格局 行业地位 龙头",
            ],
            "sections": ["管理层讨论与分析", "经营情况"],
            "top_k": 8,
        },
        "schema": None,
    },
    "custom": {
        "name": "自定义分析",
        "description": "用户可自由输入分析需求",
        "prompt": None,  # 用户提问时动态输入
        "schema": None,
    },
}


# ════════════════════════════════════════════════════════════════════
# 结构化指标抽取（折线图数据源）
# ════════════════════════════════════════════════════════════════════

METRICS_FIELDS = [
    ("revenue", "营业收入（亿元）"),
    ("net_profit", "净利润（亿元）"),
    ("roe", "净资产收益率 ROE（%）"),
    ("gross_margin", "毛利率（%）"),
    ("debt_ratio", "资产负债率（%）"),
]

METRICS_PROMPT = (
    "请从以下财务报告内容中提取核心财务指标，仅输出 JSON，不要输出其他文字。\n"
    "JSON 格式：{\"metrics\": [{\"year\": 年份, \"revenue\": 数值或null, "
    "\"net_profit\": 数值或null, \"roe\": 数值或null, \"gross_margin\": 数值或null, "
    "\"debt_ratio\": 数值或null}, ...]}\n"
    "要求：\n"
    "1. year 必须是整数年份（如 2025）；只列出报告中确认出现的年份。\n"
    "2. revenue 与 net_profit 单位为亿元；roe/gross_margin/debt_ratio 为百分数数值（如 16.2 表示 16.2%）。\n"
    "3. 报告中未出现的字段用 null，严禁编造数据。\n"
    "4. 输出必须严格为单个 JSON 对象。"
)

# ════════════════════════════════════════════════════════════════════
# 数据类型定义
# ════════════════════════════════════════════════════════════════════


@dataclass
class DimensionResult:
    """单个分析维度的结果"""

    dimension_id: str           # 维度 ID，如 "financial_summary"
    dimension_name: str         # 显示名称，如 "财务摘要"
    content: str                # AI 回复的文本内容
    tokens: int = 0             # 该维度消耗的 token 数
    error: Optional[str] = None # 如果该维度分析时异常记录


@dataclass
class AnalysisReport:
    """一次完整的分析任务产出的报告体"""
    # 元信息
    source_file: str            # 原始 PDF 路径
    company: str                # 公司代码或简称
    report_year: int             # 年份
    period: Optional[str] = None # 报告期（YYYY-MM-DD）

    # 分析结果
    dimensions: List[DimensionResult] = field(default_factory=list)
    metrics: Optional[List[Dict[str, Any]]] = None

    # 执行记录
    timestamp: datetime.datetime = field(default_factory=datetime.datetime.now)
    model: str = ""
    total_tokens: int = 0

    def add_dimension(self, dim: DimensionResult) -> None:
        """添加一个维度的分析结果"""
        self.dimensions.append(dim)
        self.total_tokens += dim.tokens

    # ── 序列化 ─────────────────────────────────────────────────────

    def to_markdown(self) -> str:
        """导出为 Markdown 格式报告文件"""
        lines: List[str] = [
            f"# {self.company} 财报分析报告",
            "",
            f"- **源文件**：{self.source_file}",
            f"- **分析时间**：{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- **AI 模型**：{self.model}",
            f"- **总 Token 消耗**：{self.total_tokens}",
            "",
            "---",
            "",
        ]

        for dim in self.dimensions:
            lines.append(f"## {dim.dimension_name}")
            lines.append("")
            content = (dim.content or "").strip()
            if not content:
                # 内容为空时给出"暂无对应数据"占位，避免导出空白段落
                hint = "> 📭 暂无对应数据"
                if dim.error:
                    hint += f"（{dim.error}）"
                lines.append(hint)
            elif dim.error:
                lines.append(f"> ❌ 分析失败：{dim.error}")
                lines.append("")
                lines.append(dim.content)
            else:
                lines.append(dim.content)
            lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    def to_json(self) -> Dict[str, Any]:
        """导出为结构化的 JSON 数据"""
        return {
            "meta": {
                "company": self.company,
                "source_file": self.source_file,
                "timestamp": self.timestamp.isoformat(),
                "model": self.model,
                "total_tokens": self.total_tokens,
                "period": self.period,
            },
            "dimensions": [
                {
                    "id": dim.dimension_id,
                    "name": dim.dimension_name,
                    "content": dim.content,
                    "error": dim.error,
                }
                for dim in self.dimensions
            ],
            "metrics": self.metrics,
        }

    def save(self, output_dir: str) -> str:
        """
        保存报告到指定目录。

        目录结构：
            reports/analysis/
            ├── 600519_2025_年报_分析报告.md
            └── 600519_2025_年报_分析报告.json

        Args:
            output_dir: 输出目录路径

        Returns:
            MD 文件的路径
        """
        # 安全文件名（替换特殊字符 + 合并连续下划线）
        safe_company = re.sub(r'[^\w]', '_', self.company)
        safe_company = re.sub(r'_+', '_', safe_company).strip('_')
        period_suffix = self.period or str(self.report_year)
        safe_title = f"{safe_company}_{period_suffix}_分析报告"

        md_path = os.path.join(output_dir, f"{safe_title}.md")
        json_path = os.path.join(output_dir, f"{safe_title}.json")

        os.makedirs(output_dir, exist_ok=True)

        # 写 Markdown
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(self.to_markdown())
        logger.info("Markdown 报告已保存：%s", md_path)

        # 写 JSON
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False, indent=2)
        logger.info("JSON 报告已保存：%s", json_path)

        return md_path


# ════════════════════════════════════════════════════════════════════
# 核心分析器
# ════════════════════════════════════════════════════════════════════


class ReportAnalyzer:
    """
    财报分析器 —— 通过 AI 对财报 PDF 内容进行多维度分析。

    职责：
    - 基于 AIClient 与中转站通信
    - 使用 AI 对文本做各项预设指标（PROMPT）的分析
    - 格式化输出 Markdown + JSON 报告
    """

    # 默认打开的分析维度
    # 默认分析维度（财务摘要/风险识别/经营亮点/盈利质量/现金流）
    # 可通过 config.yaml 的 rag.analysis_dimensions 覆盖
    DEFAULT_DIMENSIONS = [
        "financial_summary", "risk_warning", "business_highlights",
        "profit_quality", "cashflow",
    ]

    def __init__(self, client: AIClient, rag_analysis: Optional["RagAnalysis"] = None):
        """
        Args:
            client: AIClient 实例（注入，使得依赖外置）
            rag_analysis: 可选的 RAG 维度检索器；注入后 analyze() 对每个维度
                优先使用按维度检索的片段上下文，为空/异常时回退截断全文。
                缺省 None 时行为与未启用 RAG 完全一致（向后兼容）。
        """
        self.client = client
        self.rag_analysis = rag_analysis

    # ── 批量分析 ──────────────────────────────────────────────────

    def analyze(
        self,
        pdf_path: str,
        dimensions: Optional[List[str]] = None,
        model: Optional[str] = None,
        max_chars: int = 15000,
        meta: Optional[Dict[str, Any]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> AnalysisReport:
        """
        基于已下载的 PDF 文件进行全维度自动化分析。

        Args:
            pdf_path:    PDF 文件路径
            dimensions:  需要分析维度 ID 列表
                 默认使用全部维度（["financial_summary", "risk_warning", "inspection"]）
            model:       AI 模型的名称
            max_chars:   传给 LLM 的文本量的最大长度上限
                 有时 PDF 全文过长，超出模型的上下文长度，截取此处。
            meta:        可选显式元信息 {"ticker": str, "year": int, "company": str}。
                 传入时跳过对文件名的解析（Web 端使用，新文件名格式下
                 原正则解析不完整）；缺省时维持原文件名解析行为。
            stop_event:  可选取消信号（Web 端「停止分析」按钮使用）。
                 维度循环间检查，置位时抛 AnalysisCancelledError 终止分析。

        Return:
            AnalysisReport 对象，可用 .save() 直接导出到分析报告
        """
        if dimensions is None:
            dimensions = self.DEFAULT_DIMENSIONS

        logger.info("开始分析 %s [维度=%s, model=%s]", pdf_path, dimensions, model or self.client.default_model)

        # 第一步：提取 PDF 文本
        pdf_text = self._extract_pdf_text(pdf_path, max_chars=max_chars)
        if not pdf_text.strip():
            raise ValueError(f"PDF 文件为空或无法抽取文字：{pdf_path}")

        # 第二步：解析元信息（代码 + 年份 + 中文名称）
        if meta is not None:
            ticker = str(meta.get("ticker") or "")
            report_year = int(meta.get("year") or 0)
            company_name = str(meta.get("company") or "")
            period = str(meta.get("period") or "") or None
        else:
            ticker, report_year = self._parse_meta(pdf_path)
            company_name = self._extract_company_name(pdf_text) or ticker
            period = None
        display_name = f"{company_name}（{ticker}）" if company_name and company_name != ticker else ticker

        # 第三步：遍历维度逐个分析
        report = AnalysisReport(
            source_file=pdf_path,
            company=display_name,
            report_year=report_year,
            period=period,
            model=model or self.client.default_model,
        )

        # RAG 检索用的报告 ID（惰性推导，仅当注入 rag_analysis 且维度配置了 retrieval 时使用）
        rag_report_id: Optional[str] = None

        for dim_id in dimensions:
            if stop_event is not None and stop_event.is_set():
                raise AnalysisCancelledError("分析已由用户停止")
            dim_config = ANALYSIS_TEMPLATES.get(dim_id)
            if dim_config is None:
                logger.warning("跳过未知维度：%s", dim_id)
                continue

            logger.info("  执行维度：%s（%s）", dim_config["name"], dim_id)

            # 如果是自定义分析维度，跳过
            if dim_config["prompt"] is None:
                logger.info("  跳过自定义维度（无预设提示词）")
                continue

            # 构造 system prompt
            system_prompt = (
                "你是一位专业的金融分析师，擅长阅读和分析上市公司财务报告。\n"
                "请根据用户提供的财报内容，基于事实回答问题。\n"
                "如果内容不足以回答，请明确指出哪些数据缺失。\n"
                "注意：财报内容较长，请聚焦在关键财务数据和重要信息上。"
            )

            # RAG 增强：按维度定向检索片段，非空则替代截断全文（兜底见 spec §7）
            content_source = pdf_text
            if self.rag_analysis is not None:
                retrieval = dim_config.get("retrieval")
                if retrieval:
                    if rag_report_id is None:
                        rag_report_id = self._resolve_report_id(pdf_path, meta)
                    if rag_report_id:
                        try:
                            rag_context = self.rag_analysis.build_context(
                                report_id=rag_report_id,
                                dimension=dim_id,
                                queries=retrieval.get("queries") or [],
                                sections=retrieval.get("sections"),
                                top_k=retrieval.get("top_k"),
                            )
                        except Exception as exc:
                            logger.warning("维度 %s RAG 上下文构建失败：%s", dim_id, exc)
                            rag_context = None
                        if rag_context:
                            content_source = rag_context

            # 将 PDF 内容（或 RAG 检索片段）和分析需求合并在一条 user 消息中
            user_content = (
                f"以下是目标财务报告的内容（请基于此进行分析）：\n\n"
                f"--- 财报内容开始 ---\n"
                f"{content_source}\n"
                f"--- 财报内容结束 ---\n\n"
                f"以下是需要你完成的分析任务：\n"
                f"{dim_config['prompt']}"
            )

            # 模型空返回防护：最多调用 2 次（首次返回空内容时自动重试一次，
            # 覆盖 DeepSeek 推理模型偶发"仅产出思考过程、content 为空"的情况）
            resp: Optional[Dict[str, Any]] = None
            last_exc: Optional[Exception] = None
            dim_tokens = 0
            for attempt in range(2):
                if stop_event is not None and stop_event.is_set():
                    raise AnalysisCancelledError("分析已由用户停止")
                try:
                    resp = self.client.chat(
                        messages=[{"role": "user", "content": user_content}],
                        system=system_prompt,
                        model=model,
                    )
                except Exception as exc:
                    last_exc = exc
                    break
                # 单维度耗时较长，调用后再次检查停止信号
                if stop_event is not None and stop_event.is_set():
                    raise AnalysisCancelledError("分析已由用户停止")
                dim_tokens += int(resp.get("usage", {}).get("total_tokens", 0) or 0)
                if (resp.get("content") or "").strip():
                    break
                logger.warning(
                    "维度 '%s' 模型返回空内容（第 %d 次），自动重试",
                    dim_config["name"], attempt + 1,
                )

            if resp is None:
                # 模型调用异常：记录错误，前端展示失败原因
                logger.error("维度 '%s' 分析失败：%s", dim_id, last_exc, exc_info=last_exc)
                dim_result = DimensionResult(
                    dimension_id=dim_id,
                    dimension_name=dim_config["name"],
                    content="",
                    error=str(last_exc) if last_exc else "分析失败",
                )
            elif not (resp.get("content") or "").strip():
                # 重试后仍为空：不静默留白，先区分具体原因再标记，
                # 供前端"暂无对应数据"占位时把原因说清楚
                reasoning = (resp.get("reasoning") or "").strip()
                finish = resp.get("finish_reason")
                if reasoning:
                    # 推理模型偶发只输出思考过程、未生成正式答案
                    cause = "模型仅返回思考过程、未生成正式答案"
                elif finish == "length":
                    cause = "模型输出长度超限被截断"
                else:
                    cause = "模型未返回有效内容"
                logger.warning("维度 '%s' 重试后仍为空（原因：%s）", dim_config["name"], cause)
                dim_result = DimensionResult(
                    dimension_id=dim_id,
                    dimension_name=dim_config["name"],
                    content="",
                    tokens=dim_tokens,
                    error=f"{cause}，已自动重试一次仍为空，请重新分析",
                )
            else:
                dim_result = DimensionResult(
                    dimension_id=dim_id,
                    dimension_name=dim_config["name"],
                    content=resp["content"],
                    tokens=dim_tokens,
                )
                # 记录实际使用的模型名
                report.model = resp.get("model", report.model)

            report.add_dimension(dim_result)

        report.metrics, metrics_tokens = self._extract_metrics(
            pdf_text,
            model=report.model or model or self.client.default_model,
        )
        report.total_tokens += metrics_tokens

        logger.info(
            "分析完成 [维度数=%d, total_tokens=%d]",
            len(report.dimensions), report.total_tokens,
        )

        return report

    # ── 交互式问答 ────────────────────────────────────────────────

    def chat(self, pdf_path: str, max_chars: int = 10000) -> None:
        """
        进入交互模式，用户可以对指定财报自由提问。

        用法：在终端输入问题，输入 q 或 quit 退出
        """
        print(f"\n📄 加载财务报告：{pdf_path}")
        print("💬 交互问答模式。输入问题后 Enter 即可回答，输入 'q' 退出。\n")

        pdf_text = self._extract_pdf_text(pdf_path, max_chars=max_chars)
        if not pdf_text.strip():
            print("❌ 文件无法抽取对应文字，请检查 PDF 是否可读。")
            return

        history: List[Dict[str, str]] = []
        ticker, _ = self._parse_meta(pdf_path)
        company_name = self._extract_company_name(pdf_text) or ticker
        display = f"{company_name}（{ticker}）" if company_name != ticker else ticker

        while True:
            try:
                user_input = input(f"〔{display}〕问题 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input:
                continue
            if user_input.lower() in ("q", "quit"):
                break

            # 第一次对话时补 pdf context
            if not history:
                resp = self.client.chat_with_context(
                    pdf_content=pdf_text,
                    user_prompt=user_input,
                )
            else:
                # 后续对话用历史
                messages = [{"role":"system","content": self._build_system_prompt(pdf_text[:10000])}]
                messages.extend(history)
                messages.append({"role": "user", "content": user_input})
                resp = self.client.chat(messages)["content"]

            print(f"  🤖 {resp}")

            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": resp})

            # 历史的长度不可太长，保留最近4轮
            if len(history) > 8:
                history = history[-8:]

        print("\n再见！👋")

    # ── 非交互式问答（Web 端）──────────────────────────────

    def qa(
        self,
        pdf_path: str,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
        max_chars: int = 10000,
        model: Optional[str] = None,
    ) -> str:
        """
        非交互式单轮问答：提取 PDF 文本并带上历史消息请求 AI，
        返回模型回复文本。Web 端每次调用传入累积历史、自行维护会话。

        Args:
            pdf_path:  PDF 文件路径
            question:  用户当前问题
            history:   历史消息 [{"role", "content"}, ...]（不含 system）
            max_chars: PDF 文本截取上限
            model:     模型名，缺省使用 AIClient 默认

        Returns:
            模型回复文本
        """
        pdf_text = self._extract_pdf_text(pdf_path, max_chars=max_chars)
        if not pdf_text.strip():
            raise ValueError(f"PDF 文件为空或无法抽取文字：{pdf_path}")

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self._build_system_prompt(pdf_text[:10000])}
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": question})

        resp = self.client.chat(messages=messages, model=model)
        return resp["content"]

    # ── PDF 提取 ──────────────────────────────────────────────────

    def _resolve_report_id(self, pdf_path: str, meta: Optional[Dict[str, Any]]) -> Optional[str]:
        """推导 RAG 检索用的 report_id；无法推导返回 None（该维度回退截断全文）。

        优先级：meta.period 显式期次（归一化季度/半年报）→ 从 PDF 文件名解析
        → meta.ticker+year 默认年报。与 rag/ingest 的 report_id 命名保持一致，
        保证分析前自动摄取后检索能命中同一批片段。
        """
        if meta:
            ticker = str(meta.get("ticker") or "")
            period = str(meta.get("period") or "")
            if ticker and re.match(r"^\d{4}-\d{2}-\d{2}$", period):
                year, month = period[:4], period[5:7]
                if month == "06":
                    rtype, norm = "semi_annual", f"{year}-06-30"
                elif month in ("03", "09"):
                    # 季报文件名无法区分一/三季报，store 统一映射为 03-31
                    rtype, norm = "quarterly", f"{year}-03-31"
                else:
                    rtype, norm = "annual", f"{year}-12-31"
                return f"{ticker}:{norm}:{rtype}"
        try:
            from .rag.chunking import parse_pdf_report_id

            rid = parse_pdf_report_id(pdf_path)
            if rid:
                return rid
        except Exception:
            logger.warning("PDF 文件名解析 report_id 失败：%s", pdf_path)
        if meta:
            ticker = str(meta.get("ticker") or "")
            year = meta.get("year")
            if ticker and year:
                return f"{ticker}:{int(year)}-12-31:annual"
        return None

    def _extract_pdf_text(self, pdf_path: str, max_chars: int = 15000) -> str:
        """
        读取 PDF 文本。只从python pypdf 库读取。

        Args:
            pdf_path:  PDF 文件路径
            max_chars: 提取字符上限（超出会截取首部内容，根据模型自动判断）

        Return:
            PDF 文本内容
        """
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ImportError("请执行 pip install pypdf，以便读取 PDF 文件")

        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)

        # PDF 可能很大（143页），我们要保留关键部分：
        # - 前一部分: 标题、提示、重要内容页
        # - 会优先保留后一部分: 财务数据（报表）
        text_parts = []
        current_len = 0

        # 先读前 10 页（概览、业绩摘要、董事长致辞等关键部分）
        for i in range(min(10, total_pages)):
            p_text = reader.pages[i].extract_text() or ""
            text_parts.append(p_text)
            current_len += len(p_text)

            if current_len >= max_chars // 3:
                break

        # 如果页数较多，再从后面 10 页捞取财务数据表
        if total_pages > 20 and current_len < max_chars:
            for i in range(max(total_pages - 10, 10), total_pages):
                p_text = reader.pages[i].extract_text() or ""
                text_parts.append(p_text)
                current_len += len(p_text)
                if current_len >= max_chars:
                    break

        # 如果内容仍未达到上限，继续填充中间部分
        if current_len < max_chars:
            for i in range(10, min(total_pages - 10, 10 + 20)):
                if current_len >= max_chars:
                    break
                p_text = reader.pages[i].extract_text() or ""
                text_parts.append(p_text)
                current_len += len(p_text)

        full_text = "\n".join(text_parts)

        # 最终对 max_chars 做硬截断
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars]
            logger.info("PDF 内容超过 %d 字符（实际 %d），已截断", max_chars, len(full_text))

        logger.info("PDF 已读取：%s（%d / %d 页，%d 字符）",
                    pdf_path, current_len, total_pages, len(full_text))
        return full_text

    # ── 辅助解析 ────────────────────────────────────────────────

    @staticmethod
    def _parse_meta(pdf_path: str) -> Tuple[str, int]:
        """
        从日志提取的基础文件名解析公司名和年份。

        文件名格式示例：
           600519_年报_2025.pdf
           000651_年报_2024.pdf

        Return:
            (company, report_year)
            若未能解析，公司与年份分别为 ("unknown", 0)
        """
        basename = os.path.splitext(os.path.basename(pdf_path))[0]

        # 先尝试格式：{ticker}_年报_{year[-MM-DD]} 或 {company}_{ticker}_年报_{year[-MM-DD]}
        pattern = r"^(?:(?P<company>.+)_)?(?P<ticker>[^_]+)_\w+_(?P<year>\d{4})(?:-\d{2}-\d{2})?$"
        match = re.match(pattern, basename)
        if match:
            return match.group("ticker"), int(match.group("year"))

        # 后尝试：纯 ticker 的形式
        return basename, 0

    @staticmethod
    def _extract_company_name(pdf_text: str) -> Optional[str]:
        """
        从前几页 PDF 文本中提取公司中文名称。

        策略（依次尝试）：
        1. 查找「公司简称：XXX」行（A 股年报标准格式）
        2. 查找「XXX股份有限公司\\d{4}年」模式
        3. 回退 None

        示例：
            输入: "贵州茅台酒股份有限公司2025年年度报告\n...公司简称：贵州茅台"
            输出: "贵州茅台"

            输入: "珠海格力电器股份有限公司2025年年度报告"
            输出: "珠海格力电器"
        """
        # 策略 1：「公司简称：XXX」（最精确）
        m = re.search(r"公司简称[：:]\s*(\S+)", pdf_text[:1000])
        if m:
            return m.group(1).strip()

        # 策略 2：「XXX股份有限公司 XXXX年年度报告」（可能有空格）
        m = re.search(
            r"([一-鿿]{2,20})股份有限公司?\s*\d{4}年",
            pdf_text[:500],
        )
        if m:
            return m.group(1).strip()

        # 策略 3：「XXX有限公司 XXXX年年度报告」（可能有空格）
        m = re.search(
            r"([一-鿿]{2,20})有限公司\s*\d{4}年",
            pdf_text[:500],
        )
        if m:
            return m.group(1).strip()

        return None

    @staticmethod
    def _build_system_prompt(pdf_context: str) -> str:
        """构造带 PDF 上下文的 chat system prompt"""
        return (
            "你是一位专业的金融分析师，擅长阅读和分析上市公司财报。\n"
            "回答时应参考已附上的财务报告内容，客观、简洁、专业。\n"
            "如果财报内容中包含的信息无法直接回答用户问题，"
            "请明确告知哪些信息属于缺失。\n\n"
            f"【附】财务报告核心内容：\n\n{pdf_context}"
        )

    def _extract_metrics(
        self, pdf_text: str, model: Optional[str] = None
    ) -> Tuple[Optional[List[Dict[str, Any]]], int]:
        """
        从财报文本中抽取结构化指标（折线图数据源）。

        一次结构化调用（response_format=json_object，max_tokens=2000）。
        任何失败（网络/非法 JSON/解析失败）只返回 (None, 0)，不抛异常。

        Returns:
            [{"year": int, "revenue": float|None, "net_profit": float|None,
              "roe": float|None, "gross_margin": float|None,
              "debt_ratio": float|None}, ...] 按年升序；数据全部为空返回 None；第二项为消耗 token 数。
        """

        def _num(value: Any) -> Optional[float]:
            """尽力转数字：None/空串/非法字符串/非有限值 → None；字符串数字 → float"""
            if value is None or value == "":
                return None
            if isinstance(value, str):
                value = value.strip().replace(",", "")
                if value.endswith("%"):
                    value = value[:-1].strip()
                if not value:
                    return None
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            if value != value or value in (float("inf"), float("-inf")):
                return None
            return value

        try:
            resp = self.client.chat(
                messages=[{
                    "role": "user",
                    "content": f"{METRICS_PROMPT}\n\n--- 财报内容开始 ---\n{pdf_text}\n--- 财报内容结束 ---",
                }],
                model=model,
                max_tokens=2000,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            logger.warning("指标抽取失败：%s", exc)
            return None, 0

        metrics_tokens = int(resp.get("usage", {}).get("total_tokens", 0) or 0)
        try:
            payload = json.loads(resp["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("指标抽取结果解析失败：%s", exc)
            return None, metrics_tokens

        if not isinstance(payload, dict):
            return None, metrics_tokens
        rows = payload.get("metrics")
        if not isinstance(rows, list):
            return None, metrics_tokens

        now_year = datetime.date.today().year
        by_year: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                year = int(row.get("year"))
            except (TypeError, ValueError):
                continue
            if not (2000 <= year <= now_year + 1):
                continue

            cleaned = by_year.setdefault(
                year,
                {"year": year, **{field_name: None for field_name, _label in METRICS_FIELDS}},
            )
            updated = False
            for field_name, _label in METRICS_FIELDS:
                value = _num(row.get(field_name))
                if value is not None:
                    cleaned[field_name] = value
                    updated = True
            if not updated and all(cleaned[field_name] is None for field_name, _ in METRICS_FIELDS):
                by_year.pop(year, None)

        if not by_year:
            return None, metrics_tokens

        metrics = [by_year[year] for year in sorted(by_year)]
        if not any(
            any(row[field_name] is not None for field_name, _ in METRICS_FIELDS)
            for row in metrics
        ):
            return None, metrics_tokens
        return metrics, metrics_tokens

    def analyze_all_in_directory(
        self,
        report_dir: str,
        output_dir: Optional[str] = None,
        dimensions: Optional[List[str]] = None,
        model: Optional[str] = None,
    ) -> List[AnalysisReport]:
        """
        扫描目录下的所有 PDF 并批量分析。

        Args:
            report_dir:  PDF 所在的目录（如 reports/）
            output_dir:  分析报告输出目录（默认 subdir "analysis" under report_dir）
            dimensions:  需要分析的维度列表
            model:       模型名

        Return:
            分析完成的报告列表
        """
        if output_dir is None:
            output_dir = os.path.join(report_dir, "analysis")

        pdf_files = sorted([
            os.path.join(report_dir, f)
            for f in os.listdir(report_dir)
            if f.endswith(".pdf")
        ])

        if not pdf_files:
            logger.warning("目标目录未发现 PDF 文件'：%s", report_dir)
            return []

        logger.info("发现 %d 份 PDF，开始批量分析...", len(pdf_files))

        reports = []
        for pdf_path in pdf_files:
            try:
                report = self.analyze(
                    pdf_path=pdf_path,
                    dimensions=dimensions,
                    model=model,
                )
                report.save(output_dir)
                reports.append(report)
            except Exception as exc:
                logger.exception("分析失败：%s", pdf_path)

        logger.info("批量分析完成：成功 %d / 总计 %d", len(reports), len(pdf_files))
        return reports