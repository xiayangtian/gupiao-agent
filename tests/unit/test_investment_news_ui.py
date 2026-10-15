from pathlib import Path

import webapp.server as server


ROOT = Path(__file__).resolve().parents[2]


def test_index_has_investment_news_route_and_separate_actions():
    html = server._render_index().body.decode("utf-8")

    assert 'href="#/investment-news"' in html
    assert 'id="investment-news-page"' in html
    assert 'id="news-collect"' in html
    assert 'id="news-generate-digest"' in html
    assert 'id="news-digest-day"' in html
    assert 'id="news-market-day-controls"' in html
    assert 'data-news-window="market-today"' in html
    assert 'data-news-window="market-yesterday"' in html
    assert 'data-news-window="watchlist-today"' in html
    assert 'id="news-symbol-filter-label"' in html
    assert '股票筛选（市场资讯不可用）' in html
    assert 'id="news-symbol-filter-help"' in html
    assert 'aria-describedby="news-symbol-filter-help"' in html
    assert 'id="news-symbol-filter" aria-label="股票筛选，仅自选股资讯可用" aria-describedby="news-symbol-filter-help" disabled' in html
    assert html.index("investment_news_polling.js") < html.index("investment_news.js")
    assert "/static/investment_news.js?v=" in html


def test_news_renderer_uses_text_content_instead_of_api_html():
    script = (ROOT / "webapp/static/investment_news.js").read_text(encoding="utf-8")

    assert ".textContent" in script
    assert ".innerHTML" not in script
    assert "noopener noreferrer" in script
