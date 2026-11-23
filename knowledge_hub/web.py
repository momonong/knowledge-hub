"""WSGI application, server-rendered Traditional Chinese UI, security boundaries, and JSON export."""

from __future__ import annotations

import hmac
import html
import json
import re
import secrets
from collections import Counter
from functools import lru_cache
from threading import BoundedSemaphore
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from .sources import (
    ARXIV_HTML_PATH_RE,
    HF_FEED_LABEL,
    fetch_response_limit,
    default_fetch,
    parse_python_whatsnew_html,
    validate_manual_url,
)
from .store import KnowledgeStore
from .library import KnowledgeLibrary
from . import article_media, discovery, hive_reader, library_views, local_study, project_context, visuals
from . import pdf_evidence
from .editions import ArticleEditions, failure_code
from . import qa, lessons, connections, connection_views, teaching, learning, study, study_views, reading, reader_views, daily

MAX_FORM_BYTES = 65536
# Percent-encoded UTF-8 can use twelve bytes per Unicode character.
MAX_LESSON_FORM_BYTES = 300000
HOST_RE = re.compile(r"^(?:127\.0\.0\.1|localhost)(?::[1-9]\d{0,4})?$")
ITEM_ROUTE_RE = re.compile(r"^/items/([1-9]\d*)(?:/(fetch|status|notes|relations|questions|lessons|connections|study|progress|reading|source-reading|teach))?$")
VISUAL_ROUTE_RE = re.compile(r'/items/([1-9][0-9]{0,18})/visuals(?:/(?:(?P<panel>[0-9]{1,2})/(?P<image>[0-9]{1,2})|pdf/(?P<page>[1-9][0-9]{0,2})))?')
VIDEO_ROUTE_RE = re.compile(r'/items/([1-9][0-9]{0,18})/media/(original|guided)\.mp4')
LESSON_ROUTE_RE = re.compile(r"^/lessons/([1-9][0-9]*)(?:/(result|export\.zip))?$")

STATUS_LABELS = {
    "unread": "未讀",
    "later": "待讀",
    "read": "已讀",
}

SCOPE_LABELS = {
    "full": "完整全文 (full)",
    "abstract": "僅摘要 (abstract)",
    "partial": "部分內容 (partial)",
    "unavailable": "無可用正文 (unavailable)",
}

SOURCE_STATE_LABELS = {
    "unattempted": "尚未讀取 (unattempted)",
    "updated": "已更新 (updated)",
    "unchanged": "無變更 (unchanged)",
    "failed": "讀取失敗 (failed)",
}

FULLTEXT_STATUS_LABELS = {
    "not_requested": "尚未擷取全文",
    "ready": "全文已就緒",
    "partial": "部分全文可用",
    "failed": "全文擷取失敗（已保留摘要）",
    "unavailable": "來源無可用正文",
}

STYLESHEET = """
:root {
    --bg-paper: #fbf8f1;
    --bg-card: #f3ede0;
    --bg-subtle: #ebe3d2;
    --ink: #1d2324;
    --ink-muted: #4a5456;
    --teal: #0f5c63;
    --teal-dark: #094146;
    --teal-tint: #dcecee;
    --border: #cfc5b1;
    --warn-bg: #f9ebe7;
    --warn-ink: #7d2319;
}
*, *::before, *::after {
    box-sizing: border-box;
}
html, body {
    margin: 0;
    padding: 0;
    background-color: var(--bg-paper);
    color: var(--ink);
    font-family: system-ui, -apple-system, "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif;
    line-height: 1.65;
    overflow-x: hidden;
}
a {
    color: var(--teal);
    text-decoration: underline;
    text-underline-offset: 2px;
}
a:hover {
    color: var(--teal-dark);
}
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible {
    outline: 2px solid var(--teal);
    outline-offset: 2px;
}
.topbar {
    border-bottom: 1px solid var(--border);
    background-color: var(--bg-card);
    padding: 0.65rem 1rem;
}
.topbar-inner {
    max-width: 960px;
    margin: 0 auto;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem 1rem;
}
.brand {
    font-weight: 700;
    font-size: 1.05rem;
    color: var(--ink);
    text-decoration: none;
}
.nav-links {
    display: flex;
    flex-wrap: wrap;
    gap: 0.85rem;
    font-size: 0.92rem;
}
.container {
    max-width: 960px;
    margin: 0 auto;
    padding: 1.25rem 1rem 3rem;
}
.hero-header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
    border-bottom: 2px solid var(--ink);
    padding-bottom: 0.6rem;
    margin-bottom: 1.25rem;
}
.hero-header h1 {
    font-size: 1.5rem;
    margin: 0;
}
.btn {
    display: inline-block;
    background-color: var(--teal);
    color: #ffffff;
    border: 1px solid var(--teal-dark);
    border-radius: 4px;
    padding: 0.42rem 0.85rem;
    font-size: 0.92rem;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
}
.btn:hover {
    background-color: var(--teal-dark);
    color: #ffffff;
}
.btn-secondary {
    background-color: var(--bg-paper);
    color: var(--teal-dark);
    border: 1px solid var(--teal);
}
.btn-secondary:hover {
    background-color: var(--teal-tint);
    color: var(--teal-dark);
}
.pick-list {
    list-style: none;
    padding: 0;
    margin: 0 0 2rem;
    border-top: 1px solid var(--border);
}
.pick-item {
    padding: 1rem 0;
    border-bottom: 1px solid var(--border);
}
.pick-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    align-items: center;
    font-size: 0.84rem;
    color: var(--ink-muted);
    margin-bottom: 0.3rem;
}
.tag {
    display: inline-block;
    padding: 0.1rem 0.45rem;
    border-radius: 3px;
    background-color: var(--teal-tint);
    color: var(--teal-dark);
    font-weight: 600;
    font-size: 0.8rem;
}
.tag-status {
    background-color: var(--bg-subtle);
    color: var(--ink);
}
.tag-warn {
    background-color: var(--warn-bg);
    color: var(--warn-ink);
}
.pick-title {
    font-size: 1.15rem;
    font-weight: 700;
    margin: 0.2rem 0;
}
.pick-reason {
    font-size: 0.88rem;
    color: var(--teal-dark);
    margin: 0.25rem 0;
}
.pick-excerpt {
    font-size: 0.94rem;
    color: var(--ink);
    background-color: var(--bg-card);
    border-left: 3px solid var(--teal);
    padding: 0.45rem 0.7rem;
    margin: 0.4rem 0 0;
    overflow-wrap: break-word;
    word-break: break-word;
}
.panel-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
}
.panel {
    background-color: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.9rem 1rem;
}
.panel h2 {
    font-size: 1.05rem;
    margin: 0 0 0.6rem;
}
.learning-path, #learning-comparison, .connection-tools {
    margin: 1rem 0 1.5rem;
    scroll-margin-top: 1rem;
}
.learning-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 1.2rem;
    margin: 0.75rem 0;
}
.learning-nav a, .learning-steps summary, .learning-home summary {
    min-height: 44px;
    padding: 0.5rem 0;
}
.learning-steps {
    padding-left: 1.5rem;
    margin: 0;
}
.learning-steps > li {
    border-top: 1px solid var(--border);
    padding: 0.5rem 0;
}
.learning-steps summary, .learning-home summary {
    cursor: pointer;
    font-weight: 600;
}
.learning-reason, .learning-prompt {
    margin: 0.25rem 0;
}
.learning-prompt {
    color: var(--ink-muted);
    font-size: 0.9rem;
}
.learning-home { margin-bottom: 1.5rem; }
.learning-home ul { padding-left: 1.25rem; }
.learning-home li { margin: 0.5rem 0; }
.form-row {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    margin-bottom: 0.65rem;
}
.form-inline {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    align-items: center;
}
input[type="text"], input[type="url"], input[type="search"], select, textarea {
    width: 100%;
    max-width: 100%;
    padding: 0.42rem 0.55rem;
    border: 1px solid var(--border);
    border-radius: 4px;
    background-color: #ffffff;
    color: var(--ink);
    font-size: 0.94rem;
    font-family: inherit;
}
.reading-shell {
    max-width: 70ch;
    width: 100%;
    margin: 0 auto;
    overflow-wrap: break-word;
    word-break: break-word;
}
.reading-meta-bar {
    background-color: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.75rem 0.9rem;
    margin-bottom: 1rem;
    font-size: 0.9rem;
}
.version-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    align-items: center;
    margin-top: 0.5rem;
}
.block-item {
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--border);
}
.block-header {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.4rem;
    font-size: 0.82rem;
    color: var(--ink-muted);
    margin-bottom: 0.25rem;
}
.block-text {
    margin: 0;
    font-size: 1rem;
}
.notice {
    padding: 0.65rem 0.85rem;
    border-radius: 4px;
    background-color: var(--bg-card);
    border-left: 4px solid var(--teal);
    margin-bottom: 1rem;
    font-size: 0.92rem;
}
.notice-warn {
    background-color: var(--warn-bg);
    border-left-color: var(--warn-ink);
    color: var(--warn-ink);
}
@media (max-width: 640px) {
    .panel-grid {
        grid-template-columns: 1fr;
    }
    .container {
        padding: 0.9rem 0.75rem 2rem;
    }
}
"""


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _search_excerpt(value: str, query: str) -> str:
    """Display at most 320 characters including ellipses; never alter stored text."""
    if len(value) <= 320:
        return value
    needle = query.strip().casefold()
    folded_start = value.casefold().find(needle) if needle else -1
    match_start = 0
    if folded_start >= 0:
        # Map the folded offset back to the original string. Characters such as
        # ß and İ expand during casefold, so folded offsets cannot index value.
        folded_offset = 0
        for index, character in enumerate(value):
            folded_offset += len(character.casefold())
            if folded_offset > folded_start:
                match_start = index
                break
    # Reserve two characters for ellipses; keep context before the first match.
    start = min(max(0, match_start - 80), max(0, len(value) - 318))
    end = min(len(value), start + 318)
    return ('…' if start else '') + value[start:end] + ('…' if end < len(value) else '')


def _safe_href(url: str) -> str:
    cleaned = str(url or "").strip()
    if cleaned.startswith("/") and not cleaned.startswith("//"):
        return _e(cleaned)
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and "@" not in parsed.netloc
    ):
        return _e(cleaned)
    return "#"


def _is_trusted_host(environ: dict) -> bool:
    host = environ.get("HTTP_HOST", "").strip()
    if not host:
        return False
    return bool(HOST_RE.fullmatch(host))


def _is_trusted_origin(environ: dict) -> bool:
    origin = environ.get("HTTP_ORIGIN", "").strip()
    host = environ.get("HTTP_HOST", "").strip().lower()
    scheme = (environ.get("wsgi.url_scheme") or "http").strip().lower()
    if not origin or not host:
        return False
    parsed = urlsplit(origin)
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return False
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False
    if parsed.scheme.lower() != scheme or parsed.netloc.lower() != host:
        return False
    return True


def _parse_post_form(environ: dict, max_bytes: int = MAX_FORM_BYTES) -> dict[str, str]:
    ctype_raw = environ.get("CONTENT_TYPE", "")
    ctype = ctype_raw.split(";", 1)[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        raise ValueError("Unsupported Content-Type")

    clen_str = environ.get("CONTENT_LENGTH", "").strip()
    if not clen_str or not clen_str.isdigit():
        raise ValueError("Invalid Content-Length")
    clen = int(clen_str)
    if clen < 0 or clen > max_bytes:
        raise ValueError("Form body out of bounds")

    stream = environ.get("wsgi.input")
    if stream is None:
        raise ValueError("Missing wsgi.input stream")
    raw_bytes = stream.read(clen)
    if len(raw_bytes) != clen or len(raw_bytes) > max_bytes:
        raise ValueError("Form body exceeds limit")

    try:
        body_text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as err:
        raise ValueError("Form body is not valid UTF-8") from err

    parsed = parse_qs(body_text, keep_blank_values=True)
    return {k: (v[-1] if v else "") for k, v in parsed.items()}


def _layout(title: str, csrf_token: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} — Knowledge Hub</title>
<style>{STYLESHEET}</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="/">Knowledge Hub 本機知識閱讀器</a>
    <nav class="nav-links" aria-label="主要導覽">
      <a href="/">今日精選</a>
      <a href="/search">搜尋知識庫</a>
      <a href="/lessons">學習包</a>
      <a href="/library">初始知識庫</a>
      <a href="/export.json">匯出 JSON</a>
    </nav>
  </div>
</header>
<main class="container">
<form hidden aria-hidden="true"><input type="hidden" name="csrf" value="{_e(csrf_token)}"></form>
{body_html}
</main>
</body>
</html>"""


def create_app(
    db_path: str | Path, *, fetch: Callable[[str], bytes] | None = None,
    answer_provider: Callable[[dict], dict] | None = None,
    connection_provider=None, study_provider=None, project_root=None, public_projects=False, article_media_root=None,
    brief_generator=None, model_routes=None
) -> Callable:
    """Create and return the Knowledge Hub WSGI application."""
    from .followup import Followups
    from .captures import CaptureStore
    store = KnowledgeStore(db_path)
    followups = Followups(store, model_routes)
    captures = CaptureStore(store, followups)
    library = KnowledgeLibrary(store)
    connection_service = connections.ConnectionService(store, connection_provider)
    study_service = study.StudyService(store, study_provider)
    reading_state = reading.ReadingState(store)
    local_reader = local_study.LocalStudy(store)
    daily_refresh = daily.DailyRefresh(store)
    raw_fetcher = fetch if fetch is not None else default_fetch
    visual_slots = BoundedSemaphore(4)
    media_root = Path(article_media_root) if article_media_root is not None else Path(db_path).parent/'article-media'/'hive-mem-v1'

    def projects():
        return project_context.current(store, project_root, public_projects=public_projects)

    def safe_fetch(url: str) -> bytes:
        limit = fetch_response_limit(url)
        data = raw_fetcher(url)
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("Fetcher must return bytes")
        if len(data) > limit:
            raise ValueError(f"Fetched payload exceeds {limit // (1024*1024)} MiB limit")
        return bytes(data)

    editions = ArticleEditions(store, safe_fetch, lambda source: hive_reader.local_figure(media_root, source))

    @lru_cache(maxsize=12)
    def visual_image(url):
        data = hive_reader.local_figure(media_root, url)
        if data is None:
            data = safe_fetch(url)
        return data, visuals.media_type(url, data)

    def _respond(
        start_response: Callable,
        status_line: str,
        payload: bytes,
        content_type: str = "text/html; charset=utf-8",
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> list[bytes]:
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(payload))),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "same-origin" if content_type.startswith("text/html") else "no-referrer"),
            (
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self'; media-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            ),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        start_response(status_line, headers)
        return [payload]

    def _redirect(start_response: Callable, location: str) -> list[bytes]:
        safe_loc = location if location.startswith("/") and not location.startswith("//") else "/"
        return _respond(
            start_response,
            "303 See Other",
            b"",
            content_type="text/plain; charset=utf-8",
            extra_headers=[("Location", safe_loc)],
        )

    def _error_page(
        start_response: Callable, code: int, reason: str, message: str
    ) -> list[bytes]:
        csrf_token = store.get_csrf_token()
        html_doc = _layout(
            f"錯誤 {code}",
            csrf_token,
            f'<div class="notice notice-warn"><h1>{_e(code)} {_e(reason)}</h1><p>{_e(message)}</p><p><a href="/">返回首頁</a></p></div>',
        )
        return _respond(
            start_response, f"{code} {reason}", html_doc.encode("utf-8")
        )

    def _render_home() -> str:
        csrf = store.get_csrf_token()
        interests = store.get_interests()
        sources = store.get_sources()
        picks = store.get_todays_picks(limit=8)
        all_items = store.get_all_items()
        later_items = [it for it in all_items if it["status"] == "later"]

        picks_parts: list[str] = []
        if not picks:
            picks_parts.append(
                '<div class="notice">目前尚無同步文章。請按上方「更新來源」取得最新 PyTorch 與 arXiv 內容，或於下方手動加入允許的文件網址。</div>'
            )
        else:
            picks_parts.append('<ul class="pick-list">')
            for pick in picks:
                it = pick["item"]
                ver = pick["version"]
                item_href = f"/items/{it['id']}"
                anchor_href = f"/items/{it['id']}?" + urlencode({"version": ver["id"]}) + f"#{pick['anchor']}"
                src_href = _safe_href(pick["block_source_url"])
                status_label = STATUS_LABELS.get(it["status"], it["status"])
                scope_label = SCOPE_LABELS.get(ver["scope"], ver["scope"])
                picks_parts.append(
                    f"""<li class="pick-item">
  <div class="pick-meta">
    <span class="tag">{_e(it['source_name'])}</span>
    <span class="tag tag-status">{_e(status_label)}</span>
    <span class="tag tag-status" data-reading-scope="{_e(ver['scope'])}">{_e(scope_label)}</span>
    <span>版本 {_e(ver['label'])}</span>
  </div>
  <h2 class="pick-title"><a href="{_e(item_href)}">{_e(it['title'])}</a></h2>
  <p class="pick-reason">入選理由：{_e(pick['reason'])}</p>
  <blockquote class="pick-excerpt">
    {_e(pick['excerpt'])}
    <div class="pick-meta" style="margin-top:0.35rem;">
      <a href="{_e(anchor_href)}">段落 #{_e(pick['anchor'])}</a>
      <span>·</span>
      <a href="{src_href}" rel="noopener noreferrer">原文定位</a>
    </div>
  </blockquote>
</li>"""
                )
            picks_parts.append("</ul>")

        source_rows: list[str] = []
        for s_name, s_info in sources.items():
            state_val = s_info["state"]
            state_label = SOURCE_STATE_LABELS.get(state_val, state_val)
            fetched_text = s_info["last_fetched_at"] or "尚未嘗試"
            badge_cls = "tag tag-warn" if state_val == "failed" else "tag"
            err_note = (
                f'<div style="font-size:0.82rem;color:var(--warn-ink);">{_e(s_info["last_error"] or "上次同步失敗；已保留先前內容。")}</div>'
                if state_val == "failed"
                else ""
            )
            source_rows.append(
                f"""<li style="margin-bottom:0.5rem;" data-source="{_e(s_name)}" data-source-state="{_e(state_val)}">
  <strong>{_e(s_name)}</strong>：
  <span class="{badge_cls}">{_e(state_label)}</span>
  <span style="font-size:0.84rem;color:var(--ink-muted);">最後讀取：{_e(fetched_text)}</span>
  {err_note}
</li>"""
            )

        later_rows: list[str] = []
        if not later_items:
            later_rows.append("<p>目前沒有標記為「待讀」的文章。</p>")
        else:
            later_rows.append("<ul>")
            for lit in later_items[:10]:
                later_rows.append(
                    f'<li><a href="/items/{lit["id"]}">{_e(lit["title"])}</a> <span class="tag tag-status">{_e(lit["key"])}</span></li>'
                )
            later_rows.append("</ul>")

        interests_str = ", ".join(interests)
        starter_mappings = [dict(d, source_id=s['id']) for s in library.list_sources(starter_only=True)
                            for d in s['documents']]
        available = learning.available_guides(starter_mappings, {it['id']: it for it in all_items})
        featured = learning.featured_lessons(all_items)
        learning_home = []
        if available or featured:
            learning_home.append('<section class="panel learning-home" aria-labelledby="heading-learn"><h2 id="heading-learn">把新知接回基礎，從這裡開始</h2>')
            for entry in featured:
                guide = entry['guide']
                href = connections.reading_url(guide['item_id'], guide['version_id']) + '#learning-path'
                learning_home.append(f'<h3>{_e(guide["title"])}</h3><p>{_e(guide["goal"])}</p>'
                    f'<p>原文發表：{_e(entry["published_at"][:10] or "未標示")} · 已策展的固定版本示範，非今日即時整理</p>'
                    f'<p><a class="btn" href="{_e(href)}">開始同頁學習</a></p>')
            learning_home.append(f'<details><summary>核心論文教學（{len(available)} 篇可讀）</summary><ul>')
            for guide in available.values():
                href = connections.reading_url(guide['item_id'], guide['version_id']) + '#learning-path'
                learning_home.append(f'<li><a href="{_e(href)}">{_e(guide["title"])}</a></li>')
            learning_home.append('</ul><p>每篇包含機制、例子、自我檢查與來源。讀過與掌握分開判斷。</p></details></section>')
        body = f"""
<section aria-labelledby="heading-today">
  <div class="hero-header">
    <div>
      <h1 id="heading-today">今天有什麼值得讀？</h1>
      <p style="margin:0.2rem 0 0;color:var(--ink-muted);font-size:0.92rem;">
        今日精選（依明示興趣匹配與時間排序，最多顯示 8 則）
      </p>
    </div>
    <form method="post" action="/sync">
      <input type="hidden" name="csrf" value="{_e(csrf)}">
      <button type="submit" class="btn">更新來源</button>
    </form>
  </div>
  {"".join(learning_home)}
  {"".join(picks_parts)}
</section>

<div class="panel-grid">
  <section class="panel" aria-labelledby="heading-sources">
    <h2 id="heading-sources">來源同步狀態</h2>
    <ul style="padding-left:1.1rem;margin:0 0 0.75rem;">
      {"".join(source_rows)}
    </ul>
    <form method="post" action="/sync">
      <input type="hidden" name="csrf" value="{_e(csrf)}">
      <button type="submit" class="btn btn-secondary">手動重新同步來源</button>
    </form>
  </section>

  <section class="panel" aria-labelledby="heading-search">
    <h2 id="heading-search">搜尋與最近待讀</h2>
    <form method="get" action="/search" class="form-row">
      <label for="home-search-q">搜尋標題、段落、概念或筆記（支援繁中子字串）：</label>
      <div class="form-inline">
        <input id="home-search-q" type="search" name="q" placeholder="例如：跨日找回、檢索、PyTorch">
        <button type="submit" class="btn">搜尋</button>
      </div>
    </form>
    <h3 style="font-size:0.95rem;margin:0.8rem 0 0.35rem;">最近待讀清單 (later)</h3>
    {"".join(later_rows)}
  </section>

  <section class="panel" aria-labelledby="heading-interests">
    <h2 id="heading-interests">興趣關鍵字設定</h2>
    <form method="post" action="/interests" class="form-row">
      <input type="hidden" name="csrf" value="{_e(csrf)}">
      <label for="interests-terms">以逗號分隔興趣詞（留空並儲存即可清除）：</label>
      <input id="interests-terms" type="text" name="terms" value="{_e(interests_str)}" placeholder="PyTorch, retrieval, 檢索">
      <div class="form-inline" style="margin-top:0.35rem;">
        <button type="submit" class="btn">儲存興趣</button>
      </div>
    </form>
    <form method="post" action="/interests">
      <input type="hidden" name="csrf" value="{_e(csrf)}">
      <input type="hidden" name="terms" value="">
      <button type="submit" class="btn btn-secondary">清除興趣設定</button>
    </form>
  </section>

  <section class="panel" aria-labelledby="heading-manual-add">
    <h2 id="heading-manual-add">手動加入允許來源</h2>
    <form method="post" action="/add" class="form-row">
      <input type="hidden" name="csrf" value="{_e(csrf)}">
      <label for="manual-url">僅允許 Python 3.14 What's New 或帶版本號之 arXiv HTML：</label>
      <input id="manual-url" type="url" name="url" required placeholder="https://docs.python.org/3.14/whatsnew/3.14.html">
      <div style="margin-top:0.35rem;">
        <button type="submit" class="btn">加入文章</button>
      </div>
    </form>
  </section>
</div>
"""
        body = study_views.resume(store) + body
        body += f'<section class="reading-shell"><h2><a href="/library">初始知識庫</a></h2><p>已保存 {library.stats()["sources"]} 筆歷史來源卡；可搜尋、閱讀原文與找回建議背景。</p></section>'
        body += '<section class="reading-shell"><h2>最近學習包</h2><p><a href="/lessons">找回全部學習包</a>；選一篇文章與版本即可建立教材。</p>' + _lesson_rows(store.get_lessons(limit=5)) + '</section>'
        return _layout("今日精選", csrf, body)

    def _render_item_page(item: dict, active_version: dict, focus='', *, study_error='', study_form=None) -> str:
        from .knowledge_recall import recommend

        context = connection_service.build_context(item['id'], active_version['id'], focus)
        context['source_turns'] = local_reader.turns(item['id'], active_version['id'])
        context['followups'] = followups.history(item['id'], active_version['id'])
        context['captures'] = captures.list(item['id'], active_version['id'])
        context['model_routes'] = {role: bool(followups.routes.get(role)) for role in ('primary','fallback')}
        if visuals.paper_url(item['key'], active_version['label']) == hive_reader.PAPER:
            context['hive_videos'] = all((media_root/name).is_file() for name in ('original.mp4', 'guided.mp4'))
        return reader_views.detail(item,active_version,context,study_service,reading_state,
                                   store.get_csrf_token(),_safe_href,error=study_error,form=study_form,local_reader=local_reader,
                                   related_knowledge=recommend(store, item, active_version))

    def _serve_visuals(match, query, start_response):
        version_args = query.get('version', [])
        if len(version_args) != 1 or not version_args[0]:
            return _error_page(start_response, 404, 'Not Found', '找不到此版本的圖表來源。')
        version_id = version_args[0]
        page = int(match['page']) if match['page'] else None
        if page is not None and not re.fullmatch(r'[1-9][0-9]{0,18}', version_id):
            return _error_page(start_response, 404, 'Not Found', '找不到此版本的 PDF 圖表頁。')
        item = store.get_item(int(match[1]))
        version = store.select_version(item['versions'], version_id) if item else None
        source = visuals.paper_url(item['key'], version['label']) if version else None
        manifest_request = match['panel'] is None and page is None
        if source is None:
            if manifest_request and version and item['key'].startswith('arxiv:') and version['label'] == HF_FEED_LABEL:
                body = {'state':'unavailable', 'panels':[], 'source':None, 'reason':'edition_unresolved'}
                return _respond(start_response,'200 OK',json.dumps(body).encode(),'application/json; charset=utf-8')
            return _error_page(start_response, 404, 'Not Found', '找不到此版本的圖表來源。')
        pdf_url = pdf_evidence.pdf_source(item['key'], version['label'])
        if not visual_slots.acquire(blocking=False):
            return _error_page(start_response, 503, 'Service Unavailable', '目前正在載入其他圖表，請稍後再試。')
        try:
            try:
                evidence = editions.visual_manifest(source, pdf_url)
            except (OSError, ValueError):
                return _error_page(start_response, 503, 'Service Unavailable', '目前正在處理其他 PDF，請稍後再試。')
            if manifest_request:
                evidence = editions.remember_panels(version['id'], evidence)
                result = {**evidence, 'panels': [
                    {**panel, 'preview_url': f'/items/{item["id"]}/visuals/pdf/{panel["page_number"]}?version={version["id"]}'}
                    if panel.get('representation') == 'pdf_page' else panel
                    for panel in evidence['panels']
                ]}
                payload = json.dumps(result, ensure_ascii=False).encode('utf-8')
                return _respond(start_response, '200 OK', payload, 'application/json; charset=utf-8')
            evidence = editions.saved_panels(version['id']) or evidence
            headers = [('Cross-Origin-Resource-Policy', 'same-origin')]
            if page is not None:
                try:
                    data = editions.render_page(source, pdf_url, page, evidence)
                except (OSError, ValueError) as error:
                    code = failure_code(error)
                    if code == 'page_not_in_manifest':
                        return _error_page(start_response, 404, 'Not Found', '找不到此版本的 PDF 圖表頁。')
                    if code == 'busy':
                        return _error_page(start_response, 503, 'Service Unavailable', '目前正在處理其他 PDF，請稍後再試。')
                    return _error_page(start_response, 502, 'Bad Gateway', 'PDF 圖表頁暫時無法顯示。')
                return _respond(start_response, '200 OK', data, 'image/png', headers)
            try:
                url = evidence['panels'][int(match['panel'])]['images'][int(match['image'])]
            except IndexError:
                return _error_page(start_response, 404, 'Not Found', '找不到這張圖。')
            try:
                data, mime = visual_image(url)
            except (OSError, ValueError):
                return _error_page(start_response, 502, 'Bad Gateway', '原圖暫時無法取得。')
            if mime == 'image/svg+xml':
                headers.extend([('Content-Disposition', 'attachment; filename="publisher-figure.svg"'),
                                ('Content-Security-Policy', "sandbox; default-src 'none'; style-src 'unsafe-inline'" )])
            return _respond(start_response, '200 OK', data, mime, headers)
        finally:
            visual_slots.release()

    def _render_manage_item(item: dict, active_version: dict, focus='', *, study_error='', study_form=None) -> str:
        library_backlinks = ''.join(
            f'<p><a href="{_e(library_views.source_href(source["source_id"]))}">來源卡／相關背景：{_e(source["title"])}</a></p>'
            for source in library.sources_for_item(item['id']))
        csrf = store.get_csrf_token()
        scope = active_version["scope"]
        scope_label = SCOPE_LABELS.get(scope, scope)
        status_label = STATUS_LABELS.get(item["status"], item["status"])
        ft_status = item["fulltext_status"]
        ft_label = FULLTEXT_STATUS_LABELS.get(ft_status, ft_status)

        notices: list[str] = []
        if ft_status == "failed":
            notices.append(
                '<div class="notice notice-warn" data-fulltext-status="failed">全文擷取失敗，已保留既有摘要與版本內容供閱讀。</div>'
            )
        if scope == "abstract":
            notices.append(
                '<div class="notice">目前顯示摘要範圍 (abstract)。若為 arXiv 論文，可點擊「取得全文」擷取完整章節與段落錨點。</div>'
            )
        elif scope == "partial":
            notices.append(
                '<div class="notice">目前顯示部分解析範圍 (partial)，少數複雜結構已簡化為純文字段落。</div>'
            )
        elif scope == "unavailable":
            notices.append(
                '<div class="notice notice-warn">此版本目前無可用正文內容 (unavailable)。</div>'
            )

        version_links: list[str] = []
        for v in item["versions"]:
            href = f"/items/{item['id']}?" + urlencode({"version": v["id"]})
            is_current = v["id"] == active_version["id"]
            cls = "btn" if is_current else "btn btn-secondary"
            version_links.append(
                f'<a class="{cls}" href="{_e(href)}">{_e(v["label"])} ({_e(v["scope"])} · #{_e(v["id"])})</a>'
            )

        fetch_btn_html = ""
        if item["key"].startswith("arxiv:"):
            fetch_btn_html = f"""
<form method="post" action="/items/{item['id']}/fetch" style="display:inline-block;margin-right:0.5rem;">
  <input type="hidden" name="csrf" value="{_e(csrf)}">
  <button type="submit" class="btn">取得全文</button>
</form>"""

        blocks_html: list[str] = []
        anchor_options: list[str] = []
        for blk in active_version["blocks"]:
            anc = blk["anchor"]
            src_link = _safe_href(blk.get("source_url") or active_version["source_url"])
            self_link = f"/items/{item['id']}?" + urlencode({"version": active_version["id"]}) + f"#{anc}"
            anchor_options.append(f'<option value="{_e(anc)}">{_e(anc)}</option>')
            blocks_html.append(
                f"""<div class="block-item" id="{_e(anc)}" data-anchor="{_e(anc)}">
  <div class="block-header">
    <span>段落錨點：<a href="{_e(self_link)}">#{_e(anc)}</a></span>
    <a href="{src_link}" rel="noopener noreferrer">跳回原文 ({src_link})</a>
  </div>
  <p class="block-text">{_e(blk["text"])}</p>
</div>"""
            )

        notes_html: list[str] = []
        if not item["notes"]:
            notes_html.append("<p>尚無筆記紀錄。</p>")
        else:
            notes_html.append("<ul>")
            for n in item["notes"]:
                n_href = f"/items/{item['id']}?" + urlencode({"version": n["version_id"]}) + f"#{n['anchor']}"
                notes_html.append(
                    f"""<li>
  <a href="{_e(n_href)}">版本 #{_e(n['version_id'])} · 段落 #{_e(n['anchor'])}</a>：
  <span>{_e(n['text'])}</span>
</li>"""
                )
            notes_html.append("</ul>")

        relations_html: list[str] = []
        if not item["relations"]:
            relations_html.append("<p>尚無人工概念關係。</p>")
        else:
            relations_html.append("<ul>")
            for r in item["relations"]:
                r_href = f"/items/{item['id']}?" + urlencode({"version": r["version_id"]}) + f"#{r['anchor']}"
                relations_html.append(
                    f"""<li>
  <a href="{_e(r_href)}">版本 #{_e(r['version_id'])} · 段落 #{_e(r['anchor'])}</a>：
  <strong>{_e(r['concept'])}</strong> — <em>{_e(r['kind'])}</em> ({_e(r['origin'])})
</li>"""
                )
            relations_html.append("</ul>")

        qa_html = _render_questions(item, active_version, csrf)
        context = connection_service.build_context(item['id'], active_version['id'], focus)
        preset = teaching.lesson_preset(context['focal'].get('teaching_guide'), context['journey'])
        options_joined = "".join(anchor_options)
        body = f"""
<article class="reading-shell" data-reading-scope="{_e(scope)}">
  {library_backlinks}
  <header style="margin-bottom:1rem;">
    <div class="pick-meta">
      <span class="tag">{_e(item['key'])}</span>
      <span class="tag tag-status">狀態：{_e(status_label)}</span>
      <span class="tag tag-status">閱讀範圍：{_e(scope_label)}</span>
      <span class="tag tag-status">{_e(ft_label)}</span>
    </div>
    <h1 style="margin:0.35rem 0;">{_e(item['title'])}</h1>
    <div style="font-size:0.88rem;color:var(--ink-muted);">
      作者：{_e(active_version['author'] or '未標示')} ·
      時間：{_e(active_version['published_at'] or active_version['fetched_at'])} ·
      <a href="{_safe_href(active_version['source_url'])}" rel="noopener noreferrer">查看原始來源</a>
    </div>
    <nav class="form-inline" aria-label="文章閱讀捷徑" style="margin-top:0.5rem;">
      <a href="#study">教學與追問</a>
      <a href="#study-save">保存與接續</a>
      <a href="#heading-connections">串連既有知識</a>
      <a href="#reading-questions">提問與查看解說</a>
      <a href="#heading-content">閱讀正文</a>
    </nav>
  </header>

  {"".join(notices)}

  <div class="reading-meta-bar">
    <div class="form-inline" style="justify-content:space-between;">
      <div>
        {fetch_btn_html}
      </div>
      <form method="post" action="/items/{item['id']}/status" class="form-inline">
        <input type="hidden" name="csrf" value="{_e(csrf)}">
        <label for="status-select">閱讀狀態：</label>
        <select id="status-select" name="status" style="width:auto;">
          <option value="unread" {"selected" if item["status"] == "unread" else ""}>未讀 (unread)</option>
          <option value="later" {"selected" if item["status"] == "later" else ""}>待讀 (later)</option>
          <option value="read" {"selected" if item["status"] == "read" else ""}>已讀 (read)</option>
        </select>
        <button type="submit" class="btn btn-secondary">更新狀態</button>
      </form>
    </div>

    <div class="version-bar" aria-label="版本切換">
      <span>版本歷史：</span>
      {"".join(version_links)}
    </div>
  </div>

  {study_views.render(study_service, item['id'], active_version['id'], csrf, _safe_href, error=study_error, form=study_form)}
  {connection_views.render(connection_service, item['id'], active_version['id'], csrf, focus, _safe_href, context=context)}
  {qa_html}
  <section id="lesson-draft" class="panel" style="margin-bottom:1rem;">
    <h2>建立學習包：教材草稿／待核對</h2>
    <p>固定目前版本 #{_e(active_version['id'])}（{_e(scope_label)}）。{_e(preset['description']) if preset else '下方是可編輯模板；也可直接貼入 Agent 整理的 Markdown。'}</p>
    {_lesson_form(item['id'], active_version['id'], csrf, preset=preset)}
  </section>
  <section aria-labelledby="heading-content" style="margin-bottom:2rem;">
    <h2 id="heading-content" style="font-size:1.15rem;border-bottom:1px solid var(--border);padding-bottom:0.3rem;">
      正文與段落錨點（版本 {_e(active_version['label'])} · {_e(scope)}）
    </h2>
    {"".join(blocks_html)}
  </section>

  <div class="panel-grid">
    <section class="panel" aria-labelledby="heading-add-note">
      <h2 id="heading-add-note">新增理解或疑問筆記</h2>
      <form method="post" action="/items/{item['id']}/notes">
        <input type="hidden" name="csrf" value="{_e(csrf)}">
        <input type="hidden" name="version_id" value="{_e(active_version['id'])}">
        <div class="form-row">
          <label for="note-anchor">綁定段落錨點（版本 #{_e(active_version['id'])}）：</label>
          <select id="note-anchor" name="anchor" required>
            {options_joined}
          </select>
        </div>
        <div class="form-row">
          <label for="note-text">筆記內容：</label>
          <textarea id="note-text" name="text" rows="3" required placeholder="記錄本段理解、待驗證假設或疑問..."></textarea>
        </div>
        <button type="submit" class="btn">儲存筆記</button>
      </form>
    </section>

    <section class="panel" aria-labelledby="heading-add-relation">
      <h2 id="heading-add-relation">新增人工概念關係</h2>
      <form method="post" action="/items/{item['id']}/relations">
        <input type="hidden" name="csrf" value="{_e(csrf)}">
        <input type="hidden" name="version_id" value="{_e(active_version['id'])}">
        <div class="form-row">
          <label for="rel-anchor">綁定段落錨點（版本 #{_e(active_version['id'])}）：</label>
          <select id="rel-anchor" name="anchor" required>
            {options_joined}
          </select>
        </div>
        <div class="form-row">
          <label for="rel-concept">概念名稱：</label>
          <input id="rel-concept" type="text" name="concept" required placeholder="例如：檢索、Free threading">
        </div>
        <div class="form-row">
          <label for="rel-kind">關係類型：</label>
          <input id="rel-kind" type="text" name="kind" value="explains" required placeholder="explains / uses / contrasts">
        </div>
        <button type="submit" class="btn">儲存概念關係</button>
      </form>
    </section>
  </div>

  <section class="panel" style="margin-bottom:1rem;" aria-labelledby="heading-saved-notes">
    <h2 id="heading-saved-notes">已保存筆記（跨版本保留原錨點）</h2>
    {"".join(notes_html)}
  </section>

  <section class="panel" aria-labelledby="heading-saved-relations">
    <h2 id="heading-saved-relations">已保存概念關係</h2>
    {"".join(relations_html)}
  </section>
</article>
"""
        return _layout(item["title"], csrf, body)

    def _lesson_rows(records):
        parts = ['<ul class="pick-list">']
        for lesson in records:
            origin = 'Agent 協助' if lesson['origin'] == 'agent_assisted' else '人工編寫'
            parts.append(f'''<li class="pick-item"><a href="/lessons/{lesson['id']}">{_e(lesson['title'])}</a>
              <div class="pick-meta">教材草稿／待核對 · {origin} · 版本 #{_e(lesson['version_id'])} · {_e(lesson['created_at'])}</div>
              <p>{_e(lesson['learning_goal'])}</p></li>''')
        if not records:
            parts.append('<li>尚無學習包。請從文章頁選定版本，填寫教材草稿。</li>')
        parts.append('</ul>')
        return ''.join(parts)

    def _lesson_form(item_id, version_id, csrf, draft=None, preset=None):
        parent = f'<input type="hidden" name="parent_id" value="{draft["id"]}">' if draft else ''
        revision = bool(draft)
        draft = draft or preset or {}
        origin = draft.get('origin', 'manual')
        return f'''<form method="post" action="/items/{item_id}/lessons">
          <input type="hidden" name="csrf" value="{_e(csrf)}">
          <input type="hidden" name="version_id" value="{_e(version_id)}">{parent}
          <div class="form-row"><label for="lesson-title">教材標題（最多 200 字元）</label>
          <input id="lesson-title" type="text" name="title" maxlength="200" value="{_e(draft.get('title', ''))}" required></div>
          <div class="form-row"><label for="lesson-goal">學習目標（最多 2000 字元）</label>
          <textarea id="lesson-goal" name="learning_goal" rows="3" maxlength="2000" required>{_e(draft.get('learning_goal', ''))}</textarea></div>
          <div class="form-row"><label for="lesson-blueprint">教學藍圖 Markdown（最多 16000 字元、64 KiB；逐字保存）</label>
          <textarea id="lesson-blueprint" name="blueprint_md" rows="16" maxlength="16000" required>{_e(draft.get('blueprint_md', lessons.BLUEPRINT_TEMPLATE))}</textarea></div>
          <div class="form-row"><label for="lesson-origin">教材編寫方式</label>
          <select id="lesson-origin" name="origin"><option value="manual" {'selected' if origin == 'manual' else ''}>人工編寫</option>
          <option value="agent_assisted" {'selected' if origin == 'agent_assisted' else ''}>Agent 協助（仍待人工核對）</option></select></div>
          <button type="submit" class="btn">{'另存修訂新稿' if revision else '保存教材草稿'}</button>
          <p>保存不表示已學會或影片品質通過；修訂另存新稿，成果連結不會自動繼承。</p></form>'''

    def _render_lesson(lesson):
        csrf = store.get_csrf_token()
        source = lesson['source']
        origin = 'Agent 協助' if lesson['origin'] == 'agent_assisted' else '人工編寫'
        back = f"/items/{lesson['item_id']}?" + urlencode({'version': lesson['version_id']})
        parent = (f'<a href="/lessons/{lesson["parent_id"]}">原稿 #{lesson["parent_id"]}</a>'
                  if lesson['parent_id'] is not None else '初稿')
        result = (f'<a href="{_safe_href(lesson["notebook_url"])}" rel="noopener noreferrer" target="_blank">開啟已登記 NotebookLM 工作區</a>'
                  if lesson['notebook_url'] else '尚未登記成果連結')
        blocks = ''.join(f'<div class="block-item"><p>段落 #{_e(b["anchor"])}</p><p style="white-space:pre-wrap">{_e(b["text"])}</p><a href="{_safe_href(b["source_url"])}" rel="noopener noreferrer">原文位置</a></div>' for b in source['blocks'])
        body = f'''<article class="reading-shell"><h1>{_e(lesson['title'])}</h1>
          <p class="tag">教材草稿／待核對 · {origin}</p>
          <p>{parent} · {_e(lesson['created_at'])} · <a href="{_e(back)}">回原文章固定版本</a> · <a href="/lessons">全部學習包</a></p>
          <h2>學習目標</h2><p style="white-space:pre-wrap">{_e(lesson['learning_goal'])}</p>
          <section class="panel"><h2>帶入 NotebookLM</h2>
          <p>下載來源包並解壓縮，在 NotebookLM 手動加入 source.md 與 lesson-blueprint.md，參考 producer-instructions.txt 製作解說。原文提供研究證據，藍圖安排教學；生成後核對內容，再回填工作區連結。</p>
          <a class="btn" href="/lessons/{lesson['id']}/export.zip">下載 NotebookLM 來源包 ZIP</a>
          <p>只匯出這份固定公開來源與教材，不含私人筆記或問答。文字快照不代表圖表／公式全部解析。沒有自動登入或影片生成。</p></section>
          <h2>固定教材 Markdown</h2><div style="white-space:pre-wrap">{_e(lesson['blueprint_md'])}</div>
          <h2>固定來源：{_e(source['title'])}</h2>
          <p>版本 #{_e(source['version_id'])} · {_e(source['label'])} · 實際解析範圍 {_e(source['scope'])}</p>
          <a href="{_safe_href(source['source_url'])}" rel="noopener noreferrer">原文</a>
          <details><summary>來源文字快照</summary>{blocks}</details>
          <details><summary>版本稽核與內容雜湊</summary><p>content_hash：{_e(source['content_hash'])}<br>取得時間：{_e(source['fetched_at'])}<br>學習包 #{lesson['id']} · 文章 #{lesson['item_id']}</p></details>
          <section class="panel" style="margin-top:1rem;"><h2>成果連結與核對筆記</h2><p>{result}</p>
          <p style="white-space:pre-wrap">{_e(lesson['review_notes'])}</p>
          <form method="post" action="/lessons/{lesson['id']}/result">
          <input type="hidden" name="csrf" value="{_e(csrf)}">
          <div class="form-row"><label for="notebook-url">NotebookLM 工作區連結（可留空）</label>
          <input id="notebook-url" type="url" name="notebook_url" value="{_e(lesson['notebook_url'])}" placeholder="https://notebooklm.google.com/notebook/UUID"></div>
          <div class="form-row"><label for="review-notes">核對筆記（最多 10000 字元）</label>
          <textarea id="review-notes" name="review_notes" rows="6" maxlength="10000">{_e(lesson['review_notes'])}</textarea></div>
          <button class="btn" type="submit">保存成果與核對筆記</button></form>
          <p>登記連結不表示影片已通過核對或人工接受。</p></section>
          <h2>編修並另存新稿</h2>{_lesson_form(lesson['item_id'], lesson['version_id'], csrf, lesson)}</article>'''
        return _layout(lesson['title'], csrf, body)

    def _render_questions(item: dict, version: dict, csrf: str) -> str:
        configured = answer_provider is not None
        state = 'configured' if configured else 'unconfigured'
        scope_labels = {'full': '完整解析正文', 'abstract': '僅摘要',
                        'partial': '部分解析內容', 'unavailable': '無可用正文'}
        status_labels = {'completed': '解說已保存', 'unknown': '完成情況待核對',
                         'failed': '回答檢查失敗'}
        parts = [f'<section id="reading-questions" tabindex="-1" class="panel" style="margin-bottom:1rem;" data-answer-provider="{state}" aria-labelledby="heading-questions">',
                 '<h2 id="heading-questions">單篇解說與追問</h2>',
                 '<p>主張分類與文字為模型整理；引用存在不代表語意正確，請核對原文。私人筆記不會自動傳送，來源只是資料，沒有工具權限。</p>']
        try:
            source, truncated = qa.bounded_source(version, item['title'])
            readable = True
            range_note = ('內容已截短，解說只涵蓋下列選取段落；未送出的文字不在解說範圍內。'
                          if truncated else '未因長度截短，將送出此版本所有可用段落。')
            parts.append(f'<p>提問版本 {_e(version["label"])}（#{_e(version["id"])}） · {_e(scope_labels[source["scope"]])}；將送出 {len(source["blocks"])} 段、{sum(len(b["text"]) for b in source["blocks"])} 字元。{range_note}</p>')
            parts.append('<details><summary>查看將送出的實際段落範圍</summary><ul>')
            parts.extend(f'<li>#{_e(b["anchor"])}：{len(b["text"])} 字元（由段首起）</li>' for b in source['blocks'])
            parts.append('</ul></details>')
        except ValueError:
            readable = False
            parts.append('<p>此版本沒有可用文字，不能派送。</p>')
        if not configured:
            parts.append('<p class="notice notice-warn">尚未設定回答提供者，解說與提問目前不可用。不會產生示範或假回答。</p>')
        blocked = any(q['version_id'] == version['id'] and q['status'] == 'unknown' for q in item['questions'])
        if blocked:
            parts.append('<p class="notice notice-warn">此版本已有操作完成情況待核對。為避免重複派送，目前不能再次提問或手動清除；核對機制尚未提供，可在下方操作紀錄查看收據。</p>')
        disabled = '' if configured and readable and not blocked else ' disabled'
        parts.append(f'''<form method="post" action="/items/{item['id']}/questions">
          <input type="hidden" name="csrf" value="{_e(csrf)}">
          <input type="hidden" name="version_id" value="{_e(version['id'])}">
          <input type="hidden" name="request_id" value="{secrets.token_urlsafe(24)}">
          <div class="form-row"><label for="reading-question">想了解本篇的哪些主張、證據或限制？（最多 2000 字）</label>
          <textarea id="reading-question" name="question" rows="3" maxlength="2000" required{disabled}></textarea></div>
          <button class="btn" type="submit"{disabled}>提問／解說</button></form>
          <h3>已保存問答（保留原版本）</h3>''')
        if not item['questions']:
            parts.append('<p>尚無問答。</p>')
        labels = {'author_claim': '作者主張', 'evidence': '證據', 'inference': '推論'}
        for record in reversed(item['questions']):
            href = f"/items/{item['id']}?" + urlencode({'version': record['version_id']}) + f"#question-{record['id']}"
            range_note = ('內容已截短，解說僅涵蓋實際送出的段落。'
                          if record['context_truncated'] else '未因長度截短，已送出此版本所有可用段落。')
            parts.append(f'''<div class="block-item" tabindex="-1" id="question-{record['id']}" data-question-status="{_e(record['status'])}">
              <h3>{_e(record['question'])}</h3>
              <p><a href="{_e(href)}">版本 #{_e(record['version_id'])}</a> · {_e(scope_labels[record['reading_scope']])} · <strong>{_e(status_labels[record['status']])}</strong></p>
              <p>已送出 {len(record['source']['blocks'])} 段、{sum(len(b['text']) for b in record['source']['blocks'])} 字元。{range_note}</p>
              <details><summary>操作紀錄與收據</summary>
              <p>原始狀態：{_e(record['status'])}<br>
              request_id：{_e(record['request_id'])}<br>operation_id：{_e(record['operation_id'])}<br>
              context_truncated={str(record['context_truncated']).lower()}</p></details>
              <details><summary>查看實際送出的來源文字</summary>''')
            for block in record['source']['blocks']:
                parts.append(f'<p>#{_e(block["anchor"])}（{len(block["text"])} 字元）</p><blockquote>{_e(block["text"])}</blockquote>')
            parts.append('</details>')
            if record['status'] == 'completed' and record['answer']:
                for claim in record['answer']['claims']:
                    parts.append(f'<p><strong>{_e(labels[claim["kind"]])}</strong>：{_e(claim["text"])}</p>')
                    for citation in claim['citations']:
                        local = f"/items/{item['id']}?" + urlencode({'version': record['version_id']}) + '#' + citation['anchor']
                        parts.append(f'<blockquote>{_e(citation["quote"])}<br><a href="{_e(local)}">本機段落 #{_e(citation["anchor"])}</a> · <a href="{_safe_href(citation["source_url"])}" rel="noopener noreferrer">原文定位</a></blockquote>')
                parts.append(f'<p>限制：{_e(record["answer"]["limitations"])}</p>')
            elif record['status'] == 'unknown':
                parts.append('<p>目前無法確認是否完成；已保存操作收據，不能重送，也不能手動清除。</p>')
            else:
                parts.append('<p>回答結構或引用未通過檢查；沒有有效答案，不會自動重試。</p>')
            parts.append('</div>')
        parts.append('</section>')
        return ''.join(parts)

    def _render_search_page(query: str) -> str:
        csrf = store.get_csrf_token()
        results = store.search(query) if query.strip() else []
        results_html: list[str] = []
        if not query.strip():
            results_html.append('<div class="notice">請輸入關鍵字以搜尋標題、段落、概念關係與筆記。</div>')
        elif not results:
            results_html.append('<div class="notice">找不到符合此關鍵字的文章、段落或筆記。</div>')
        else:
            results_html.append('<ul class="pick-list">')
            for res in results:
                it = res["item"]
                hit_items: list[str] = []
                for h in res["hits"]:
                    qs = urlencode({"version": h["version_id"]})
                    frag = f"#{h['anchor']}" if h["anchor"] else ""
                    hit_href = f"/items/{it['id']}?{qs}{frag}"
                    hit_items.append(
                        f"""<li>
  <span class="tag tag-status">{_e(h['kind'])}</span>
  <a href="{_e(hit_href)}">{_e(it['title'])}（版本 {_e(h['version_label'])} #{_e(h['anchor'])}）</a>：
  <span class="search-excerpt">{_e(_search_excerpt(h['snippet'], query))}</span>
</li>"""
                    )
                more_hits = ''
                if len(hit_items) > 3:
                    more_hits = (f'<details><summary>其餘 {len(hit_items) - 3} 筆符合結果</summary>'
                                 f'<ul>{"".join(hit_items[3:])}</ul></details>')
                results_html.append(
                    f"""<li class="pick-item">
  <h3 class="pick-title"><a href="/items/{it['id']}">{_e(it['title'])}</a></h3>
  <ul>{"".join(hit_items[:3])}</ul>
  {more_hits}
</li>"""
                )
            results_html.append("</ul>")

        body = f"""
<section class="reading-shell" aria-labelledby="heading-search-page">
  <h1 id="heading-search-page">搜尋知識庫</h1>
  <form method="get" action="/search" class="panel" style="margin-bottom:1.5rem;">
    <div class="form-row">
      <label for="search-input">搜尋關鍵字（支援繁體中文與英文子字串）：</label>
      <div class="form-inline">
        <input id="search-input" type="search" name="q" value="{_e(query)}" placeholder="輸入標題、段落、筆記或概念...">
        <button type="submit" class="btn">搜尋</button>
      </div>
    </div>
  </form>
  {library_views.hits(library.search(query)) if query.strip() else ""}
  <h2>既有文章、筆記與問答</h2>
  {"".join(results_html)}
</section>
"""
        return _layout("搜尋知識庫", csrf, body)

    def public_daily_edition(item_id, version_id):
        published = followups.public_edition(item_id, version_id)
        if published:
            item, version = published
            if item['key'].startswith('arxiv:') and version['scope'] in ('full','partial'):
                html = visuals.paper_url(item['key'],version['label'])
                pdf = pdf_evidence.pdf_source(item['key'],version['label'])
                if version['source_url'] not in (html,pdf):
                    return None
        return published

    def build_daily_brief(pick, generator=None, *, retry_limit=None):
        from . import briefs
        # The model callback can see published metadata, source and the
        # spectrum classification. Private reading reasons never reach it.
        public_item = {key:pick['item'][key] for key in ('id','key','title','source_name') if key in pick['item']}
        candidate = {'item':public_item,'version':pick['version'],
                     'category':pick.get('category',''),'spectrum_reason':pick.get('spectrum_reason','')}
        if generator is not None:
            panels = editions.saved_panels(pick['version']['id'])
            if panels:
                from .grounding import _panels
                verified = _panels(public_item,pick['version'],panels)
                fields = ('anchor','kind','caption','rows','source_url','representation','page_url','page_number')
                candidate['brief_panels'] = {'source':panels['source'],'panels':[
                    {key:panel[key] for key in fields if key in panel} for panel,_ in verified.values()
                ]}
        result = (briefs.build_brief(candidate,generator) if retry_limit is None
                  else briefs.build_brief(candidate,generator,max_attempts=retry_limit))
        if result['state'] == 'ready' and generator is not None:
            result.setdefault('mode','model')
        return result

    def refresh_daily_briefs(day, *, item_id=None, manual=False, freshly_built=()):
        from . import briefs
        daily_refresh.upgrade_briefs(day,public_daily_edition,
            lambda pick: build_daily_brief(pick,brief_generator,retry_limit=1),
            format_version=briefs.FORMAT_VERSION, mode='model' if brief_generator is not None else 'local',
            item_id=item_id, manual=manual, freshly_built=freshly_built)
        return daily_refresh.reconcile_health(day)

    def refresh_daily_sources(*, prefetch=True, force=False):
        from . import briefs, feeds
        day = daily_refresh.day()
        freshly_built = set()

        def collect():
            if public_projects:
                project_context.refresh_public_projects(store, safe_fetch)
            feeds.collect(store, safe_fetch, day)
            picks = daily_refresh.selection(day,resolved=False)
            if len(picks) == daily.PICK_COUNT:
                return picks
            active, _ = projects()
            pool = discovery.rank(store, active, limit=daily.PICK_COUNT,
                                  exclude_keys=daily_refresh.recent_keys(day,include_today=True))
            categories = Counter(pick.get('category') for pick in picks)
            for ranked in pool:
                if len(picks) == daily.PICK_COUNT:
                    break
                category = ranked['category']
                if categories[category] >= 2:
                    continue
                pick = editions.pin_abstract(ranked)
                if pick.get('brief_format') != briefs.FORMAT_VERSION or pick.get('brief', {}).get('state') != 'ready':
                    generator = None
                    if brief_generator is not None:
                        published = public_daily_edition(pick['item']['id'], pick['version']['id'])
                        if published:
                            public_item, fixed_version = published
                            pick['item'] = {**pick['item'], **public_item}
                            pick['version'] = fixed_version
                            generator = brief_generator
                    pick['brief'] = build_daily_brief(pick, generator)
                    pick['brief_format'] = briefs.FORMAT_VERSION
                    freshly_built.add((pick['item']['id'],str(pick['version']['id'])))
                picks.append(pick)
                categories[category] += 1
            return picks

        result = daily_refresh.run(collect, day=day, force=force)
        # A fixed original is reusable for deep reading. It never holds the daily cards transaction open.
        if prefetch and result['state'] in ('completed', 'partial') and editions.claim_prefetch(day):
            for pick in daily_refresh.selection(day):
                item, version = pick['item'], pick['version']
                if item['key'].startswith('arxiv:') and version['scope'] == 'abstract':
                    editions.acquire(item, version)
        if result['state'] in ('completed','partial'):
            return refresh_daily_briefs(day,manual=force,freshly_built=freshly_built) or result
        return result

    def app(environ: dict, start_response: Callable) -> list[bytes]:
        if not _is_trusted_host(environ):
            return _respond(
                start_response,
                "400 Bad Request",
                b"Untrusted Host header",
                content_type="text/plain; charset=utf-8",
            )

        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"
        query_params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)

        layered = re.fullmatch(r'/items/([1-9][0-9]{0,17})/(guide|pdf|followup|captures|recall|daily-choice)', path)
        video = VIDEO_ROUTE_RE.fullmatch(path)
        if video and method in ('GET', 'HEAD'):
            version_id = query_params.get('version', [])
            if len(version_id) == 1 and re.fullmatch(r'[1-9][0-9]{0,18}', version_id[0]):
                with store._connect() as conn:
                    owns_video = conn.execute(
                        'SELECT 1 FROM items i JOIN item_versions v ON v.item_id=i.id '
                        'WHERE i.id=? AND v.id=? AND i.key=? AND v.label=?',
                        (video[1], version_id[0], hive_reader.PAPER_KEY, 'v1')).fetchone()
                if owns_video:
                    return article_media.serve(environ, start_response, media_root, video[2]+'.mp4')
            return _respond(start_response, '404 Not Found', b'', 'text/plain; charset=utf-8')
        if method not in ("GET", "POST"):
            return _error_page(start_response, 405, "Method Not Allowed", "不支援的 HTTP 方法。")

        form: dict[str, str] = {}
        if method == "POST":
            if not _is_trusted_origin(environ):
                return _error_page(start_response, 403, "Forbidden", "拒絕非本機來源的寫入請求。")
            try:
                has_long_text = bool(re.fullmatch(r'/items/[1-9][0-9]*/(?:lessons|progress|reading|captures)|/lessons/[1-9][0-9]*/result', path))
                form = _parse_post_form(environ, MAX_LESSON_FORM_BYTES if has_long_text else MAX_FORM_BYTES)
            except ValueError:
                return _error_page(start_response, 400, "Bad Request", "表單格式或長度無效。")

            submitted_csrf = form.get("csrf", "")
            expected_csrf = store.get_csrf_token()
            if not submitted_csrf or not hmac.compare_digest(
                submitted_csrf.encode("utf-8"), expected_csrf.encode("utf-8")
            ):
                return _error_page(start_response, 403, "Forbidden", "CSRF 驗證失敗。")

        # GET routes
        if layered:
            item = store.get_item(int(layered[1]))
            version_id = form.get('version_id') if method == 'POST' else query_params.get('version', [''])[-1]
            version = next((v for v in item['versions'] if v['id'] == version_id), None) if item else None
            if not version:
                return _error_page(start_response, 404, 'Not Found', '找不到這篇文章的指定版本。')
            if layered[2] == 'pdf' and method == 'GET':
                source = visuals.paper_url(item['key'], version['label'])
                pdf_url = pdf_evidence.pdf_source(item['key'], version['label'])
                if not source or not pdf_url:
                    return _error_page(start_response, 404, 'Not Found', '沒有這個版本的出版者 PDF。')
                try:
                    document = editions.original_pdf(source, pdf_url)
                except (ValueError, OSError):
                    return _error_page(start_response, 503, 'Service Unavailable', '同版 PDF 暫時無法取得。可從閱讀頁開啟出版者原檔。')
                return _respond(start_response, '200 OK', document, 'application/pdf',
                                [('Content-Disposition','inline; filename="article.pdf"')])
            if layered[2] == 'daily-choice' and method == 'POST':
                try:
                    daily_refresh.choose(form.get('day',''), item['id'], version_id, form.get('choice',''))
                except ValueError as exc:
                    return _error_page(start_response, 400, 'Bad Request', str(exc))
                return _redirect(start_response, '/#daily-' + str(item['id']))
            if layered[2] in ('captures','recall'):
                try:
                    if layered[2] == 'captures' and method == 'POST':
                        command = {key: value for key, value in form.items() if key not in ('csrf','version_id')}
                        result = {'record': captures.act(item, version, command)}
                    elif layered[2] == 'captures':
                        result = {'records': captures.list(item['id'], version_id)}
                    elif method == 'GET':
                        query = query_params.get('q',[''])[-1].strip()
                        result = {'results': captures.search(query) if query else captures.related(item, version)}
                    else:
                        return _error_page(start_response, 405, 'Method Not Allowed', '搜尋僅支援讀取。')
                except ValueError as exc:
                    code = getattr(exc, 'status', 400)
                    status = {400:'400 Bad Request', 404:'404 Not Found', 409:'409 Conflict'}.get(code,'400 Bad Request')
                    return _respond(start_response, status, json.dumps({'error': str(exc)}, ensure_ascii=False).encode(),
                                    'application/json; charset=utf-8')
                return _respond(start_response, '200 OK', json.dumps(result, ensure_ascii=False).encode(),
                                'application/json; charset=utf-8')
            if layered[2] == 'guide' and method == 'GET':
                from .guided import guide as reading_guide
                route = query_params.get('route', ['primary'])[-1]
                if route not in ('primary', 'fallback'):
                    return _error_page(start_response, 400, 'Bad Request', '帶讀模型路由無效。')
                panels = editions.saved_panels(version_id)
                source = visuals.paper_url(item['key'], version['label'])
                text_stage = query_params.get('stage') == ['text']
                if not panels and not text_stage and source and version['scope'] in ('full', 'partial'):
                    try:
                        panels = editions.visual_manifest(source, pdf_evidence.pdf_source(item['key'], version['label']))
                    except (OSError, ValueError):
                        panels = {}
                    if panels:
                        panels = editions.remember_panels(version_id, panels)
                guide = reading_guide(store, item['id'], version_id, followups.routes,
                                      route=route,
                                      retry=query_params.get('retry') == ['1'])
                guide['visuals_ready'] = bool(panels and panels.get('panels'))
                return _respond(start_response, '200 OK', json.dumps(guide, ensure_ascii=False).encode(),
                                'application/json; charset=utf-8')
            if layered[2] == 'followup' and method == 'POST':
                events = followups.run(item, version, form.get('question', ''), form.get('anchor', ''),
                                       form.get('request_id', ''), route=form.get('route', 'primary'),
                                       panels=editions.saved_panels(version_id), quote=form.get('quote',''))
                start_response('200 OK', [('Content-Type','text/event-stream; charset=utf-8'),
                                          ('Cache-Control','no-store, no-transform'), ('X-Accel-Buffering','no'),
                                          ('X-Content-Type-Options','nosniff'), ('Referrer-Policy','no-referrer')])
                def stream():
                    try:
                        for event, value in events:
                            yield f'event: {event}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n'.encode()
                    finally:
                        events.close()
                return stream()
            return _error_page(start_response, 405, 'Method Not Allowed', '此端點不支援這個 HTTP 方法。')

        if method == "GET":
            visual_match = VISUAL_ROUTE_RE.fullmatch(path)
            if visual_match:
                return _serve_visuals(visual_match, query_params, start_response)
            if path in ('/static/reader.css', '/static/briefing.css', '/static/room.css',
                        '/static/reading-actions.css', '/static/reader.js', '/static/layers.js'):
                filename = path.rsplit('/', 1)[1]
                data = (Path(__file__).parent / 'static' / filename).read_bytes()
                mime = 'text/css; charset=utf-8' if filename.endswith('.css') else 'text/javascript; charset=utf-8'
                return _respond(start_response, '200 OK', data, mime)
            if path == '/reading':
                page = query_params.get('page', ['1'])[-1]
                if not re.fullmatch(r'[1-9][0-9]{0,8}', page):
                    return _error_page(start_response, 400, 'Bad Request', '接續頁碼須為正整數。')
                return _respond(start_response,'200 OK',reader_views.history(
                    store,reading_state,store.get_csrf_token(),page=int(page)).encode('utf-8'))
            if path == '/manage':
                return _respond(start_response, '200 OK', _render_home().encode('utf-8'))
            if path == '/library':
                try:
                    page = library_views.listing(library, query_params.get('q', [''])[-1],
                        query_params.get('domain', [''])[-1],
                        query_params.get('starter', [''])[-1] in ('1', 'on', 'true'))
                except ValueError as error:
                    return _error_page(start_response, 400, 'Bad Request', str(error))
                return _respond(start_response, '200 OK', _layout('初始知識庫', store.get_csrf_token(), page).encode('utf-8'))

            if path.startswith('/library/files/'):
                try:
                    digest = path[len('/library/files/'):]
                    raw, mime = library.read_blob(digest)
                except (ValueError, OSError) as error:
                    return _error_page(start_response, 404, 'Not Found', '原檔不存在、無法安全讀取或雜湊核對失敗。')
                extension = {'application/pdf': 'pdf', 'text/plain': 'txt', 'text/html': 'html'}[mime]
                return _respond(start_response, '200 OK', raw, mime,
                    [('Content-Disposition', f'attachment; filename="{digest}.{extension}"')])

            library_match = re.fullmatch(r'/library/([A-Za-z0-9][A-Za-z0-9_.-]{0,199})', path)
            if library_match:
                source = library.get_source(library_match[1])
                if source is None:
                    return _error_page(start_response, 404, 'Not Found', '找不到來源卡。')
                return _respond(start_response, '200 OK', _layout(source['title'],
                    store.get_csrf_token(), library_views.detail(source)).encode('utf-8'))

            if path == "/":
                today = daily_refresh.day()
                selected_day = daily_refresh.latest_day()
                picks = daily_refresh.selection(selected_day) if selected_day else []
                status = daily_refresh.status()
                active_projects, project_mode = project_context.current(store, public_projects=public_projects)
                return _respond(start_response, "200 OK", reader_views.home(
                    store, library, reading_state, store.get_csrf_token(), status, picks=picks,
                    selected_day=selected_day, today=today, active_projects=active_projects, project_mode=project_mode).encode("utf-8"))

            if path == '/discovery/status':
                from . import daily_views

                report = daily_refresh.report()
                headers = [('Vary', 'Accept')]
                if 'application/json' in environ.get('HTTP_ACCEPT', ''):
                    return _respond(start_response, '200 OK', json.dumps(report, ensure_ascii=False).encode(),
                                    'application/json; charset=utf-8', headers)
                body = daily_views.status_page(report, store.get_csrf_token())
                return _respond(start_response, '200 OK', body.encode(), extra_headers=headers)

            if path == "/export.json":
                payload = json.dumps(
                    store.export_data(), ensure_ascii=False, indent=2
                ).encode("utf-8")
                return _respond(
                    start_response,
                    "200 OK",
                    payload,
                    content_type="application/json; charset=utf-8",
                )

            if path == "/search":
                q = query_params.get("q", [""])[-1]
                if len(q) > 2000:
                    return _error_page(start_response, 400, "Bad Request", "搜尋上限為 2000 字元。")
                return _respond(
                    start_response, "200 OK", _render_search_page(q).encode("utf-8")
                )

            if path == '/lessons':
                csrf = store.get_csrf_token()
                page = _layout('學習包', csrf, '<section class="reading-shell"><h1>學習包</h1><p>教材草稿／待核對。由文章頁選定版本建立；此處可跨日找回固定教材與成果連結。</p>' + _lesson_rows(store.get_lessons()) + '</section>')
                return _respond(start_response, '200 OK', page.encode('utf-8'))

            lesson_match = LESSON_ROUTE_RE.fullmatch(path)
            if lesson_match:
                try:
                    lesson = store.get_lesson(lesson_match.group(1))
                except ValueError:
                    return _error_page(start_response, 400, 'Bad Request', '學習包識別值無效。')
                if lesson is None:
                    return _error_page(start_response, 404, 'Not Found', '找不到學習包。')
                if lesson_match.group(2) == 'export.zip':
                    try:
                        payload = lessons.export_zip(lesson)
                    except ValueError as error:
                        return _error_page(start_response, 400, 'Bad Request', str(error))
                    return _respond(start_response, '200 OK', payload, 'application/zip',
                                    [('Content-Disposition', f'attachment; filename="lesson-{lesson["id"]}.zip"')])
                if lesson_match.group(2) is None:
                    return _respond(start_response, '200 OK', _render_lesson(lesson).encode('utf-8'))

            item_match = ITEM_ROUTE_RE.fullmatch(path)
            if item_match and item_match.group(2) is None:
                try:
                    item_id = lessons.identifier(item_match.group(1))
                except ValueError:
                    return _error_page(start_response, 400, 'Bad Request', '文章識別值無效。')
                item = store.get_item(item_id)
                if item is None or not item["versions"]:
                    return _error_page(start_response, 404, "Not Found", "找不到指定的文章。")

                requested_v = query_params.get("version", [""])[-1].strip()
                focus = query_params.get('focus', [''])[-1]
                if len(focus) > 500:
                    return _error_page(start_response, 400, 'Bad Request', '焦點最多 500 字元。')
                active_version = store.select_version(item["versions"], requested_v)
                if active_version is None:
                    return _error_page(
                        start_response, 404, "Not Found", "找不到指定的文章版本。"
                    )

                return _respond(
                    start_response,
                    "200 OK",
                    (_render_manage_item if query_params.get('view',[''])[-1] == 'manage'
                     else _render_item_page)(item, active_version, focus).encode("utf-8"),
                )

            return _error_page(start_response, 404, "Not Found", "找不到請求的頁面。")

        # POST routes
        def saved_location(location, **details):
            if environ.get('HTTP_ACCEPT') == 'application/json':
                return _respond(start_response, '200 OK', json.dumps({'location':location, **details}).encode(),
                                'application/json; charset=utf-8')
            return _redirect(start_response, location)

        study_refresh = re.fullmatch(r'/study/([1-9][0-9]{0,18})/reconcile', path)
        if study_refresh:
            try:
                row = study_service.reconcile(study_refresh[1],inspect_unknown=form.get('inspect_unknown') == '1')
            except study.StudyError as error:
                reason = {400:'Bad Request',404:'Not Found',409:'Conflict',503:'Service Unavailable'}[error.code]
                return _error_page(start_response,error.code,reason,str(error))
            location = connections.reading_url(row['item_id'],row['version_id'],f"study-{row['id']}")
            return saved_location(location, state=row['state'])
        refresh_match = re.fullmatch(r'/connections/([1-9][0-9]{0,18})/refresh', path)
        if refresh_match:
            try:
                record = connection_service.reconcile(int(refresh_match[1]))
            except connections.ConnectionError as error:
                reason = {400: 'Bad Request', 404: 'Not Found', 409: 'Conflict',
                          502: 'Bad Gateway', 503: 'Service Unavailable'}[error.code]
                return _error_page(start_response, error.code, reason, str(error))
            return _redirect(start_response, connections.reading_url(record['item_id'], record['version_id'])
                             + f"#connection-{record['id']}")

        lesson_match = LESSON_ROUTE_RE.fullmatch(path)
        if lesson_match and lesson_match.group(2) == 'result':
            try:
                store.update_lesson_result(lesson_match.group(1), form.get('notebook_url', ''),
                                           form.get('review_notes', ''))
            except ValueError as error:
                return _error_page(start_response, 400, 'Bad Request', str(error))
            except KeyError:
                return _error_page(start_response, 404, 'Not Found', '找不到學習包。')
            return _redirect(start_response, f'/lessons/{lesson_match.group(1)}')

        if path == "/interests":
            terms_raw = form.get("terms", "")
            try:
                store.set_interests(terms_raw)
            except ValueError:
                return _error_page(start_response, 400, "Bad Request", "興趣關鍵字設定無效。")
            return _redirect(start_response, "/")

        if path == "/sync":
            refresh_daily_sources(prefetch=False, force=True)
            return _redirect(start_response, "/")

        if path == '/sync/daily':
            result = refresh_daily_sources(prefetch=False)
            return _respond(start_response,'200 OK',json.dumps(result,ensure_ascii=False).encode('utf-8'),
                            'application/json; charset=utf-8')

        if path == "/add":
            raw_url = form.get("url", "").strip()
            try:
                kind, canonical_url = validate_manual_url(raw_url)
                if kind == "python":
                    parsed_entry = parse_python_whatsnew_html(safe_fetch(canonical_url), canonical_url)
                else:
                    paper, version = ARXIV_HTML_PATH_RE.fullmatch(urlsplit(canonical_url).path).groups()
                    pdf_url = pdf_evidence.pdf_source('arxiv:' + paper, 'v' + version)
                    parsed_entry, _ = editions.originals(canonical_url, pdf_url)
                    if parsed_entry is None:
                        raise ValueError('No readable original')
                item_id, _ver_id, _changed = store.upsert_parsed_entry(parsed_entry)
            except (ValueError, OSError):
                return _error_page(
                    start_response,
                    400,
                    "Bad Request",
                    "不受允許或無法解析的手動來源網址。",
                )
            return _redirect(start_response, f"/items/{item_id}")

        item_match = ITEM_ROUTE_RE.fullmatch(path)
        if item_match and item_match.group(2) is not None:
            try:
                item_id = lessons.identifier(item_match.group(1))
            except ValueError:
                return _error_page(start_response, 400, 'Bad Request', '文章識別值無效。')
            action = item_match.group(2)
            item = store.get_item(item_id)
            if item is None:
                return _error_page(start_response, 404, "Not Found", "找不到指定的文章。")

            if action == 'source-reading':
                prompt = form.get('prompt','')
                try:
                    if 'bridge' in form:
                        row = local_reader.resume(item_id,form.get('version_id',''), form.get('request_id',''),form['bridge'])
                    else:
                        row = local_reader.ask(item_id,form.get('version_id',''), form.get('request_id',''),
                                               prompt,form.get('local_parent',''))
                        reading_state.acknowledge_question(item_id,row['version_id'],prompt)
                except study.StudyError as error:
                    reason = {400:'Bad Request',404:'Not Found',409:'Conflict'}[error.code]
                    return _error_page(start_response,error.code,reason,str(error))
                target = connections.reading_url(item_id,row['version_id'],f"source-turn-{row['id']}")
                return saved_location(target)

            if action == 'reading':
                try:
                    fields = {key: value for key, value in form.items() if key in reading.FIELDS}
                    result = reading_state.save(item_id,form.get('version_id',''),form.get('revision',''),fields)
                    code, reason = 200, 'OK'
                except study.StudyError as error:
                    code = error.code
                    reason = {400:'Bad Request',404:'Not Found',409:'Conflict'}[code]
                    result = {'error':str(error)}
                return _respond(start_response,f'{code} {reason}',json.dumps(result,ensure_ascii=False).encode('utf-8'),
                                'application/json; charset=utf-8')

            if action == 'teach':
                version = store.select_version(item['versions'],form.get('version_id',''))
                if version is None:
                    return _error_page(start_response,404,'Not Found','找不到指定版本。')
                # Existing teaching is a read, never an implicit second model call.
                existing = [r for r in study_service.records(item_id) if r['version_id'] == str(version['id'])
                            and r['mode'] == 'lesson' and r['state'] == 'completed']
                if existing:
                    return saved_location(connections.reading_url(item_id,version['id'],f"study-{existing[-1]['id']}"))
                if (study_provider is not None and version['scope'] in ('abstract','unavailable')
                        and item['key'].startswith('arxiv:')):
                    new_version_id, acquisition = editions.acquire(item, version)
                    item = store.get_item(item_id)
                    version = store.select_version(item['versions'],new_version_id)
                    if acquisition['state'] == 'unavailable':
                        message = ('尚未核對此篇的固定版號，已保留現有版本。'
                                   if acquisition['reason'] == 'edition_unresolved' else
                                   '同版 HTML 和 PDF 暫時沒有可讀正文，已保留現有版本。')
                        return _respond(start_response, '409 Conflict', _render_item_page(
                            item, version, study_error=message, study_form=form).encode('utf-8'))
                form = {**form,'mode':'lesson','version_id':str(version['id']), 'prompt':study.PAPER_LEARNING_GOAL}
                action = 'study'

            if action == 'lessons':
                try:
                    lesson_id = store.create_lesson(item_id, form.get('version_id', ''),
                        form.get('title', ''), form.get('learning_goal', ''),
                        form.get('blueprint_md', ''), form.get('origin', ''), form.get('parent_id', ''))
                except ValueError as error:
                    return _error_page(start_response, 400, 'Bad Request', str(error))
                return _redirect(start_response, f'/lessons/{lesson_id}')

            if action in ('study','progress'):
                try:
                    if action == 'study':
                        row = study_service.generate(item_id,form.get('version_id',''),form.get('request_id',''),
                            form.get('mode',''),form.get('prompt',''),form.get('parent_id',''))
                        if row['mode'] == 'qa' and row['state'] in ('pending','completed'):
                            reading_state.acknowledge_question(item_id,row['version_id'],row['prompt'])
                    else:
                        row = study_service.save_progress(item_id,form.get('version_id',''),form.get('request_id',''),
                            form.get('record_id',''),form.get('understanding',''),form.get('questions',''))
                except study.StudyError as error:
                    reason = {400:'Bad Request',404:'Not Found',409:'Conflict',503:'Service Unavailable'}[error.code]
                    version = store.select_version(item['versions'],form.get('version_id',''))
                    if version is None:
                        return _error_page(start_response,error.code,reason,str(error))
                    return _respond(start_response,f'{error.code} {reason}',_render_item_page(
                        item,version,study_error=str(error),study_form=form).encode('utf-8'))
                location = connections.reading_url(item_id, row['version_id'], f"{action}-{row['id']}")
                return saved_location(location)

            if action == 'connections':
                try:
                    record = connection_service.generate(item_id, form.get('version_id', ''),
                        form.get('request_id', ''), form.get('focus', ''))
                except connections.ConnectionError as error:
                    reason = {400: 'Bad Request', 404: 'Not Found', 409: 'Conflict',
                              502: 'Bad Gateway', 503: 'Service Unavailable'}[error.code]
                    return _error_page(start_response, error.code, reason, str(error))
                return _redirect(start_response, connections.reading_url(item_id, record['version_id'])
                                 + f"#connection-{record['id']}")

            if action == "questions":
                try:
                    version_id = qa.ask(store, answer_provider, item_id,
                                        form.get('version_id', ''), form.get('request_id', ''),
                                        form.get('question', ''))
                except qa.QAError as error:
                    reasons = {400: 'Bad Request', 409: 'Conflict',
                               502: 'Bad Gateway', 503: 'Service Unavailable'}
                    return _error_page(start_response, error.code, reasons[error.code], str(error))
                saved_item = store.get_item(item_id)
                record = next(q for q in saved_item['questions']
                              if q['request_id'] == form.get('request_id', ''))
                location = (f'/items/{item_id}?' + urlencode({'version': version_id})
                            + f"#question-{record['id']}")
                return _redirect(start_response, location)

            if action == "fetch":
                if not item["key"].startswith("arxiv:"):
                    return _error_page(
                        start_response, 400, "Bad Request", "此來源不支援 arXiv 全文擷取。"
                    )
                target_v = (store.select_version(item['versions'],form['version_id'])
                            if form.get('version_id') else store.select_default_version(item['versions']))
                if target_v is None:
                    return _error_page(start_response,404,'Not Found','找不到指定版本。')
                fetched_version, acquisition = editions.acquire(item, target_v)
                day = daily_refresh.latest_day()
                if day and acquisition['state'] in ('ready','partial'):
                    refresh_daily_briefs(day,item_id=item_id,manual=True)
                return _redirect(start_response, connections.reading_url(item_id,fetched_version))

            if action == "status":
                new_status = form.get("status", "").strip()
                try:
                    store.set_item_status(item_id, new_status)
                except (ValueError, KeyError):
                    return _error_page(start_response, 400, "Bad Request", "無效的閱讀狀態。")
                return _redirect(start_response, f"/items/{item_id}")

            if action == "notes":
                version_id = form.get("version_id", "").strip()
                anchor = form.get("anchor", "").strip()
                text = form.get("text", "")
                try:
                    note = store.add_note(item_id, version_id, anchor, text)
                except ValueError:
                    return _error_page(
                        start_response, 400, "Bad Request", "筆記欄位、版本或段落錨點無效。"
                    )
                loc = f"/items/{item_id}?" + urlencode({"version": note["version_id"]})
                return _redirect(start_response, loc)

            if action == "relations":
                version_id = form.get("version_id", "").strip()
                anchor = form.get("anchor", "").strip()
                concept = form.get("concept", "")
                kind = form.get("kind", "")
                try:
                    rel = store.add_relation(item_id, version_id, anchor, concept, kind)
                except ValueError:
                    return _error_page(
                        start_response, 400, "Bad Request", "概念關係欄位、版本或段落錨點無效。"
                    )
                loc = f"/items/{item_id}?" + urlencode({"version": rel["version_id"]})
                return _redirect(start_response, loc)

        return _error_page(start_response, 404, "Not Found", "找不到請求的操作路徑。")

    app.refresh_daily_sources = refresh_daily_sources
    return app
