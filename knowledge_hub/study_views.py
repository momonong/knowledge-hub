"""Same-page study controls; all model and user text is escaped."""
import html
import secrets

from .study import StudyError
from .connections import REASON_LABELS, reading_url


def e(value):
    return html.escape(str(value), quote=True)


KINDS = {'author_claim':'原文主張', 'evidence':'研究證據', 'inference':'推論，待核對', 'example':'合成例子'}
STATES = {'completed':'解說已保存', 'pending':'正在處理', 'unknown':'結果待核對', 'failed':'生成或引用檢查失敗'}
SCOPES = {'full':'全文文字解析', 'partial':'部分解析', 'abstract':'僅摘要'}
DIAGNOSTIC_LABELS = {
    'provider_deadline': REASON_LABELS['provider_deadline'],
    'answer_shape': '模型未回傳有效的答案結構；未保存為解說。請先閱讀原文，並依原收據核對生成結果。',
    'comparison_sources': '比較段未同時引用本篇與背景原文；跨篇證據不足，未保存為解說。請核對本篇與背景的原文片段。',
    'provider_http_failed': '提供者 HTTP 請求失敗；未取得可保存的解說。請依原收據核對提供者狀態與用量，不會自動重送。',
    'transport_unknown': '傳輸結果未知，不能判定提供者是否已完成。請只核對原操作收據；不會重新提交。',
    'request_unknown': '原操作結果仍待核對。請只核對原操作收據；不會重新提交。',
    'protocol_unknown': '提供者回覆狀態無法確認。請只核對原操作收據；不會重新提交。',
}


def diagnostic_label(diagnostic, state):
    """Only fixed labels cross the reader boundary, never metadata strings."""
    code = diagnostic.get('code')
    if type(code) is str and code in DIAGNOSTIC_LABELS:
        return DIAGNOSTIC_LABELS[code]
    if state == 'unknown':
        return DIAGNOSTIC_LABELS['request_unknown']
    return '生成或引用檢查失敗，未保存為解說。請閱讀原文並核對原操作收據；不會自動重送。'


def hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{e(value)}">'


def evidence(sources, safe_href):
    parts = []
    for source in sources:
        parts.append(f'<details><summary>{e(source["id"])} · {e(source["title"])} — {e(SCOPES.get(source["scope"],source["scope"]))}</summary>')
        parts.append(f'<p>{e(source["reason"])}。固定版本 {e(source["label"])}；選取 {len(source["excerpts"])} 個片段。</p>')
        for span in source['excerpts']:
            parts.append(f'<blockquote style="white-space:pre-wrap">{e(span["quote"])}<br>'
                         f'<a href="{e(span["reading_url"])}">本機原文 #{e(span["anchor"])}</a> · '
                         f'<a href="{safe_href(span["source_url"])}" rel="noopener noreferrer">出版者原文</a></blockquote>')
            if span.get('quality_note'):
                parts.append(f'<p>{e(span["quality_note"])}</p>')
        parts.append('</details>')
    return ''.join(parts)


def resume(store):
    with store._connect() as c:
        rows = c.execute('''SELECT p.*,t.title FROM study_progress p JOIN version_titles t ON t.version_id=p.version_id
            WHERE p.id IN (SELECT MAX(id) FROM study_progress GROUP BY item_id,version_id)
            ORDER BY p.id DESC LIMIT 5''').fetchall()
    if not rows:
        return ''
    parts = ['<section class="reading-shell"><h2>接續上次學習</h2><ul>']
    for row in rows:
        url = reading_url(row['item_id'],row['version_id'],f"progress-{row['id']}")
        parts.append(f'<li><a href="{e(url)}">{e(row["title"])}</a><p>{e((row["questions"] or row["understanding"])[:240])}</p></li>')
    return ''.join(parts) + '</ul></section>'


def render(service, item_id, version_id, csrf, safe_href, *, error='', form=None):
    form = form or {}
    saved = [r for r in service.records(item_id) if r['version_id'] == str(version_id)]
    diagnostics = service.diagnostics(item_id, version_id)
    completed = [r for r in saved if r['state'] == 'completed']
    latest = completed[-1] if completed else None
    progress = service.progress(item_id,version_id)
    previous = progress[0] if progress else {}
    blocked = service.unresolved(item_id,version_id)
    disabled = service.provider is None or blocked
    off = ' disabled aria-disabled="true"' if disabled else ''
    base = hidden('csrf',csrf) + hidden('version_id',version_id)
    parts = ['<section id="study" class="panel" aria-labelledby="heading-study" style="margin-bottom:1.5rem;overflow-wrap:anywhere">',
             '<h2 id="heading-study">讀懂這篇，接上舊知</h2>',
             '<p>從原文取證、理解機制與差異，再留下自己的理解。模型解說與個人筆記分開保存。</p>']
    if error:
        parts.append(f'<p role="alert" class="notice notice-warn">{e(error)} 輸入保留如下。</p>')
    if service.provider is None:
        parts.append('<p class="notice" data-study-provider="unconfigured">通用教學尚未啟用；可閱讀原文與找回筆記。需要配置已授權的模型與本次資源上限。</p>')
    if blocked:
        parts.append('<p class="notice notice-warn">這個版本有處理中或結果未知的操作，只能核對原收據；新生成暫停。</p>')
    try:
        context = service.context(item_id,version_id)
        parts += ['<details><summary>本篇與建議背景：查看選取理由及生成證據</summary>',
                  f'<p>{e(context["limitations"])}。問題與學習目標會影響選段，送出後的精確範圍保存在每份操作收據。</p>',
                  evidence(context['sources'],safe_href), '</details>']
        related_saved = [p for p in context['saved_learning'] if p['item_id'] != item_id]
        if related_saved:
            parts.append('<details><summary>找回相關閱讀留下的理解（留在本機）</summary>')
            for p in related_saved:
                parts.append(f'<p><a href="{e(reading_url(p["item_id"],p["version_id"],"progress-"+str(p["id"])))}">{e(p["title"])}</a></p>'
                             f'<p style="white-space:pre-wrap">我的理解：{e(p["understanding"])}\n未解問題：{e(p["questions"])}</p>')
            parts.append('</details>')
    except StudyError as exc:
        parts.append(f'<p>{e(exc)}</p>')
    goal = form.get('prompt') if form.get('mode') == 'lesson' else '理解本篇解決的問題、機制、具體例子，以及與相關舊方法的差異與限制。'
    parts.append(f'''<details{' open' if not saved else ''}><summary>產生本篇教學與跨篇比較</summary>
      <form method="post" action="/items/{item_id}/study">{base}{hidden('mode','lesson')}
      {hidden('request_id', form.get('request_id') if form.get('mode') == 'lesson' else secrets.token_urlsafe(24))}
      <label for="study-prompt">這次想學到什麼</label>
      <textarea id="study-prompt" name="prompt" rows="3" maxlength="2000" required>{e(goal)}</textarea>
      <p>送出此目標與本篇、最多三份背景的公開原文片段；不傳既有私人筆記。</p>
      <button type="submit" class="btn"{off}>產生有來源的教學</button></form></details>''')
    for row in saved:
        parts.append(f'<article id="study-{row["id"]}" class="pick-item" data-study-state="{row["state"]}">'
                     f'<h3>{"本篇教學" if row["mode"] == "lesson" else "同頁追問"} · {e(STATES[row["state"]])}</h3>'
                     f'<p style="white-space:pre-wrap">{e(row["prompt"])}</p>')
        if row['answer'] is not None:
            for s in row['answer']['sections']:
                parts.append(f'<h4>{e(s["title"])} <small>· {e(KINDS[s["kind"]])}</small></h4>'
                             f'<p style="white-space:pre-wrap">{e(s["text"])}</p><details><summary>核對本段證據</summary>')
                for c in s['citations']:
                    parts.append(f'<blockquote style="white-space:pre-wrap">{e(c["quote"])}<br>'
                                 f'<a href="{e(c["reading_url"])}">{e(c["source_id"])} · 固定版本原文 #{e(c["anchor"])}</a> · '
                                 f'<a href="{safe_href(c["source_url"])}" rel="noopener noreferrer">出版者定位</a></blockquote>')
                parts.append('</details>')
            parts.append(f'<p><strong>範圍與限制：</strong>{e(row["answer"]["limitations"])}</p>'
                         '<p>引用已核對逐字出處；是否支持整段解讀，仍需閱讀者核對。</p>')
        if row['state'] in ('failed','unknown'):
            label = diagnostic_label(diagnostics.get(row['id'], {}), row['state'])
            parts.append(f'<p class="notice notice-warn" data-study-diagnostic="true">{e(label)}</p>')
        elif row['reason_code']:
            parts.append(f'<p>{e(REASON_LABELS.get(row["reason_code"],"原操作待核對"))}</p>')
        if row['state'] in ('unknown','pending'):
            parts.append(f'<form method="post" action="/study/{row["id"]}/reconcile">{hidden("csrf",csrf)}'
                         f'{hidden("inspect_unknown","1") if row["state"] == "unknown" else ""}'
                         '<button type="submit" class="btn btn-secondary">核對原操作</button><p>只查原工作，不重新提交。</p></form>')
        parts.append(f'<details><summary>操作收據與實際送出的原文</summary><p>時間：{e(row["created_at"])} · '
                     f'操作：{e(row["operation_id"])} · 用量：{e(row["usage_state"] or "尚未確認")}</p>'
                     + evidence(row['payload']['sources'],safe_href) + '</details></article>')
    question = form.get('prompt','') if form.get('mode') == 'qa' else ''
    parent = form.get('parent_id',str(latest['id']) if latest else '')
    parts.append(f'''<form method="post" action="/items/{item_id}/study" style="margin-top:1rem">{base}
      {hidden('mode','qa')}{hidden('parent_id',parent) if parent else ''}
      {hidden('request_id',form.get('request_id') if form.get('mode') == 'qa' else secrets.token_urlsafe(24))}
      <label for="study-question">在這裡繼續問</label>
      <textarea id="study-question" name="prompt" rows="3" maxlength="2000" required>{e(question)}</textarea>
      <p>{('接續最近一份解說 #' + str(latest['id']) + '；') if latest else ''}會送出你的新問題、上份問題／有效解說與重新選取的公開原文。既有私人筆記不自動外送。</p>
      <button type="submit" class="btn"{off}>依原文回答</button></form>''')
    understanding = form.get('understanding', previous.get('understanding',''))
    questions = form.get('questions', previous.get('questions',''))
    selection = form.get('record_id', str(latest['id']) if latest else '')
    options = f'<option value=""{" selected" if selection == "" else ""}>只保存我的筆記</option>' + ''.join(
        f'<option value="{r["id"]}"{" selected" if str(r["id"]) == selection else ""}>#{r["id"]} {"教學" if r["mode"] == "lesson" else "追問"}：{e(r["prompt"][:70])}</option>' for r in completed)
    parts.append(f'''<form id="study-save" method="post" action="/items/{item_id}/progress" style="margin-top:1.5rem">{base}
      {hidden('request_id',form.get('request_id') if 'understanding' in form else secrets.token_urlsafe(24))}
      <h3>保存理解，下次從這裡接續</h3><p>這兩欄留在本機；每次保存建立新紀錄，保留舊稿。</p>
      <label for="study-record">一起保存的解說</label><select id="study-record" name="record_id">{options}</select>
      <label for="study-understanding">我的理解</label>
      <textarea id="study-understanding" name="understanding" rows="4" maxlength="8000">{e(understanding)}</textarea>
      <label for="study-open-questions">未解問題與下次要做的事</label>
      <textarea id="study-open-questions" name="questions" rows="3" maxlength="8000">{e(questions)}</textarea>
      <button type="submit" class="btn">保存理解與未解問題</button></form>''')
    for i,p in enumerate(progress):
        parts.append(f'<details id="progress-{p["id"]}"{" open" if i == 0 else ""}><summary>已保存的學習紀錄 · {e(p["created_at"])}</summary>'
                     f'<p style="white-space:pre-wrap"><strong>我的理解</strong>\n{e(p["understanding"])}</p>'
                     f'<p style="white-space:pre-wrap"><strong>未解問題</strong>\n{e(p["questions"])}</p>')
        if p['record_id']:
            parts.append(f'<p><a href="#study-{p["record_id"]}">接續這份完整解說與引用</a></p>')
        if p['export_error']:
            parts.append(f'<p class="notice notice-warn">理解、未解問題與完整解說已保留；未建立額外來源包：{e(p["export_error"])}</p>')
        if p['lesson_id']:
            parts.append(f'<p><a href="/lessons/{p["lesson_id"]}">已保存的教材稿與來源包</a></p>')
        parts.append('</details>')
    return ''.join(parts) + '</section>'
