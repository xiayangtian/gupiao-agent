"""Keep application lifecycle tests isolated from real news sources and project data."""

import atexit
import os
import shutil
import tempfile
from pathlib import Path


_TEST_DATA = Path(tempfile.mkdtemp(prefix="gp-agent-news-tests-"))
os.environ["INVESTMENT_NEWS_DB_PATH"] = str(_TEST_DATA / "investment_news.sqlite3")
os.environ["INVESTMENT_NEWS_SCHEDULER_ENABLED"] = "0"
atexit.register(shutil.rmtree, _TEST_DATA, ignore_errors=True)
