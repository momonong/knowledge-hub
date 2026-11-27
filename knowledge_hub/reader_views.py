"""Reading-first product views. Model output and private drafts are always escaped."""
from datetime import datetime, timezone, timedelta
import re
import secrets
from urllib.parse import unquote

from . import hive_guide, hive_reader, learning, study_views, visuals
from .connections import reading_url
from .domains import article_domains
from .sources import HF_FEED_LABEL, SAFE_ANCHOR_RE
from .study_views import e, hidden

SCOPE = {'full': '全文文字', 'partial': '部分原文', 'abstract': '僅有摘要', 'unavailable': '原文待取得'}
ROLE = {'problem': '從問題開始', 'mechanism': '看懂它怎麼運作', 'example': '走一次具體例子',
        'comparison': '與舊知放在一起看', 'check': '試著用自己的話說', 'answer': '接著想下去'}
CONNECTION_ACTION = {'source': '查看來源卡', 'starter': '回到先備教材', 'read': '回到已讀文章',
                     'note': '回到筆記', 'question': '回到提問', 'understanding': '回到理解紀錄'}


def shell(title, csrf, body, active='today', *, styles=()):
    links = [('/', '今天讀什麼', 'today'), ('/reading', '接續與理解', 'reading'),
             ('/library', '知識庫', 'library')]
    nav = ''.join(f'<a href="{url}"{chr(32)+"aria-current=page" if key == active else ""}>{label}</a>'
                  for url, label, key in links)
    page_styles = ''.join(f'<link rel="stylesheet" href="/static/{e(name)}.css">' for name in styles)
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1, interactive-widget=resizes-content">
      <title>{e(title)} — Knowledge Hub</title><link rel="icon" sizes="any" href="/static/book.svg" type="image/svg+xml">
      <link rel="stylesheet" href="/static/palette.css"><link rel="stylesheet" href="/static/reader.css">{page_styles}
      <script src="/static/reader.js" defer></script><script src="/static/layers.js" defer></script></head><body>
      <header class="app-header"><div class="header-inner"><a class="wordmark" href="/">
      <img class="kh-book" src="/static/book.svg" alt="" width="32" height="32">
      <span class="reader-web-only">Knowledge Hub</span><span class="reader-native-only">研讀手記</span></a>
      <nav aria-label="主要導覽">{nav}</nav><a class="utility" href="/manage">管理</a></div></header>
      <main class="workspace"><input type="hidden" id="page-csrf" name="csrf" value="{e(csrf)}">{body}</main>
      <footer class="app-footer"><span class="reader-web-only">閱讀、提問與理解，接著往下走。</span><a class="reader-web-only" href="/export.json">匯出我的資料</a></footer></body></html>'''


def date_label(value):
    if not value:
        return '日期未標示'
    try:
        date = datetime.fromisoformat(value.replace('Z','+00:00'))
        return date.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d') if date.tzinfo else value[:10]
    except ValueError:
        return '日期未標示'


def age_label(value):
    try:
        published = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - published).days
        if days < 0:
            return '發布日期待核對'
        return '近一週發布' if days <= 7 else '較早文章，仍可深入閱讀'
    except (ValueError, TypeError):
        return '發布日期待核對'


def resumable(store, reading):
    return reading.continuations(page_size=5)['rows']


def edition_label(row):
    if row['label'] == HF_FEED_LABEL:
        return 'Hugging Face 推薦摘要'
    return f'{row["label"]} · {SCOPE[row["scope"]]}'


def continuation_links(row):
    anchor = row['anchor'] or 'reader-start'
    href = reading_url(row['item_id'], row['version_id'], anchor)
    preview = row['open_questions'] or row['reflection'] or row['question_draft']
    original = ''
    if row.get('study_id'):
        receipt_anchor = f'study-{row["study_id"]}'
        label = {'pending':'查看原工作進度', 'unknown':'查看結果待查的原工作',
                 'failed':'查看上次未完成的原因'}.get(row['study_state'])
        if anchor == receipt_anchor:
            preview = preview or label or '上次的回答與引用已保存。'
        elif label:
            receipt_url = reading_url(row['item_id'], row['version_id'], receipt_anchor)
            original = f' <a class="text-action" href="{e(receipt_url)}">{label}</a>'
    if row.get('turn_id') and anchor == f'source-turn-{row["turn_id"]}':
        preview = preview or row['turn_prompt']
    if row.get('followup_id') and anchor == f'l4-{row["followup_id"]}':
        preview = preview or row['followup_question']
    return href, preview or '閱讀位置已保留。', original


def domain_summary(domains):
    if not domains:
        return ''
    labels = '、'.join(f'<span title="{e(evidence)}">{e(label)}</span>'
                      for label, evidence in domains.items())
    return f'<p class="meta" data-related-domains><strong>相關領域</strong> {labels}</p>'


def source_outline(blocks):
    """Link to section headings retained from the selected source version."""
    anchors = {block['anchor'] for block in blocks}
    links = []
    for block in blocks:
        anchor, text = block['anchor'], ' '.join(block['text'].split())
        abstract = anchor == 'abstract'
        section = anchor + '.p1' in anchors or re.fullmatch(r'S[0-9]+(?:\.S+[0-9]+)*', anchor)
        if not SAFE_ANCHOR_RE.fullmatch(anchor) or not (abstract or section and 0 < len(text) <= 140):
            continue
        label = '摘要' if abstract else text
        links.append(f'<li><a href="#{e(anchor)}">{e(label)}</a></li>')
        if len(links) == 24:
            break
    if not links:
        return ''
    return ('<details class="source-outline reader-native-only"><summary>章節目錄</summary>'
            '<ul>' + ''.join(links) + '</ul></details>')


def home(store, library, reading, csrf, daily_status=None, *, picks=(), selected_day=None, today=None):
    from .daily_views import story

    display_day = selected_day or today or datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    stamp = datetime.fromisoformat(display_day)
    day_text = f'{stamp.year} 年 {stamp.month} 月 {stamp.day} 日'
    earlier = bool(selected_day and today and selected_day != today)
    heading = '科技簡報' if earlier else '今日科技簡報'
    count = f'<span class="briefing-count">{len(picks)} 篇</span>' if picks else ''
    sources = store.get_sources()
    last = max((s['last_success_at'] for s in sources.values() if s['last_success_at']), default='')
    if daily_status and daily_status['state'] == 'running':
        health = '今日簡報更新中'
    elif any(s['state'] == 'failed' for s in sources.values()):
        health = '部分來源讀取失敗，已保留先前內容'
    elif daily_status and (daily_status.get('error') or daily_status['state'] == 'partial'):
        health = '今日簡報有部分內容待更新'
    else:
        health = ''
    health_label = f'{health} · 各來源更新與失敗紀錄' if health else '來源狀態'
    parts = [f'<section class="briefing" aria-labelledby="briefing-heading" data-daily>'
             f'<header class="briefing-masthead"><div>'
             f'<p class="briefing-date"><time datetime="{e(display_day)}">{day_text}</time>{count}</p>'
             f'<h1 id="briefing-heading">{heading}</h1></div>'
             '<div class="briefing-update"><form method="post" action="/sync" data-busy="正在更新公開來源…">'
             f'{hidden("csrf", csrf)}<button class="text-action" type="submit">更新簡報</button></form>'
             f'<a id="daily-status" href="/discovery/status">{health_label}</a></div></header>']
    if earlier:
        parts.append('<p class="briefing-edition-note">今日簡報尚未發布，先看這期。</p>')
    parts.append('<div class="briefing-layout"><div class="briefing-stories">')
    if not picks:
        message = ('目前沒有足夠的新文章。可以先接著讀，或稍後更新簡報。' if daily_status
                   else '本期簡報尚未備妥。你可以先接著讀，或更新簡報。')
        parts.append(f'<div class="briefing-empty"><h2>今日選文</h2><p>{e(message)}</p></div>')
    parts.extend(story(pick, index, csrf, display_day) for index, pick in enumerate(picks))
    parts.append('</div><aside class="briefing-rail" aria-label="閱讀與選文資訊">')
    recent = resumable(store, reading)
    if recent:
        row = recent[0]
        href, preview, original = continuation_links(row)
        parts.append('<section class="briefing-resume"><h2>接續閱讀</h2>'
                     f'<h3><a href="{e(href)}">{e(row["title"])}</a></h3>'
                     f'<p class="briefing-preview">{e(preview[:220])}</p>'
                     f'<div class="briefing-resume-actions"><a class="text-action" href="{e(href)}">繼續閱讀</a>{original}</div>'
                     f'<p class="meta">{e(date_label(row["updated_at"]))} · {e(edition_label(row))}</p></section>')
        if len(recent) > 1:
            parts.append('<details class="briefing-rail-section"><summary>其他閱讀進度</summary>')
            for row in recent[1:]:
                href, _, original = continuation_links(row)
                parts.append(f'<a class="background-link" href="{e(href)}">{e(row["title"])} · {e(edition_label(row))}</a>{original}')
            parts.append('</details>')
        parts.append('<a class="briefing-rail-link text-action" href="/reading">查看全部閱讀進度</a>')
    parts.append('<details class="briefing-rail-section"><summary>選文與來源</summary>'
                 '<p>每天台灣時間 07:00 更新。</p>')
    freshness = f'上次成功讀取來源：{date_label(last)}' if last else '尚無成功更新紀錄'
    parts.append(f'<p class="meta">{e(freshness)}</p>')
    if daily_status and daily_status.get('error'):
        parts.append(f'<p>{e(daily_status["error"])}</p>')
    parts.append('</details><details class="briefing-rail-section"><summary>補讀基礎</summary>')
    mappings = [dict(d, source_id=s['id']) for s in library.list_sources(starter_only=True) for d in s['documents']]
    items = {d['item_id'] for d in mappings}
    all_items = {item_id: item for item_id in items if (item := store.get_item(item_id)) is not None}
    guides = learning.available_guides(mappings, all_items)
    for sid in ('rag-original-rag', 'rag-react', 'rag-longmemeval'):
        guide = guides.get(sid)
        if guide:
            parts.append(f'<a class="background-link" href="{e(reading_url(guide["item_id"], guide["version_id"]))}">{e(guide["title"])}</a>')
    parts.append('<a class="briefing-rail-link text-action" href="/library">瀏覽知識庫</a></details></aside></div></section>')
    return shell(heading, csrf, ''.join(parts), styles=('briefing',))


def citations(cites, safe_href):
    body = []
    for c in cites:
        body.append(f'<blockquote><p>{e(c["quote"])}</p><a href="{e(c["reading_url"])}">原文段落 {e(c.get("source_id", ""))}</a>'
                    f' · <a href="{safe_href(c["source_url"])}" rel="noopener noreferrer">出版者來源</a></blockquote>')
    return '<details class="citations"><summary>核對原文依據</summary>' + ''.join(body) + '</details>'


def context_limitations(record):
    """Show the saved system boundary even when the model does not repeat it."""
    limitations = record.get('payload', {}).get('limitations', '')
    if '對話因輸入容量限制已省略' not in limitations:
        return ''
    return ('<p class="notice" data-context-limitations><strong>這次使用的對話脈絡有節錄。</strong> '
            + e(limitations) + '</p>')


def lesson_sections(record, safe_href):
    parts = [context_limitations(record)]
    for index, section in enumerate(record['answer']['sections']):
        parts.append(f'<section class="teaching-section" id="lesson-section-{index}" data-position>'
                     f'<p class="section-role">{e(ROLE[section["role"]])} · {e(study_views.KINDS[section["kind"]])}</p>'
                     f'<h3>{e(section["title"])}</h3><div class="prose">{e(section["text"])}</div>'
                     + citations(section['citations'], safe_href) + '</section>')
    parts.append(f'<div class="limits"><h3>這份解說的邊界</h3><p>{e(record["answer"]["limitations"])}</p></div>')
    return ''.join(parts)


def curated_guide(guide, safe_href):
    parts = ['<p class="meta">固定版本策展教材 · 解讀仍可核對與追問</p>', f'<p class="learning-goal">{e(guide["goal"])}</p>']
    for index, (key, title) in enumerate([('mechanism','先看問題與機制'), ('example','走一次具體例子'),
                                         ('connections','接回你已經見過的方法'), ('limitations','什麼不能直接推論')]):
        parts.append(f'<section class="teaching-section" id="lesson-section-{index}" data-position><h3>{title}</h3>'
                     f'<div class="prose">{e(guide[key])}</div></section>')
    parts.append(citations(guide['evidence'], safe_href))
    parts.append(f'<section class="self-check"><h3>試著用自己的話說</h3><p>{e(guide["question"])}</p>'
                 f'<details><summary>看參考解釋</summary><p>{e(guide["answer"])}</p></details></section>')
    return ''.join(parts)


def source_sections(answer, safe_href, *, lesson=False):
    parts = []
    for index, section in enumerate(answer['sections']):
        tag = 'details' if lesson else 'section'
        title = 'summary' if lesson else 'h4'
        opened = ' open' if lesson and index == 0 else ''
        parts.append(f'<{tag} class="source-explanation"{opened}><{title}>{e(section["title"])}</{title}>')
        parts.append(f'<p class="prose">{e(section["text"])}</p>')
        for cite in section['citations']:
            if cite['quote'] not in section['text']:
                parts.append(f'<blockquote><p>{e(cite["quote"])}</p></blockquote>')
            parts.append(f'<p class="meta"><a href="{e(cite["reading_url"])}">對照原文 {e(cite["source_id"])} · {e(cite["anchor"])}</a> · '
                         f'<a href="{safe_href(cite["source_url"])}" rel="noopener noreferrer">出版者版本 ↗</a></p>')
        parts.append(f'</{tag}>')
    parts.append(f'<p class="meta">{e(answer["limitations"])}</p>')
    return ''.join(parts)


def answer_note_button(row, *, include_quotes=True):
    answer = row['answer']
    if answer['sections']:
        lines = [row['prompt']]
        for section in answer['sections']:
            lines.append(section['text'])
            for c in section['citations']:
                excerpt = c['quote'] if include_quotes and c['quote'] not in section['text'] else ''
                lines.append(f'{excerpt}〔原文 {c["item_id"]}/{c["version_id"]} #{c["anchor"]}〕')
        field, label = 'reflection', '加入我的理解'
    else:
        lines, field, label = [row['prompt']], 'open_questions', '留在未解問題'
    return (f'<button type="button" class="text-action" data-copy-source="{field}">{label}</button>'
            f'<span hidden data-source-note>{e(chr(10).join(lines))}</span>')


def saved_source_turn(row, safe_href):
    origin = (f'<p class="meta"><a href="{e(row["origin"])}">回到上次留下這個問題的地方</a></p>'
              if row.get('origin') else '')
    return (f'<article id="source-turn-{row["id"]}" class="chat-turn" data-position data-private><p class="chat-question">{e(row["prompt"])}</p>'
            + origin
            + source_sections(row['answer'],safe_href)
            + f'<button type="button" class="text-action" data-follow-source="{row["id"]}">沿這一題追問</button> '
            + answer_note_button(row) + '</article>')


def _related_destination(href):
    if re.fullmatch(r'/library/[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', href):
        return href
    path, _, anchor = href.partition('#')
    if (re.fullmatch(r'/items/[1-9][0-9]{0,18}\?version=[1-9][0-9]{0,18}', path)
            and (not anchor or re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', unquote(anchor)))):
        return href
    return ''


def related_knowledge_panel(recommendations):
    cards = []
    for row in recommendations:
        kind = row['kind']
        action = CONNECTION_ACTION.get(kind)
        href = _related_destination(row['href'])
        title, reason, excerpt = (row[field].strip() for field in ('title', 'reason', 'excerpt'))
        if not (href and action and title and reason):
            continue
        quote = f'<blockquote data-connection-excerpt>{e(excerpt)}</blockquote>' if excerpt else ''
        cards.append(f'<li><details data-knowledge-kind="{e(kind)}"><summary>'
                     f'<strong>{e(title)}</strong><span data-connection-reason>{e(reason)}</span></summary>'
                     f'<div class="room-connection-detail">{quote}'
                     f'<a href="{e(href)}" data-connection-destination>{action}</a></div></details></li>')
        if len(cards) == 4:
            break
    if not cards:
        return ('<section class="room-connections" data-knowledge-connections data-private>'
                '<p class="meta">目前還沒有找到可直接對照的庫內紀錄。</p></section>')
    return ('<section class="room-connections" data-knowledge-connections data-private '
            'aria-labelledby="room-connections-title"><details data-connections-disclosure><summary>'
            '<span id="room-connections-title">這篇連到你庫裡的舊知</span> '
            f'<span class="room-connections-count">{len(cards)} 筆</span></summary>'
            '<ul role="list">' + ''.join(cards) + '</ul></details></section>')


def detail(item, version, context, service, reading, csrf, safe_href, *, error='', form=None, local_reader=None,
           related_knowledge=()):
    form = form or {}
    item_id, version_id = item['id'], version['id']
    state = reading.get(item_id, version_id)
    records = [r for r in service.records(item_id) if r['version_id'] == str(version_id)]
    diagnostics = service.diagnostics(item_id, version_id)
    complete = [r for r in records if r['state'] == 'completed']
    lesson = next((r for r in reversed(complete) if r['mode'] == 'lesson'), None)
    guide = context['focal'].get('teaching_guide')
    progress = service.progress(item_id, version_id)
    previous = progress[0] if progress else {}
    blocked = service.unresolved(item_id, version_id)
    availability = service.availability()
    off = ' disabled aria-disabled="true"' if not availability['available'] or blocked else ''
    base = hidden('csrf',csrf) + hidden('version_id',version_id)
    latest = complete[-1] if complete else None
    visual_source = visuals.paper_url(item['key'], version['label'])
    has_hive_guide = visual_source == hive_reader.PAPER
    title = version.get("title") or item["title"]
    domains = article_domains(title, version["blocks"])
    context_error = ''
    try:
        public_context = service.context(item_id, version_id)
    except study_views.StudyError as exc:
        public_context, context_error = None, str(exc)
    source_turns = context.get('source_turns', [])
    sources = (public_context or {}).get('sources',[])
    parts = [f'<article class="reading-room" data-reader data-reading-room data-item="{item_id}" data-version="{e(version_id)}" '
             f'data-revision="{state["revision"]}" data-tab="{e(state["tab"])}" data-anchor="{e(state["anchor"])}" '
             f'data-reading-scope="{e(version["scope"])}" id="reader-start">',
             '<a class="back-link" href="/">← 回到今天的閱讀</a>',
             f'<header class="reader-heading"><h1>{e(title)}</h1>'
             f'<p class="meta">{e(date_label(version["published_at"]))} · {e(version["label"])} · {e(SCOPE[version["scope"]])}</p>'
             f'{domain_summary(domains)}</header>',
             '<div class="room-navigation"><div class="room-tabs" role="tablist" aria-label="閱讀內容">'
             '<button id="room-guide-tab" type="button" role="tab" aria-selected="true" data-tab-button="lesson" aria-controls="panel-lesson">導讀</button>'
             '<button id="room-source-tab" type="button" role="tab" aria-selected="false" data-tab-button="original" aria-controls="panel-original">原文</button>'
             '</div><button class="room-tool" type="button" data-room-ask aria-controls="conversation" aria-expanded="false">追問</button>'
             '<a class="room-tool room-notes-link reader-native-only" href="#study-save">筆記</a></div>'
             '<p class="room-save-status" role="status" data-room-save-status></p>']
    if error:
        parts.append(f'<p role="alert" class="notice warning">{e(error)} 輸入仍保留。</p>')
    parts.append('<div class="room-panes">'
                 '<section id="panel-lesson" class="room-pane room-guide" data-reader-panel="lesson" '
                 'aria-label="導讀" tabindex="0">')
    from . import layer_views
    parts.append(related_knowledge_panel(related_knowledge))
    parts.append(layer_views.guide_panel())
    parts.append('<details class="room-fold"><summary>先前教材與教學紀錄</summary>')
    if has_hive_guide and context.get('hive_videos'):
        parts.append(hive_reader.video_section(item_id, version_id))
    parts.append('<div id="study">')
    if lesson:
        parts.append(f'<p class="meta">依本篇與背景原文產生 · 教材與引用已保存</p><article id="study-{lesson["id"]}" data-study-state="completed">'
                     + lesson_sections(lesson, safe_href) + '</article>')
    elif guide:
        parts.append(curated_guide(guide, safe_href))
    else:
        parts.append('<details class="lesson-start"><summary>先前版本的模型教學與狀態</summary>')
        parts.append(f'<form method="post" action="/items/{item_id}/teach" data-study-operation data-busy="正在讀取原文與整理教學，請稍候…">{base}'
                     f'{hidden("request_id",form.get("request_id") if form.get("mode")=="lesson" else secrets.token_urlsafe(24))}'
                     f'<button class="button" type="submit"{off}>開始教我</button></form>')
        if service.provider is None:
            parts.append('<p class="notice" data-study-provider="unconfigured" data-answer-provider="unconfigured">這個舊版教學入口尚未設定模型。</p>')
        elif version['scope'] == 'abstract':
            parts.append('<p class="meta">先嘗試取得這個版本的全文，再依實際可用範圍教學。</p>')
        parts.append('</details>')
    for row in records:
        if row['mode'] == 'lesson' and row['state'] != 'completed':
            parts.append(f'<article id="study-{row["id"]}" data-study-state="{e(row["state"])}">'
                         + operation_status(row, csrf, diagnostics.get(row['id'], {})) + '</article>')
    parts.append('</div></details>')
    steps = context.get('journey',{}).get('steps', [])[:3]
    parts.append('<details id="backgrounds" class="backgrounds" data-position><summary>串連既有知識與先備教材</summary><p class="meta">依主題建議的背景；出現相同詞彙不代表採用相同方法。</p>')
    if context_error:
        parts.append(f'<p class="notice">暫時無法找回背景與相關理解：{e(context_error)}</p>')
    shown = set()
    for step in steps:
        background = step['guide']
        shown.add((str(background['item_id']), str(background['version_id'])))
        parts.append(f'<details class="background-item"><summary>{e(background["title"])}</summary><p>{e(step["reason"])}</p>'
                     f'<h3>先補這個想法</h3><p>{e(background["mechanism"])}</p><h3>用例子看</h3><p>{e(background["example"])}</p>'
                     + citations(background['evidence'], safe_href)
                     + f'<a class="text-action" href="{e(reading_url(background["item_id"],background["version_id"]))}">深入讀這篇 →</a></details>')
    backgrounds = [source for source in (public_context or {}).get('sources', [])
                   if source['item_id'] != item_id and source['excerpts']
                   and (str(source['item_id']), str(source['version_id'])) not in shown]
    for background in backgrounds[:3]:
        parts.append(f'<details class="background-item"><summary>{e(background["title"])}</summary>'
                     '<p class="meta">公開原文的閱讀建議，尚未生成教案；詞彙命中不代表作者引用關係。</p>'
                     f'<p>{e(background["reason"])}</p><p class="meta">固定版本 {e(background["label"])} · '
                     f'{e(SCOPE[background["scope"]])} · 僅列出已取得的相關片段</p>')
        for excerpt in background['excerpts']:
            href = reading_url(background['item_id'], background['version_id'], excerpt['anchor'])
            parts.append(f'<blockquote><p>{e(excerpt["quote"])}</p>'
                         f'<a href="{e(href)}">回到固定版本原文 #{e(excerpt["anchor"])}</a>'
                         f' · <a href="{safe_href(excerpt["source_url"])}" rel="noopener noreferrer">出版者來源</a></blockquote>')
            if excerpt.get('quality_note'):
                parts.append(f'<p class="meta">{e(excerpt["quality_note"])}</p>')
        parts.append('</details>')
    if not steps and not backgrounds and public_context is not None:
        parts.append('<p>目前沒有足夠原文能推薦先備教材。先從本篇的問題與方法讀起。</p>')
    for comparison in context.get('journey',{}).get('comparisons',[]):
        parts.append(f'<details><summary>{e(comparison["title"])}</summary><p>{e(comparison["text"])}</p><p class="meta">策展跨篇比較，仍需核對。</p></details>')
    related = [p for p in (public_context or {}).get('saved_learning', []) if p['item_id'] != item_id]
    if related:
        parts.append('<details class="prior-understanding" data-private><summary>你之前留下的理解</summary><p class="meta">只在你的知識庫顯示，不送給模型。</p>')
        for p in related[:3]:
            parts.append(f'<h3><a href="{e(reading_url(p["item_id"],p["version_id"],"progress-"+str(p["id"])))}">{e(p["title"])}</a></h3>'
                         f'<p class="prose">{e(p["understanding"])}</p><p class="prose">{e(p["questions"])}</p>')
        parts.append('</details>')
    if context.get('notes'):
        parts.append('<details class="prior-understanding" data-private><summary>相關原文的私人筆記</summary>')
        for note in context['notes']:
            parts.append(f'<p class="prose">{e(note["text"])}</p><a href="{e(note["reading_url"])}">回到筆記段落</a>')
        parts.append('</details>')
    parts.append('</details></section>')
    parts.append('<section id="panel-original" class="room-pane room-source" data-reader-panel="original" data-room-source '
                 'aria-label="固定版本原文" tabindex="0">'
                 '<header class="room-source-header"><h2>固定版本原文</h2>'
                 '<button class="room-tool room-focus" type="button" data-room-focus aria-pressed="false">專心讀原文</button>'
                 '<div class="room-source-links">')
    if visual_source:
        pdf = visual_source.replace('/html/', '/pdf/', 1)
        parts.append(f'<a href="/items/{item_id}/pdf?version={e(version_id)}" target="_blank" rel="noopener">同版 PDF ↗</a>'
                     '<a href="#figures">看圖表 ↓</a>'
                     f'<a href="{e(pdf)}" target="_blank" rel="noopener noreferrer">出版者 PDF ↗</a>')
    parts.append(f'<a href="{safe_href(version["source_url"])}" rel="noopener noreferrer">出版者原文 ↗</a></div></header>')
    if version['scope'] == 'abstract':
        if item.get('fulltext_status') == 'failed':
            parts.append('<p class="notice">出版者全文暫時無法取得，目前顯示摘要。</p>')
        parts.append(f'<form method="post" action="/items/{item_id}/fetch" data-busy="正在取得同版原文…">{base}'
                     '<button class="button secondary" type="submit">取得全文與圖表</button></form>')
    parts.append(source_outline(version['blocks']))
    for b in version['blocks']:
        parts.append(f'<section class="source-paragraph" id="{e(b["anchor"])}" tabindex="-1" data-position data-anchor="{e(b["anchor"])}">'
                     f'<p class="prose">{e(b["text"])}</p><a class="source-link" href="{safe_href(b.get("source_url") or version["source_url"])}" '
                     f'rel="noopener noreferrer">原文 #{e(b["anchor"])}</a></section>')
    if visual_source:
        parts.append(f'<section id="figures" class="visual-evidence" tabindex="-1" data-visual-evidence="{e(visual_source)}">'
                     '<h2>原文圖表</h2><p class="meta" data-visual-status role="status">正在從這個版本找圖表…</p>'
                     '<div data-visual-cards></div>')
        if has_hive_guide:
            parts.append(f'<p class="meta">原圖與表格取自 Haozhen Zhang 等人的 '
                         f'<a href="{e(visual_source)}" rel="noopener noreferrer">HiVe-Mem v1 原文 ↗</a>，'
                         '<a href="https://creativecommons.org/licenses/by/4.0/" rel="license noopener noreferrer">CC BY 4.0</a>。繁中導讀由 Knowledge Hub 整理。</p>')
        parts.append('</section><div data-visual-excerpts' + (' data-hive-excerpts' if has_hive_guide else '') + '></div>')
    if not version['blocks']:
        parts.append('<p class="notice">尚無可在這裡定位的正文段落。可由上方連結閱讀出版者原文。</p>')
    parts.append('</section></div><details class="room-fold room-notes" data-room-notes data-private>'
                 '<summary>我的理解與私人筆記</summary><section id="panel-notes" data-reader-panel="notes">')
    reflection = form.get('understanding', state['reflection'] if state['revision'] else previous.get('understanding',''))
    questions = form.get('questions', state['open_questions'] if state['revision'] else previous.get('questions',''))
    summary = reading.suggest_summary(item_id, version_id)
    if not summary['understanding'] and guide:
        summary = {'record_id':None, 'understanding':guide['mechanism']+'\n\n'+guide['connections'], 'questions':guide['limitations']}
    parts.append('<h2>把想法留給下次的自己</h2><p>草稿自動保存在你的知識庫；「留下這份理解」會保留一份確認紀錄。讀過或保存都不等於已掌握。</p>')
    if summary['understanding']:
        parts.append('<details class="summary-preview"><summary>這次教學的整理稿，可檢查後採用</summary>'
                     f'<p class="prose" id="summary-suggestion">{e(summary["understanding"])}</p>'
                     '<button type="button" class="button secondary" data-use-summary>將摘要加入我的理解</button>'
                     '<p class="meta">從已保存教材摘錄；不是對你的理解程度作判定。</p></details>')
    parts.append(f'<form id="study-save" method="post" action="/items/{item_id}/progress" data-busy="正在留下這份理解…">{base}'
                 f'{hidden("request_id",form.get("request_id") if "understanding" in form else secrets.token_urlsafe(24))}'
                 f'{hidden("record_id",form.get("record_id", latest["id"] if latest else ""))}'
                 f'<label for="reflection">我的理解</label><textarea id="reflection" name="understanding" data-draft="reflection" maxlength="8000" rows="8">{e(reflection)}</textarea>'
                 f'<label for="open-questions">還想弄懂的事</label><textarea id="open-questions" name="questions" data-draft="open_questions" maxlength="8000" rows="4">{e(questions)}</textarea>'
                 '<button class="button" type="submit">留下這份理解</button></form>')
    for p in progress:
        parts.append(f'<details id="progress-{p["id"]}" class="saved-understanding" data-position><summary>已留下的理解 · {e(p["created_at"][:16].replace("T"," "))}</summary>'
                     f'<p class="prose">{e(p["understanding"])}</p><p class="prose">{e(p["questions"])}</p>')
        if p['export_error']:
            parts.append(f'<p class="notice">未建立額外來源包；完整教學與理解仍已保存。{e(p["export_error"])}</p>')
        if p['lesson_id']:
            parts.append(f'<a href="/lessons/{p["lesson_id"]}">教材與來源包</a>')
        parts.append('</details>')
    for n in item['notes']:
        if str(n['version_id']) != str(version_id):
            continue
        parts.append(f'<details id="note-{e(n["id"])}" tabindex="-1"><summary>先前的段落筆記 · {e(n["anchor"])}</summary>'
                     f'<p class="prose">{e(n["text"])}</p></details>')
    parts.append('</section></details><aside class="conversation room-drawer" id="conversation" data-room-drawer hidden '
                 'data-position aria-label="追問與沉澱">'
                 '<header class="room-drawer-header"><button class="room-tool" type="button" '
                 'data-room-close aria-label="收起追問">收起追問</button></header>')
    routes = context.get('model_routes', {})
    current_state = {**state, 'question_draft': form.get('prompt', state['question_draft'])}
    parts.append(layer_views.conversation(item_id, version_id, csrf, current_state,
                 context.get('followups', []), context.get('captures', []),
                 primary_model=bool(routes.get('primary')), fallback_model=bool(routes.get('fallback'))))
    parts.append('<details class="archived-turns"><summary>先前版本的問答與操作收據</summary><div class="conversation-log">')
    # Retain answers from the earlier reading workflow on their original version.
    for row in item['questions']:
        if str(row['version_id']) != str(version_id):
            continue
        parts.append(f'<details id="question-{row["id"]}" data-question-status="{e(row["status"])}"><summary>先前的提問：{e(row["question"])}</summary>')
        if row['status'] == 'completed' and row.get('answer'):
            for claim in row['answer']['claims']:
                cites = [{**c,'reading_url':reading_url(item_id,version_id,c['anchor'])} for c in claim['citations']]
                parts.append(f'<p class="prose">{e(claim["text"])}</p>' + citations(cites,safe_href))
            parts.append(f'<p class="meta">{e(row["answer"]["limitations"])}</p>')
        else:
            parts.append('<p>原請求未有可用結果；保留紀錄，不自動重送。</p>')
        parts.append('</details>')
    for row in item['connections']:
        if str(row['version_id']) != str(version_id):
            continue
        parts.append(f'<details id="connection-{row["id"]}"><summary>先前的兩來源比較</summary>')
        if row['state'] == 'completed' and row.get('answer'):
            for claim in row['answer']['claims']:
                parts.append(f'<p class="prose">{e(claim["text"])}</p>' + citations(claim['citations'],safe_href))
        else:
            parts.append('<p>原請求未有可用結果；保留紀錄，不自動重送。</p>')
        parts.append('</details>')
    for row in records:
        if row['mode'] != 'qa':
            continue
        parts.append(f'<article id="study-{row["id"]}" class="chat-turn" data-study-state="{e(row["state"])}"><p class="chat-question">{e(row["prompt"])}</p>')
        if row['state'] == 'completed':
            parts.append(context_limitations(row))
            for section in row['answer']['sections']:
                parts.append(f'<h3>{e(section["title"])}</h3><p class="prose">{e(section["text"])}</p>'+citations(section['citations'],safe_href))
            parts.append(f'<p class="meta">{e(row["answer"]["limitations"])}</p>')
            parts.append(answer_note_button(row, include_quotes=False))
        else:
            parts.append(operation_status(row, csrf, diagnostics.get(row['id'], {})))
        parts.append('</article>')
    for row in source_turns:
        parts.append(saved_source_turn(row,safe_href))
    parts.append('</div>')
    if has_hive_guide:
        parts.append('<button class="button secondary" type="button" data-guide-search>從本篇找解答</button>'
                     '<div data-guide-result aria-live="polite"></div>')
        for anchor, label in (('S4.T1', '怎麼算出省九成？'), ('S4.F2', '選得少，為什麼仍貴？')):
            parts.append(f'<button type="button" data-question="{e(hive_guide.GUIDE[anchor]["question"])}">{label}</button>')
    original_location = availability.get('original_location')
    if blocked and not original_location:
        parts.append('<p class="notice warning">已有處理中或結果未知的請求，已暫停新生成。</p>')
    if service.provider is not None and availability['reason']:
        link = f' <a href="{e(original_location)}">回到原工作</a>' if original_location else ''
        parts.append(f'<p class="notice warning">{e(availability["reason"])}{link}</p>')
    parts.append('</details></aside>'
                 f'<details class="reading-details"><summary>版本、來源與進階管理</summary><p>本篇版本 {e(version["label"])}；抓取時間 {e(version["fetched_at"])}。</p>'
                 f'<a href="/items/{item_id}?version={e(version_id)}&amp;view=manage">查看完整來源、操作收據及管理表單</a></details></article>')
    if context['focal'].get('source_id'):
        parts.append(f'<p class="meta"><a href="/library/{e(context["focal"]["source_id"])}">這篇在知識庫中的來源卡</a></p>')
    return shell(item['title'], csrf, ''.join(parts), 'reading', styles=('room', 'reading-actions'))


def operation_status(row, csrf, diagnostic=None):
    state = row['state']
    labels = {'pending':'教學正在準備中，可查詢原工作進度。', 'unknown':'這次請求的結果尚未確定。已停止新生成，請保留原工作核對。',
              'failed':'這次沒有取得通過檢查的內容。原請求已保留，不會自動再送。'}
    label = labels.get(state, '請求狀態尚未確認，請核對原工作。')
    result = f'<div class="notice" id="operation-{row["id"]}" data-operation-state="{e(state)}"><p>{e(label)}</p>'
    if state in ('failed', 'unknown'):
        result += f'<p data-study-diagnostic>{e(study_views.diagnostic_label(diagnostic or {}, state))}</p>'
    if state in ('pending','unknown'):
        # An explicit click can inspect the original even if this tab still shows pending.
        inspect = hidden('inspect_unknown','1')
        result += f'<form method="post" action="/study/{row["id"]}/reconcile" data-study-operation data-busy="正在核對原工作…">{hidden("csrf",csrf)}{inspect}<button class="button secondary" type="submit">查詢原工作</button></form>'
    return result + '</div>'


def history(store, reading, csrf, page=1):
    continuation = reading.continuations(page=page)
    parts = ['<div class="page-heading"><div><h1>接續與理解</h1><p>回到同一個版本，接著上次的問題往下走。</p></div></div>',
             f'<p class="meta">共 {continuation["total"]} 份固定版本的閱讀紀錄 · 每頁最多 20 份</p><div class="history-list">']
    rows = continuation['rows']
    for row in rows:
        link, preview, original = continuation_links(row)
        parts.append(f'<article><p class="meta">{e(date_label(row["updated_at"]))} · {e(edition_label(row))}</p><h2><a href="{e(link)}">{e(row["title"])}</a></h2>'
                     f'<p class="prose">{e(preview[:400])}</p>'
                     f'<div class="resume-actions"><a class="text-action" href="{e(link)}">接著讀 →</a>{original}</div></article>')
    if not rows:
        parts.append('<p>從一篇文章開始。閱讀位置、追問和理解會在這裡累積。</p><a class="button" href="/">找一篇值得讀的內容</a>')
    parts.append('</div>')
    if continuation['pages'] > 1:
        page = continuation['page']
        parts.append('<nav class="history-pagination" aria-label="接續列表分頁">')
        if page > 1:
            parts.append(f'<a class="button secondary" href="/reading?page={page-1}" rel="prev">上一頁</a>')
        parts.append(f'<span class="meta">第 {page} / {continuation["pages"]} 頁</span>')
        if page < continuation['pages']:
            parts.append(f'<a class="button secondary" href="/reading?page={page+1}" rel="next">下一頁</a>')
        parts.append('</nav>')
    return shell('接續與理解',csrf,''.join(parts),'reading')
