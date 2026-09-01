"""结构化财务数据提供方。"""

from .akshare import AkshareProvider
from .tushare import TushareProvider

__all__ = ["AkshareProvider", "TushareProvider"]
