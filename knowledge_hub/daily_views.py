"""Render the stored layers and reading actions for one daily recommendation."""

from urllib.parse import quote, urlsplit

from .connections import reading_url
from .excerpts import preview
from .sources import HF_FEED_LABEL, SAFE_ANCHOR_RE
from .study_views import e, hidden


_PROVIDERS = {
    'arxiv': 'arXiv', 'huggingface': 'arXiv', 'pytorch': 'PyTorch', 'python': 'Python',
    'vllm': 'vLLM', 'sglang': 'SGLang', 'tensorrt-llm': 'TensorRT-LLM',
    'transformers': 'Hugging Face Transformers',
}
_REASONS = {'reading': '接上閱讀', 'question': '回到未解問題'}
_QUICK_READ_LABELS = (
    ('mechanism', '核心機制'), ('metrics', '實測與指標'), ('comparison', '和什麼比較'), ('limits', '限制與代價'),
)
_REFRESH_STATES = {'running': '更新中', 'completed': '已完成', 'partial': '部分完成', 'failed': '更新失敗'}
_SOURCE_STATES = {'updated': '已更新', 'unchanged': '無變更', 'failed': '讀取失敗', 'unattempted': '尚未嘗試'}
_BRIEF_MODES = {'model': '模型簡報', 'local': '本地簡報', 'abstract_fallback': '摘要降級', 'unavailable': '尚無簡報'}


def _href(url, *, relative=False):
    if not isinstance(url, str) or any(c.isspace() or c == '\\' or ord(c) < 32 or ord(c) == 127 for c in url):
        return ''
    if relative and url.startswith('/') and not url.startswith('//'):
        return url
    try:
        parsed = urlsplit(url)
        if parsed.scheme == 'https' and parsed.hostname and parsed.username is None and parsed.password is None:
            return url
    except ValueError:
        pass
    return ''


def _reasons(items):
    rows, seen = [], set()
    for item in items:
        caption = _REASONS.get(item.get('kind'))
        title = item.get('title')
        if not caption or not title:
            continue
        href = _href(item.get('href'), relative=True)
        identity = caption, title, href
        if identity in seen:
            continue
        seen.add(identity)
        terms = '、'.join(dict.fromkeys(filter(None, item.get('matches', ()))))
        suffix = f' · {e(terms)}' if terms else ''
        label = f'<a href="{e(href)}">{e(title)}</a>' if href else e(title)
        rows.append(f'<li><strong>{caption}</strong> {label}{suffix}</li>')
    return '<ul class="discovery-reasons">' + ''.join(rows) + '</ul>' if rows else ''


def _choices(item_path, version_id, csrf, day, choice, title):
    if choice in ('skip', 'later'):
        options = [('undo', '撤銷略過' if choice == 'skip' else '撤銷待讀')]
    else:
        options = [('skip', '略過'), ('later', '待讀')]
    buttons = ''.join(
        f'<button class="text-action" type="submit" name="choice" value="{value}" '
        f'aria-label="{e(label + "《" + title + "》")}">{label}</button>'
        for value, label in options
    )
    return (f'<form class="daily-choice" method="post" action="/items/{item_path}/daily-choice">'
            f'{hidden("csrf", csrf)}{hidden("version_id", version_id)}{hidden("day", day)}{buttons}</form>')


def _full_text_form(item, item_path, version, csrf):
    if version['scope'] not in ('abstract', 'unavailable') or not item['key'].startswith('arxiv:'):
        return ''
    return (f'<form method="post" action="/items/{item_path}/fetch" '
            'data-busy="正在取得這篇的全文並更新速讀…">'
            f'{hidden("csrf", csrf)}{hidden("version_id", version["id"])}'
            '<button class="text-action" type="submit">取得全文並更新速讀</button></form>')


def _metric_link(reference, item_path, version_id):
    if not isinstance(reference, dict) or str(reference.get('version_id')) != str(version_id):
        return ''
    anchor = reference.get('anchor')
    if not isinstance(anchor, str) or len(anchor) > 200 or not SAFE_ANCHOR_RE.fullmatch(anchor):
        return ''
    href = reference.get('href')
    if href not in (reading_url(item_path, version_id, anchor),
                    reading_url(item_path, version_id, 'kh-visual-' + anchor)):
        return ''
    return f'<a class="briefing-metric" href="{e(href)}">核對量測原文</a>'


def _legacy_why(pick):
    strength = pick.get('evidence_strength', 0)
    if strength >= 2:
        angle = '先看作者報告的比較或數字，再核對資料集、基準和適用範圍。'
    elif strength:
        angle = '摘要指出可對照的評測，讀原文時先確認它測了什麼。'
    else:
        angle = '先看它提出的做法是否切中你的問題，再到原文找驗證。'
    has_history = any(row.get('kind') in _REASONS for row in pick.get('reasons', ()))
    return angle if has_history else '探索候選。' + angle


def _layers(pick, item_path, version_id, title):
    brief = pick.get('brief') or {}
    l1, l2 = brief.get('l1') or {}, brief.get('l2') or {}
    change = l1.get('change', '')
    highlight = l1.get('highlight', '')
    if change and highlight.startswith(change):
        highlight = highlight[len(change):].strip()
    if not change:
        change, highlight = highlight, ''
    relevance = l1.get('relevance') or pick.get('spectrum_reason') or _legacy_why(pick)
    if category := pick.get('category'):
        compact = relevance.removeprefix(f'屬於{category}。').strip()
        relevance = compact or pick.get('spectrum_reason') or relevance
    lede = f'<p class="briefing-lede">{e(" ".join(filter(None, (change, highlight))))}</p>' if change else ''
    if not change and (excerpt := pick.get('evidence')):
        quote = preview(excerpt, 160)[0]
        label = '摘要節錄' if pick['version']['scope'] == 'abstract' else '原文節錄'
        lede = (f'<p class="briefing-source-preview reader-native-only"><strong>{label}</strong> {e(quote)}</p>'
                '<details class="briefing-excerpt-toggle" data-daily-evidence><summary>查看來源節錄</summary>'
                f'<blockquote class="briefing-excerpt"><span class="meta">作者來源節錄</span>'
                f'<p>{e(excerpt)}</p></blockquote></details>')
    visible = (f'<div class="daily-l1 briefing-l1">{lede}'
               f'<p class="briefing-relevance"><strong>值得讀的理由</strong> {e(relevance)}</p></div>')
    if brief.get('state') == 'degraded':
        visible += '<p class="briefing-warning" role="status">速讀未完成，先看目前可用的來源。</p>'

    metric_link = _metric_link(brief.get('metrics_source'), item_path, version_id)
    detail = ''.join(
        f'<div><dt>{label}</dt><dd>{e(l2[key])}{metric_link if key == "metrics" else ""}</dd></div>'
        for key, label in _QUICK_READ_LABELS if l2.get(key)
    )
    reasons = _reasons(pick.get('reasons', ()))
    detail_heading = '展開 1 分鐘速讀' if detail else '查看選文脈絡'
    if detail:
        detail = f'<dl class="daily-l2 briefing-read-grid">{detail}</dl>'
    if brief.get('state') == 'degraded' and brief.get('error'):
        detail += f'<p class="meta">{e(brief["error"])}</p>'
    detail += reasons
    if not detail:
        return visible, ''
    return (visible, f'<details class="daily-quick-read briefing-quick-read">'
                     f'<summary aria-label="{e(detail_heading + "《" + title + "》")}">{detail_heading}</summary>'
                     f'<div class="briefing-read-content">{detail}</div></details>')


def story(pick, index, csrf, day):
    """A self-contained server-rendered card. Native disclosure needs no script."""
    from .reader_views import SCOPE, date_label, domain_summary

    item, version = pick['item'], pick['version']
    item_path = quote(str(item['id']), safe='')
    title = item['title']
    link = reading_url(item_path, version['id'])
    choice = pick.get('choice', '')
    controls = _choices(item_path, version['id'], csrf, day, choice, title)
    heading = f'<h2><a href="{e(link)}">{e(title)}</a></h2>'
    base = f'id="daily-{item_path}" class="daily-story briefing-story'
    if choice == 'skip':
        return (f'<article {base} daily-story-skipped">{heading}<div class="briefing-skipped">'
                f'<span>已略過</span>{controls}</div></article>')

    l1, l2 = _layers(pick, item_path, version['id'], title)
    hf = version['label'] == HF_FEED_LABEL
    provider = 'Hugging Face Papers' if hf else _PROVIDERS.get(item['source_name'], item['source_name'])
    date = date_label(version.get('published_at'))
    publisher = ' · '.join(value for value in (provider, date) if value)
    scope = SCOPE.get(version['scope'], '原文範圍未標示')
    label = 'arXiv 版次待核對' if hf else version['label']
    edition = ' · '.join(value for value in (label, scope) if value)
    status = '<span class="briefing-later" role="status">已加入待讀</span>' if choice == 'later' else ''
    source = _href(version.get('source_url'))
    source_label = (f'<a class="briefing-publisher" href="{e(source)}" rel="noopener noreferrer">{e(publisher)}</a>'
                    if source else f'<span class="briefing-publisher">{e(publisher)}</span>')
    category = f'<span class="briefing-topic">{e(pick["category"])}</span>' if pick.get('category') else ''
    full_text = _full_text_form(item, item_path, version, csrf)
    lead = ' briefing-lead' if index == 0 else ''
    domains = pick.get('domains', {})
    if domains and pick.get('category') in domains:
        category = ''
    return (f'<article {base}{lead}"><div class="briefing-story-meta">{category}'
            f'<span class="briefing-edition">{e(edition)}</span>{status}</div>{heading}{l1}'
            f'{l2}{domain_summary(domains)}<div class="briefing-actions"><a class="button briefing-start" href="{e(link)}">開始一起讀</a>'
            f'{full_text}{controls}</div>{source_label}</article>')


def _refresh_time(stamp, fallback='—'):
    return f'<time datetime="{e(stamp)}">{e(stamp[:19].replace("T", " "))}</time>' if stamp else fallback


def status_page(report, csrf):
    from .reader_views import SCOPE, shell

    today, receipt = report['local_date'], report['today'] or {}
    refresh_state = _REFRESH_STATES.get(receipt.get('state'), '今日尚未執行')
    timing = (
        ('今日狀態', e(refresh_state)),
        ('實際開始', _refresh_time(receipt.get('started_at_taipei'))),
        ('實際結束', _refresh_time(receipt.get('finished_at_taipei'))),
        ('下次例行排程', _refresh_time(report['next_scheduled_at'])),
    )
    slots = ''.join(f'<div><dt>{label}</dt><dd>{value}</dd></div>' for label, value in timing)
    diagnostic = f'<p class="notice">{e(receipt["error"])}</p>' if receipt.get('error') else ''
    parts = [
        '<div class="refresh-status"><a class="back-link" href="/">← 回到今日科技簡報</a>'
        '<h1>每日簡報更新</h1>'
        f'<p class="meta">台灣時間 · <time datetime="{e(today)}">{e(today)}</time></p>'
        f'<dl class="refresh-timing">{slots}</dl>{diagnostic}<div class="refresh-details">'
        '<section aria-labelledby="refresh-cards"><h2 id="refresh-cards">最新一期選文</h2>',
    ]
    if report['cards']:
        edition = '今日一期' if report['selection_day'] == today else '最近一期'
        parts.append(f'<p class="meta">{edition} · {e(report["selection_day"])} · {len(report["cards"])} 篇</p><ol>')
        for card in report['cards']:
            href = reading_url(card['item_id'], card['version_id'])
            brief = card['brief']
            label = _BRIEF_MODES[brief['mode']]
            model = f' · {e(brief["model"])}' if brief.get('model') else ''
            scope = SCOPE.get(card['scope'], '原文範圍未標示')
            parts.append(f'<li><a href="{e(href)}">{e(card["title"])}</a>'
                         f'<p class="meta">{e(card["version_label"])} · {e(scope)} · 版本 ID {e(card["version_id"])}'
                         f'<br>L1/L2 · {label}{model}</p></li>')
        parts.append('</ol>')
    else:
        parts.append('<p class="meta">還沒有已發布的簡報。</p>')

    sources, succeeded, failed, pending = [], 0, 0, 0
    for name, source in report['sources'].items():
        stamp = source.get('last_fetched_at_taipei')
        state = source['state']
        if not stamp or stamp[:10] != today:
            pending += 1
        elif state == 'failed':
            failed += 1
        elif state in ('updated', 'unchanged'):
            succeeded += 1
        attempt = _refresh_time(stamp, '尚未嘗試')
        last_success = source.get('last_success_at_taipei')
        previous = f'<br>上次成功 {_refresh_time(last_success)}' if last_success and last_success != stamp else ''
        error = f'<br>{e(source["last_error"])}' if source.get('last_error') else ''
        sources.append(f'<li><strong>{e(name)}</strong> · {e(_SOURCE_STATES.get(state, state))}'
                       f'<p class="meta">上次嘗試 {attempt}{previous}{error}</p></li>')
    parts.append('</section><section aria-labelledby="refresh-sources"><h2 id="refresh-sources">公開來源</h2>'
                 f'<p class="meta">今日 {succeeded} 筆成功 · {failed} 筆失敗 · {pending} 筆尚未嘗試</p>'
                 '<ul>' + ''.join(sources) + '</ul></section></div>')
    if report['recent_failures']:
        history = ''.join(f'<li>{_refresh_time(row["attempted_at_taipei"])} · {e(row["name"])} · {e(row["error"])}</li>'
                          for row in report['recent_failures'])
        parts.append(f'<details><summary>最近的來源失敗紀錄</summary><ul>{history}</ul></details>')
    return shell('每日簡報更新', csrf, ''.join(parts) + '</div>', styles=('briefing',))
