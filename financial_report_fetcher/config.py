"""
配置加载模块

负责从文件系统读取 YAML/JSON 配置文件，
进行格式解析和语义校验，返回 AppConfig 实例。
"""

import json
import os
from datetime import date
from typing import Any, Dict

import yaml
from pydantic import ValidationError

from .exceptions import (
    ConfigFileNotFoundError,
    ConfigParseError,
    ConfigValidationError,
)
from .models import AppConfig


def _parse_date_field(value: Any, field_name: str, is_start: bool) -> date:
    """
    将日期字段值解析为 date 对象。

    支持两种格式：
    - "YYYY"：纯年份字符串
      - start_date 解析为当年 1 月 1 日
      - end_date 解析为当年 12 月 31 日
    - "YYYY-MM-DD"：完整日期字符串，直接解析

    :param value: 配置文件中的原始日期值
    :param field_name: 字段名称（用于错误信息）
    :param is_start: True 表示 start_date，False 表示 end_date
    :raises ConfigParseError: 日期格式不合法时抛出
    """
    if isinstance(value, date):
        # 已经是 date 类型（YAML 可能自动解析），直接返回
        return value

    if not isinstance(value, str):
        raise ConfigParseError(
            f"字段 '{field_name}' 的值类型无效，期望字符串，实际为 {type(value).__name__}"
        )

    stripped = value.strip()

    # 纯年份格式："YYYY"
    if len(stripped) == 4 and stripped.isdigit():
        year = int(stripped)
        if is_start:
            # start_date：解析为当年 1 月 1 日
            return date(year, 1, 1)
        else:
            # end_date：解析为当年 12 月 31 日
            return date(year, 12, 31)

    # 完整日期格式："YYYY-MM-DD"
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        raise ConfigParseError(
            f"字段 '{field_name}' 的日期格式无效：'{value}'，"
            f"支持格式为 'YYYY' 或 'YYYY-MM-DD'"
        )


def _preprocess_dates(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    对配置字典中的日期字段进行预处理，
    在传入 Pydantic 模型之前将 "YYYY" 格式字符串转换为 date 对象。

    :param raw: 从文件中解析出的原始字典
    :returns: 预处理后的字典（日期字段已转为 date 对象）
    :raises ConfigParseError: 日期格式不合法时抛出
    """
    result = dict(raw)

    if "start_date" in result and result["start_date"] is not None:
        result["start_date"] = _parse_date_field(
            result["start_date"], "start_date", is_start=True
        )

    if "end_date" in result and result["end_date"] is not None:
        result["end_date"] = _parse_date_field(
            result["end_date"], "end_date", is_start=False
        )

    return result


class ConfigLoader:
    """
    配置文件加载器。

    支持 YAML（.yml / .yaml）和 JSON（.json）两种格式。
    """

    def load(self, path: str) -> AppConfig:
        """
        加载并验证配置文件。

        执行步骤：
        1. 检查文件是否存在，不存在则抛出 ConfigFileNotFoundError（含绝对路径）
        2. 根据扩展名解析 YAML 或 JSON，格式错误则抛出 ConfigParseError（含字段名/行号）
        3. 预处理日期字段（将 "YYYY" 转为 date 对象）
        4. 将解析结果传入 AppConfig 进行 Pydantic 验证，
           验证失败则抛出 ConfigValidationError（含字段名）

        :param path: 配置文件路径（相对路径或绝对路径）
        :returns: 验证通过的 AppConfig 实例
        :raises ConfigFileNotFoundError: 配置文件不存在，错误信息包含绝对路径
        :raises ConfigParseError: 配置格式无效，错误信息包含字段名或行号
        :raises ConfigValidationError: 配置语义错误，错误信息包含字段名
        """
        # 第一步：统一转为绝对路径，检查文件是否存在
        abs_path = os.path.abspath(path)
        if not os.path.exists(abs_path):
            raise ConfigFileNotFoundError(
                f"配置文件不存在：{abs_path}"
            )

        # 第二步：根据文件扩展名选择解析方式
        raw = self._parse_file(abs_path)

        # 第三步：校验解析结果为字典类型
        if not isinstance(raw, dict):
            raise ConfigParseError(
                f"配置文件顶层结构无效，期望键值对映射（mapping），"
                f"实际为 {type(raw).__name__}；文件：{abs_path}"
            )

        # 第四步：预处理日期字段（"YYYY" → date 对象）
        try:
            processed = _preprocess_dates(raw)
        except ConfigParseError:
            # 直接透传日期解析错误
            raise

        # 第五步：Pydantic 模型验证
        try:
            return AppConfig(**processed)
        except ValidationError as exc:
            # 提取所有错误字段名，拼成可读的错误消息
            field_errors = []
            for error in exc.errors():
                # loc 是字段路径的元组，如 ("companies", 0, "ticker")
                loc = error.get("loc", ())
                field_path = ".".join(str(part) for part in loc) if loc else "（未知字段）"
                msg = error.get("msg", "验证失败")
                field_errors.append(f"{field_path}: {msg}")

            combined = "；".join(field_errors)
            raise ConfigValidationError(
                f"配置验证失败 —— {combined}"
            ) from exc

    def _parse_file(self, abs_path: str) -> Any:
        """
        根据文件扩展名读取并解析配置文件内容。

        :param abs_path: 文件的绝对路径
        :returns: 解析后的 Python 对象（通常为 dict）
        :raises ConfigParseError: 文件读取或格式解析失败时抛出
        """
        ext = os.path.splitext(abs_path)[1].lower()

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            raise ConfigParseError(
                f"读取配置文件失败：{abs_path}，原因：{exc}"
            ) from exc

        if ext in (".yml", ".yaml"):
            return self._parse_yaml(content, abs_path)
        elif ext == ".json":
            return self._parse_json(content, abs_path)
        else:
            # 扩展名未知时，先尝试 YAML（YAML 是 JSON 的超集），再尝试 JSON
            try:
                return self._parse_yaml(content, abs_path)
            except ConfigParseError:
                return self._parse_json(content, abs_path)

    def _parse_yaml(self, content: str, abs_path: str) -> Any:
        """
        解析 YAML 格式内容。

        :param content: 文件内容字符串
        :param abs_path: 文件绝对路径（用于错误信息）
        :returns: 解析后的 Python 对象
        :raises ConfigParseError: YAML 格式错误时抛出，错误信息含行号
        """
        try:
            return yaml.safe_load(content)
        except yaml.YAMLError as exc:
            # 尝试提取行号信息
            location = ""
            if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
                mark = exc.problem_mark
                location = f"，位置：第 {mark.line + 1} 行，第 {mark.column + 1} 列"
            raise ConfigParseError(
                f"YAML 格式解析失败{location}：{exc}；文件：{abs_path}"
            ) from exc

    def _parse_json(self, content: str, abs_path: str) -> Any:
        """
        解析 JSON 格式内容。

        :param content: 文件内容字符串
        :param abs_path: 文件绝对路径（用于错误信息）
        :returns: 解析后的 Python 对象
        :raises ConfigParseError: JSON 格式错误时抛出，错误信息含行号
        """
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConfigParseError(
                f"JSON 格式解析失败，位置：第 {exc.lineno} 行，第 {exc.colno} 列："
                f"{exc.msg}；文件：{abs_path}"
            ) from exc
