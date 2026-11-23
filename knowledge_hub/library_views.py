"""Escaped fragments for the library; reuse the application's existing layout."""
from html import escape
from urllib.parse import urlencode, quote


def e(value):
    return escape(str(value if value is not None else ''), quote=True)


# Presentation only: original machine values remain in storage and exports.
SCOPE_LABELS = {
    'partial': '已擷取部分文字，圖表與公式待核對',
    'abstract': '僅摘要',
    'full': '已擷取全文文字，圖表與公式待核對',
}
CARD_LABELS = {
    'read_scope': {'overview': '內容概覽', 'abstract': '僅摘要',
                   'partial': '部分內容', 'full': '全文',
                   'unavailable': '無可用原文'},
    'license_status': {'not_assessed': '尚未評估'},
    'corpus_status': {'curated_source_card_only': '僅有策展來源卡'},
    'assertion_status': {'curator_synthesis': '策展整理', 'curator_proposal': '策展建議'},
    'mastery': {'unknown': '未知'},
}


def label(value, labels):
    # Unknown values are retained as escaped text rather than guessed.
    return e(labels.get(value, value) if isinstance(value, str) else value)


def card_label(field, value):
    return label(value, CARD_LABELS[field])


def source_href(sid):
    return '/library/' + quote(sid, safe='')


def hits(results):
    parts = ['<h2>搜尋來源與已保存原文</h2><details><summary>搜尋方式</summary><p>SQLite FTS5 詞彙排名，另保留繁中子字串比對；非向量搜尋。</p></details>']
    if not results:
        return ''.join(parts) + '<p>沒有符合的來源卡或已擷取原文。</p>'
    parts.append('<ul class="pick-list">')
    for result in results:
        local = source_href(result['source_id'])
        label = '策展來源卡／概念'
        if result['kind'] == 'document':
            local = f'/items/{result["item_id"]}?' + urlencode({'version': result['version_id']}) + '#' + result['anchor']
            label = '已擷取原文 #' + result['anchor']
        parts.append(f'<li class="pick-item"><a href="{e(local)}">{e(result["title"])}</a>'
                     f'<p>{e(label)} · <a href="{e(source_href(result["source_id"]))}">來源卡與相關背景</a></p>'
                     f'<p>{e(result["snippet"])}</p></li>')
    parts.append('</ul>')
    return ''.join(parts)


def listing(library, query='', domain='', starter_only=False):
    records = library.list_sources(query, domain, starter_only)
    domains = library.domains()
    labels = {d['id']: d['title'] for d in domains}
    parts = [f'<section class="reading-shell"><h1>初始知識庫</h1><p>共 {library.stats()["sources"]} 筆來源；目前顯示 {len(records)} 筆。歷史來源與策展摘要供背景閱讀，不代表已讀或掌握。</p>',
             '<form method="get" action="/library" class="panel"><div class="form-row"><label for="library-query">搜尋來源與已保存原文</label>',
             f'<input id="library-query" type="search" name="q" maxlength="2000" value="{e(query)}"></div>',
             '<div class="form-row"><label for="library-domain">領域</label><select id="library-domain" name="domain"><option value="">所有領域</option>']
    for d in domains:
        selected = ' selected' if domain == d['id'] else ''
        parts.append(f'<option value="{e(d["id"])}"{selected}>{e(d["title"])}</option>')
    checked = ' checked' if starter_only else ''
    parts.append(f'</select></div><label><input type="checkbox" name="starter" value="1"{checked}> 只看優先閱讀清單</label> <button class="btn" type="submit">篩選</button></form>')
    if not records:
        parts.append('<p>目前沒有符合條件的來源。尚未匯入時，請由本機 CLI 匯入書目。</p>')
    parts.append('<ul class="pick-list">')
    for s in records:
        scopes = '、'.join(label(scope, SCOPE_LABELS) for scope in sorted({d['scope'] for d in s['documents']})) if s['documents'] else '僅書目／策展摘要；無原文'
        starter = f'建議順序 {s["starter"]["order"]} · ' if s['starter'] else ''
        parts.append(f'<li class="pick-item"><h2 class="pick-title"><a href="{e(source_href(s["id"]))}">{e(s["title"])}</a></h2>'
                     f'<p>{e(starter)}{e(s.get("year", "年份未提供"))} · {e("、".join(labels[x] for x in s["domains"]))}</p>'
                     f'<p>實際內容：{scopes}</p><p>{e(s.get("summary_zh", ""))}</p></li>')
    parts.append('</ul>')
    if query.strip():
        parts.append(hits([h for h in library.search(query) if h['source_id'] in {s['id'] for s in records}]))
    parts.append('</section>')
    return ''.join(parts)


def detail(source):
    parts = [f'<article class="reading-shell"><p><a href="/library">初始知識庫</a></p><h1>{e(source["title"])}</h1>',
             f'<p>{e(source.get("year", ""))} · {e(source.get("authors_or_org", ""))}</p>',
             f'<p><a href="{e(source["primary_url"])}" rel="noopener noreferrer">原始來源網址</a></p>',
             '<h2>策展來源卡</h2><p>以下是策展介紹，並非論文原文，也不代表使用者已讀／掌握。</p>',
             f'<p>{e(source.get("summary_zh", ""))}</p><p>{e(source.get("why_fit_zh", ""))}</p>',
             f'<p>書目記錄的研究閱讀範圍：{card_label("read_scope", source.get("read_scope", "未提供"))}；不等於本機原文可用範圍。</p>',
             f'<p>權利狀態：{card_label("license_status", source.get("license_status", "未評估"))} · 研究時的語料狀態：{card_label("corpus_status", source.get("corpus_status", "未提供"))}</p>']
    if 'learner_mastery' in source:
        parts.append(f'<p>掌握度記錄：{card_label("mastery", source["learner_mastery"])}</p>')
    if 'assertion_status' in source:
        parts.append(f'<p>策展狀態：{card_label("assertion_status", source["assertion_status"])}</p>')
    if source['starter']:
        s = source['starter']
        parts.append(f'<h2>建議閱讀順序 {s["order"]}</h2><p>{e(s.get("learning_goal_zh", ""))}</p><p>複習問題：{e(s.get("review_question_zh", ""))}</p>')
    parts.append('<h2>實際原檔與原文閱讀</h2>')
    if not source['documents']:
        parts.append('<p>目前只有書目／策展摘要，尚未保存原檔或擷取原文。</p>')
    for number, d in enumerate(source['documents'], 1):
        read = f'/items/{d["item_id"]}?' + urlencode({'version': d['version_id']})
        parts.append(f'<section class="panel"><h3>已保存原文 {number}</h3>'
                     f'<p>實際文字範圍：{label(d["scope"], SCOPE_LABELS)}。</p>'
                     '<p>文字擷取不保證圖表、公式或排版完整，也不代表已逐頁核對。</p>'
                     f'<p><a href="{e(read)}">閱讀固定原文版本／筆記／學習包</a> · <a href="/library/files/{e(d["blob_sha256"])}">下載原始檔案</a> · '
                     f'<a href="{e(d["source_url"])}" rel="noopener noreferrer">取得原文的位置</a></p>'
                     '<details><summary>來源與擷取稽核資訊</summary>'
                     f'<p>原文版本標記：{e(d["version_label"])}</p>'
                     f'<p>解析工具／版本與詳細限制：{e(d["extraction_note"] or "未提供")}</p>'
                     f'<p>權利說明與依據：{e(d["rights_note"] or "未提供；不代表可再散布")}</p>'
                     f'<p>保存時間：{e(d["acquired_at"])}</p>'
                     f'<p style="overflow-wrap:anywhere">SHA256：{e(d["blob_sha256"])}</p></details></section>')
    parts.append('<h2>相關概念</h2>')
    if not source['concepts']:
        parts.append('<p>尚無概念關聯。</p>')
    for concept in source['concepts']:
        parts.append(f'<section class="panel"><h3>{e(concept["title"])}</h3><p>{e(concept.get("summary_zh", ""))}</p><p>策展狀態：{card_label("assertion_status", concept.get("assertion_status", ""))}；掌握度記錄：{card_label("mastery", concept.get("user_mastery", "unknown"))}</p></section>')
    parts.append('<h2>相關背景來源</h2><p>策展建議的背景連結；尚未核對為論文引用或技術繼承關係。</p><ul>')
    for rel in source['related']:
        parts.append(f'<li><a href="{e(source_href(rel["source_id"]))}">{e(rel["title"])}</a><p>{e(rel["reason"])}</p></li>')
    if not source['related']:
        parts.append('<li>尚無建議背景來源。</li>')
    parts.append('</ul></article>')
    return ''.join(parts)
