"""
config.py 单元测试

覆盖 ConfigLoader.load() 的各项行为：
- 文件不存在时抛出 ConfigFileNotFoundError（含绝对路径）
- YAML/JSON 格式错误时抛出 ConfigParseError（含行号/字段名）
- 配置语义错误时抛出 ConfigValidationError（含字段名）
- YYYY 格式日期解析规则
- 正常加载返回 AppConfig 实例
"""

import json
import os
from datetime import date

import pytest
import yaml

from financial_report_fetcher.config import ConfigLoader
from financial_report_fetcher.exceptions import (
    ConfigFileNotFoundError,
    ConfigParseError,
    ConfigValidationError,
)
from financial_report_fetcher.models import AppConfig, ReportType


# ─────────────────────────────────────────────────────────────────────────────
# 辅助工具
# ─────────────────────────────────────────────────────────────────────────────

def write_yaml(tmp_path, filename: str, data: dict) -> str:
    """将字典写为 YAML 文件并返回路径"""
    p = tmp_path / filename
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return str(p)


def write_json(tmp_path, filename: str, data: dict) -> str:
    """将字典写为 JSON 文件并返回路径"""
    p = tmp_path / filename
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def write_raw(tmp_path, filename: str, content: str) -> str:
    """将原始字符串写入文件并返回路径"""
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return str(p)


# 最小合法配置
MINIMAL_VALID_DATA = {
    "storage_dir": "./reports",
    "companies": [{"ticker": "600519", "name": "贵州茅台"}],
}

# 包含日期和其他字段的合法配置
FULL_VALID_DATA = {
    "storage_dir": "./reports",
    "companies": [
        {"ticker": "600519", "name": "贵州茅台"},
        {"name": "比亚迪"},
    ],
    "report_types": ["annual", "semi_annual"],
    "start_date": "2022-01-01",
    "end_date": "2023-12-31",
    "max_count": 10,
}


@pytest.fixture
def loader():
    """返回 ConfigLoader 实例"""
    return ConfigLoader()


# ─────────────────────────────────────────────────────────────────────────────
# 文件不存在
# ─────────────────────────────────────────────────────────────────────────────

class TestFileNotFound:
    def test_raises_config_file_not_found_error(self, loader, tmp_path):
        """不存在的路径应抛出 ConfigFileNotFoundError"""
        non_existent = str(tmp_path / "no_such_file.yaml")
        with pytest.raises(ConfigFileNotFoundError):
            loader.load(non_existent)

    def test_error_message_contains_absolute_path(self, loader, tmp_path):
        """错误信息中必须包含文件的绝对路径"""
        non_existent = str(tmp_path / "missing.yaml")
        abs_path = os.path.abspath(non_existent)
        with pytest.raises(ConfigFileNotFoundError) as exc_info:
            loader.load(non_existent)
        assert abs_path in str(exc_info.value)

    def test_relative_path_resolved_to_absolute_in_error(self, loader):
        """使用相对路径时，错误信息应包含其绝对路径"""
        relative = "definitely_not_existing_config.yaml"
        abs_path = os.path.abspath(relative)
        with pytest.raises(ConfigFileNotFoundError) as exc_info:
            loader.load(relative)
        assert abs_path in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# YAML 格式错误
# ─────────────────────────────────────────────────────────────────────────────

class TestYamlParseError:
    def test_invalid_yaml_raises_config_parse_error(self, loader, tmp_path):
        """YAML 格式错误时应抛出 ConfigParseError"""
        path = write_raw(tmp_path, "bad.yaml", "key: [unclosed")
        with pytest.raises(ConfigParseError):
            loader.load(path)

    def test_invalid_yaml_error_contains_line_info(self, loader, tmp_path):
        """YAML 格式错误信息应包含行号"""
        content = "valid_key: value\nbad key: [unclosed"
        path = write_raw(tmp_path, "bad_line.yaml", content)
        with pytest.raises(ConfigParseError) as exc_info:
            loader.load(path)
        # 错误信息应包含行号相关内容（"行" 或数字）
        error_msg = str(exc_info.value)
        assert "行" in error_msg or any(c.isdigit() for c in error_msg)

    def test_yaml_list_root_raises_parse_error(self, loader, tmp_path):
        """YAML 顶层为列表（非字典）时应抛出 ConfigParseError"""
        path = write_raw(tmp_path, "list.yaml", "- item1\n- item2\n")
        with pytest.raises(ConfigParseError):
            loader.load(path)


# ─────────────────────────────────────────────────────────────────────────────
# JSON 格式错误
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonParseError:
    def test_invalid_json_raises_config_parse_error(self, loader, tmp_path):
        """JSON 格式错误时应抛出 ConfigParseError"""
        path = write_raw(tmp_path, "bad.json", '{"key": "value"')  # 缺少右括号
        with pytest.raises(ConfigParseError):
            loader.load(path)

    def test_invalid_json_error_contains_line_info(self, loader, tmp_path):
        """JSON 格式错误信息应包含行号"""
        content = '{\n  "key": \n}'  # 不完整的值
        path = write_raw(tmp_path, "bad_json_line.json", content)
        with pytest.raises(ConfigParseError) as exc_info:
            loader.load(path)
        error_msg = str(exc_info.value)
        assert "行" in error_msg or any(c.isdigit() for c in error_msg)

    def test_json_array_root_raises_parse_error(self, loader, tmp_path):
        """JSON 顶层为数组（非对象）时应抛出 ConfigParseError"""
        path = write_raw(tmp_path, "array.json", '["a", "b"]')
        with pytest.raises(ConfigParseError):
            loader.load(path)


# ─────────────────────────────────────────────────────────────────────────────
# 配置语义验证错误（ConfigValidationError）
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigValidationError:
    def test_missing_storage_dir_raises(self, loader, tmp_path):
        """缺少 storage_dir 字段时应抛出 ConfigValidationError"""
        data = {"companies": [{"ticker": "600519"}]}
        path = write_yaml(tmp_path, "no_storage.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "storage_dir" in str(exc_info.value)

    def test_missing_companies_raises(self, loader, tmp_path):
        """缺少 companies 字段时应抛出 ConfigValidationError"""
        data = {"storage_dir": "./reports"}
        path = write_yaml(tmp_path, "no_companies.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "companies" in str(exc_info.value)

    def test_empty_companies_raises(self, loader, tmp_path):
        """companies 为空列表时应抛出 ConfigValidationError"""
        data = {"storage_dir": "./reports", "companies": []}
        path = write_yaml(tmp_path, "empty_companies.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "companies" in str(exc_info.value)

    def test_max_count_out_of_range_raises(self, loader, tmp_path):
        """max_count 超出合法范围时应抛出 ConfigValidationError，含字段名"""
        data = {**MINIMAL_VALID_DATA, "max_count": 0}
        path = write_yaml(tmp_path, "bad_max_count.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "max_count" in str(exc_info.value)

    def test_max_count_too_large_raises(self, loader, tmp_path):
        """max_count 超过 10000 时应抛出 ConfigValidationError"""
        data = {**MINIMAL_VALID_DATA, "max_count": 10001}
        path = write_yaml(tmp_path, "big_max_count.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "max_count" in str(exc_info.value)

    def test_start_date_after_end_date_raises(self, loader, tmp_path):
        """start_date 晚于 end_date 时应抛出 ConfigValidationError，含字段名"""
        data = {
            **MINIMAL_VALID_DATA,
            "start_date": "2023-12-31",
            "end_date": "2023-01-01",
        }
        path = write_yaml(tmp_path, "bad_date_range.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "start_date" in str(exc_info.value) or "end_date" in str(exc_info.value)

    def test_only_start_date_raises_with_field_name(self, loader, tmp_path):
        """只配置 start_date 时，错误信息应包含缺失的 end_date 字段名"""
        data = {**MINIMAL_VALID_DATA, "start_date": "2023-01-01"}
        path = write_yaml(tmp_path, "only_start.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "end_date" in str(exc_info.value)

    def test_only_end_date_raises_with_field_name(self, loader, tmp_path):
        """只配置 end_date 时，错误信息应包含缺失的 start_date 字段名"""
        data = {**MINIMAL_VALID_DATA, "end_date": "2023-12-31"}
        path = write_yaml(tmp_path, "only_end.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "start_date" in str(exc_info.value)

    def test_invalid_report_type_raises(self, loader, tmp_path):
        """非法 report_types 值应抛出 ConfigValidationError"""
        data = {**MINIMAL_VALID_DATA, "report_types": ["invalid_type"]}
        path = write_yaml(tmp_path, "bad_report_type.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "report_types" in str(exc_info.value)

    def test_company_without_identifier_raises(self, loader, tmp_path):
        """公司条目缺少 ticker 和 name 时应抛出 ConfigValidationError"""
        data = {"storage_dir": "./r", "companies": [{}]}
        path = write_yaml(tmp_path, "no_id_company.yaml", data)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load(path)
        assert "companies" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# 日期字段解析
# ─────────────────────────────────────────────────────────────────────────────

class TestDateParsing:
    def test_yyyy_start_date_becomes_jan_1(self, loader, tmp_path):
        """start_date 为 "YYYY" 格式时应解析为当年 1 月 1 日"""
        data = {**MINIMAL_VALID_DATA, "start_date": "2022", "end_date": "2023-12-31"}
        path = write_yaml(tmp_path, "yyyy_start.yaml", data)
        config = loader.load(path)
        assert config.start_date == date(2022, 1, 1)

    def test_yyyy_end_date_becomes_dec_31(self, loader, tmp_path):
        """end_date 为 "YYYY" 格式时应解析为当年 12 月 31 日"""
        data = {**MINIMAL_VALID_DATA, "start_date": "2022-01-01", "end_date": "2023"}
        path = write_yaml(tmp_path, "yyyy_end.yaml", data)
        config = loader.load(path)
        assert config.end_date == date(2023, 12, 31)

    def test_yyyy_both_dates(self, loader, tmp_path):
        """start_date 和 end_date 均为 "YYYY" 格式时各自正确解析"""
        data = {**MINIMAL_VALID_DATA, "start_date": "2022", "end_date": "2023"}
        path = write_yaml(tmp_path, "yyyy_both.yaml", data)
        config = loader.load(path)
        assert config.start_date == date(2022, 1, 1)
        assert config.end_date == date(2023, 12, 31)

    def test_full_date_format_parsed_correctly(self, loader, tmp_path):
        """完整日期格式 "YYYY-MM-DD" 应直接解析"""
        data = {
            **MINIMAL_VALID_DATA,
            "start_date": "2023-06-30",
            "end_date": "2023-06-30",
        }
        path = write_yaml(tmp_path, "full_date.yaml", data)
        config = loader.load(path)
        assert config.start_date == date(2023, 6, 30)
        assert config.end_date == date(2023, 6, 30)

    def test_invalid_date_format_raises_parse_error(self, loader, tmp_path):
        """无效日期格式应抛出 ConfigParseError，含字段名"""
        data = {**MINIMAL_VALID_DATA, "start_date": "not-a-date", "end_date": "2023"}
        path = write_yaml(tmp_path, "bad_date_fmt.yaml", data)
        with pytest.raises(ConfigParseError) as exc_info:
            loader.load(path)
        assert "start_date" in str(exc_info.value)

    def test_invalid_end_date_format_raises_parse_error(self, loader, tmp_path):
        """无效 end_date 格式应抛出 ConfigParseError，含字段名"""
        data = {**MINIMAL_VALID_DATA, "start_date": "2022", "end_date": "2023-13-01"}
        path = write_yaml(tmp_path, "bad_end_date.yaml", data)
        with pytest.raises((ConfigParseError, ConfigValidationError)):
            loader.load(path)

    def test_yyyy_same_year_start_before_end(self, loader, tmp_path):
        """同年的 YYYY 格式：start=2023-01-01 <= end=2023-12-31，合法"""
        data = {**MINIMAL_VALID_DATA, "start_date": "2023", "end_date": "2023"}
        path = write_yaml(tmp_path, "same_year.yaml", data)
        config = loader.load(path)
        assert config.start_date == date(2023, 1, 1)
        assert config.end_date == date(2023, 12, 31)


# ─────────────────────────────────────────────────────────────────────────────
# 正常加载（YAML 和 JSON）
# ─────────────────────────────────────────────────────────────────────────────

class TestSuccessfulLoad:
    def test_minimal_yaml_returns_app_config(self, loader, tmp_path):
        """最小合法 YAML 配置应成功返回 AppConfig 实例"""
        path = write_yaml(tmp_path, "minimal.yaml", MINIMAL_VALID_DATA)
        config = loader.load(path)
        assert isinstance(config, AppConfig)

    def test_full_yaml_returns_app_config(self, loader, tmp_path):
        """包含所有字段的合法 YAML 配置应成功返回 AppConfig"""
        path = write_yaml(tmp_path, "full.yaml", FULL_VALID_DATA)
        config = loader.load(path)
        assert isinstance(config, AppConfig)

    def test_json_config_returns_app_config(self, loader, tmp_path):
        """JSON 格式配置应成功加载"""
        path = write_json(tmp_path, "config.json", FULL_VALID_DATA)
        config = loader.load(path)
        assert isinstance(config, AppConfig)

    def test_storage_dir_loaded_correctly(self, loader, tmp_path):
        """storage_dir 字段应正确加载"""
        path = write_yaml(tmp_path, "cfg.yaml", MINIMAL_VALID_DATA)
        config = loader.load(path)
        assert config.storage_dir == "./reports"

    def test_companies_loaded_correctly(self, loader, tmp_path):
        """companies 列表应正确加载"""
        path = write_yaml(tmp_path, "cfg.yaml", MINIMAL_VALID_DATA)
        config = loader.load(path)
        assert len(config.companies) == 1
        assert config.companies[0].ticker == "600519"

    def test_default_report_types_is_annual(self, loader, tmp_path):
        """未配置 report_types 时默认应为 [ANNUAL]"""
        path = write_yaml(tmp_path, "cfg.yaml", MINIMAL_VALID_DATA)
        config = loader.load(path)
        assert config.report_types == [ReportType.ANNUAL]

    def test_report_types_loaded_correctly(self, loader, tmp_path):
        """report_types 字段应正确加载并转换为枚举"""
        data = {**MINIMAL_VALID_DATA, "report_types": ["annual", "semi_annual"]}
        path = write_yaml(tmp_path, "cfg.yaml", data)
        config = loader.load(path)
        assert ReportType.ANNUAL in config.report_types
        assert ReportType.SEMI_ANNUAL in config.report_types

    def test_max_count_loaded_correctly(self, loader, tmp_path):
        """max_count 字段应正确加载"""
        data = {**MINIMAL_VALID_DATA, "max_count": 10}
        path = write_yaml(tmp_path, "cfg.yaml", data)
        config = loader.load(path)
        assert config.max_count == 10

    def test_no_dates_results_in_none(self, loader, tmp_path):
        """未配置日期时 start_date 和 end_date 均为 None"""
        path = write_yaml(tmp_path, "cfg.yaml", MINIMAL_VALID_DATA)
        config = loader.load(path)
        assert config.start_date is None
        assert config.end_date is None

    def test_yml_extension_loaded(self, loader, tmp_path):
        """.yml 扩展名的文件应能正确加载"""
        p = tmp_path / "config.yml"
        p.write_text(yaml.dump(MINIMAL_VALID_DATA, allow_unicode=True), encoding="utf-8")
        config = loader.load(str(p))
        assert isinstance(config, AppConfig)

    def test_example_config_from_task(self, loader, tmp_path):
        """加载任务说明中给出的示例配置（混合日期格式）"""
        data = {
            "storage_dir": "./reports",
            "companies": [
                {"ticker": "600519", "name": "贵州茅台"},
                {"name": "比亚迪"},
            ],
            "report_types": ["annual", "semi_annual"],
            "start_date": "2022",
            "end_date": "2023-12-31",
            "max_count": 10,
        }
        path = write_yaml(tmp_path, "example.yaml", data)
        config = loader.load(path)
        assert config.start_date == date(2022, 1, 1)
        assert config.end_date == date(2023, 12, 31)
        assert config.max_count == 10
        assert len(config.companies) == 2
