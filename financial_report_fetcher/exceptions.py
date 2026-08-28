"""
自定义异常层次模块

定义财报拉取工具中所有自定义异常类，
所有异常均继承自 FetcherBaseError 基类。
"""


class FetcherBaseError(Exception):
    """所有自定义异常的基类"""


class ConfigFileNotFoundError(FetcherBaseError):
    """
    配置文件不存在时抛出。

    错误消息中应包含文件的绝对路径，
    便于用户定位问题。
    """


class ConfigParseError(FetcherBaseError):
    """
    YAML/JSON 配置文件解析失败时抛出。

    错误消息中应包含出错的字段名或行号，
    便于用户快速定位格式错误位置。
    """


class ConfigValidationError(FetcherBaseError):
    """
    配置语义验证失败时抛出（如日期范围非法、max_count 超限等）。

    错误消息中应包含字段名和错误原因，
    便于用户理解并修正配置。
    """


class DownloadTimeoutError(FetcherBaseError):
    """
    单文件下载超时（超过 60 秒）时抛出。

    触发重试逻辑，超过最大重试次数后计入下载失败。
    """


class DownloadError(FetcherBaseError):
    """
    文件下载失败（非超时原因）时抛出。

    错误消息中应包含文件名和原始异常信息，
    便于用户排查下载失败原因。
    """


class AnalysisCancelledError(FetcherBaseError):
    """财报分析被用户主动停止时抛出（维度循环间检查 stop_event）。"""
