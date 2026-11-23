"""Escaped reading fragments with actions and saved comparisons first."""
import html
import secrets

from .connections import REASON_LABELS, reading_url
from .excerpts import preview


def e(value):
    return html.escape(str(value), quote=True)


RELATION_LABELS = {
    'background': '建議先讀的背景', 'shared_concept': '共通概念',
    'next': '可接續閱讀', 'lexical': '詞彙相關，待核對語意',
}
STATE_LABELS = {'completed': '已完成', 'pending': '處理中', 'unknown': '結果待核對', 'failed': '未成功'}
SCOPE_LABELS = {'full': '完整解析', 'partial': '部分解析', 'abstract': '摘要',
                'unavailable': '無可用正文', 'metadata-only': '僅來源卡'}
CLAIM_LABELS = {'background': '背景', 'comparison': '初步比較', 'inference': '推論，待核對'}


def render(service, item_id, version_id, csrf, focus, safe_href, context=None):
    if context is None:
        context = service.build_context(item_id, version_id, focus)
    saved = [row for row in service.records(item_id) if str(row['version_id']) == str(version_id)]
    unresolved = any(row['state'] in ('unknown', 'pending') for row in saved)
    disabled = service.provider is None or unresolved
    parts = [f'''<section class="panel" aria-labelledby="heading-connections" style="margin-bottom:1rem;overflow-wrap:anywhere;">
        <h2 id="heading-connections">串連既有知識</h2>
        {render_journey(context.get('journey'), safe_href)}
        {render_teaching(context['focal'], safe_href)}
        {render_comparisons(context.get('journey'), safe_href)}
        <div class="connection-tools"><h3>深入檢索與雙來源比較</h3>
        <p>{e(context['limitations'])}</p>
        <form method="get" action="/items/{item_id}">
        <input type="hidden" name="version" value="{e(version_id)}">
        <label for="connection-focus">比較焦點（最多 500 字）</label>
        <input id="connection-focus" name="focus" maxlength="500" value="{e(focus)}">
        <button class="btn btn-secondary" type="submit">找回相關原文</button></form>
        <form method="post" action="/items/{item_id}/connections">
        <input type="hidden" name="csrf" value="{e(csrf)}">
        <input type="hidden" name="version_id" value="{e(version_id)}">
        <input type="hidden" name="request_id" value="{secrets.token_urlsafe(24)}">
        <input type="hidden" name="focus" value="{e(focus)}">
        <p>僅傳目前文章與第一份有原文的相關來源片段，產生簡短初步比較；不傳私人筆記。</p>
        <button class="btn" type="submit"{' disabled aria-disabled="true"' if disabled else ''}>產生雙來源初步比較</button></form>''']
    if service.provider is None:
        parts.append('<p>尚未配置解說提供者，無法生成；本機關聯檢索仍可使用。</p>')
    if unresolved:
        parts.append('<p>此版本已有處理中或結果待核對的操作，暫停新生成；請在下方核對原操作。</p>')

    def show_excerpt(block, short=False):
        text, truncated = preview(block['quote']) if short else (block['quote'], False)
        note = block.get('quality_note', '')
        if truncated and block.get('quality') != 'fragment':
            note = '預覽為節錄；展開可看全部選段。' + note
        return f'''<blockquote>{e(text)}<br>
            <a href="{e(block['reading_url'])}">固定版本原文 #{e(block['anchor'])}</a> ·
            <a href="{safe_href(block['source_url'])}" rel="noopener noreferrer">來源定位</a>
            {('<p>' + e(note) + '</p>') if note else ''}</blockquote>'''

    # Saved answers precede recommendation excerpts so daily reading does not
    # require scrolling through several thousand characters to reach an answer.
    for row in reversed(saved):
        parts.append(f'''<div id="connection-{row['id']}" class="pick-item">
            <h3>已保存的雙來源初步比較</h3>
            <p>狀態：{e(STATE_LABELS[row['state']])} · 固定版本 #{e(row['version_id'])}</p>
            <p>焦點：{e(row['focus'])}</p>''')
        reason = REASON_LABELS.get(row.get('reason_code'))
        if reason:
            parts.append(f'<p>{e(reason)}</p>')
        if row.get('http_status') is not None:
            parts.append(f'<p>Broker HTTP 狀態：{e(row["http_status"])}</p>')
        if row['state'] == 'completed' and row['answer']:
            if row.get('usage_state') == 'unknown':
                parts.append('<p>內容已完成；提供者用量仍待核對。未確認實際費用，不會重送。</p>')
            elif row.get('usage_state') == 'known':
                parts.append('<p>內容已完成；提供者未標記用量不確定。實際費用仍未核對。</p>')
            else:
                parts.append('<p>內容已完成；沒有用量核對證據，實際費用未核對。</p>')
            for claim in row['answer']['claims']:
                parts.append(f'<p><strong>{e(CLAIM_LABELS[claim["kind"]])}</strong>：{e(claim["text"])}</p>')
                for cite in claim['citations']:
                    parts.append(f'''<blockquote>{e(cite['quote'])}<br>
                        <a href="{e(cite['reading_url'])}">{e(cite['source_id'])} 固定版本原文</a> ·
                        <a href="{safe_href(cite['source_url'])}" rel="noopener noreferrer">來源定位</a></blockquote>''')
            parts.append(f'<p>限制：{e(row["answer"]["limitations"])}</p>')
            parts.append('<p>這是有限片段的初步比較；引用檢查只驗證出處，解讀仍需讀者核對。下方保留完整本機證據供深入閱讀。</p>')
        elif row['state'] in ('pending', 'unknown'):
            parts.append(f'''<p>只查詢原操作，不會重新提交。若連線設定改變，請先恢復原端點、模型與輸出上限。</p>
                <form method="post" action="/connections/{row['id']}/refresh">
                <input type="hidden" name="csrf" value="{e(csrf)}">
                <button class="btn btn-secondary" type="submit">核對原操作</button></form>''')
        elif not reason:
            parts.append('<p>此舊紀錄沒有細分失敗原因；沒有有效解說，不會自動重送。</p>')
        parts.append(f'''<details><summary>操作收據與實際送出證據</summary>
            <p>request_id：{e(row['request_id'])}<br>operation_id：{e(row['operation_id'])}</p>
            <p>以下為已保存的有限片段；私人筆記不在此快照。</p>''')
        for source in row['payload']['sources']:
            parts.append(f'<h4>{e(source["id"])}：{e(source["title"])}（{e(SCOPE_LABELS.get(source["scope"], source["scope"]))}）</h4>')
            parts.extend(show_excerpt(block) for block in source['excerpts'])
        binding = row['payload'].get('provider_binding')
        if binding:
            parts.append(f'<p>原提供者連線綁定：{e(binding)}</p>')
        if row.get('reason_code'):
            parts.append(f'<p>診斷代碼：{e(row["reason_code"])}</p>')
        parts.append('</details></div>')

    parts.append('<h3>找回相關原文</h3><p>比較閱讀：解決什麼問題？取回單位是什麼？證據在哪？有哪些限制？</p>')
    for index, source in enumerate(context['related']):
        if index == 3:
            parts.append('<details><summary>更多相關來源</summary>')
        href = reading_url(source['item_id'], source['version_id']) if source.get('version_id') else '/library/' + source['source_id']
        parts.append(f'''<div class="pick-item"><h3><a href="{e(href)}">{e(source['title'])}</a></h3>
            <p>{e(RELATION_LABELS[source['relation_kind']])}：{e(source['reason'][:180])}</p>
            <p>來源範圍：{e(SCOPE_LABELS.get(source['scope'], source['scope']))}，以下僅為短摘錄。</p>
            <p>適用概念：{e('、'.join(c['title'] for c in source['concepts']) or '詞彙建議，尚無策展概念')}</p>''')
        if source['excerpts']:
            guide = source.get('teaching_guide')
            if guide:
                parts.append(show_excerpt({**guide['evidence'][0], 'quality': 'fragment',
                    'quality_note': '教材選用的定位短引文；不是完整句或方法摘要。展開可核對自動選段。'}))
            else:
                parts.append(show_excerpt(source['excerpts'][0], short=True))
            parts.append('<details><summary>展開全部原文證據片段</summary>')
            parts.extend(show_excerpt(block) for block in source['excerpts'])
            parts.append('</details>')
        else:
            parts.append('<p>僅有來源卡資訊；無原文證據，不用於生成解說。</p>')
        parts.append(f'''<details><summary>關聯判斷紀錄</summary>
            <p>這是學習建議，並非論文引用或掌握程度。</p>
            <p>{e(source['relation_kind'])}／{e(source['assertion_status'])}</p></details></div>''')
    if len(context['related']) > 3:
        parts.append('</details>')
    if not context['related']:
        parts.append('<p>目前沒有可解釋的相關來源；可調整焦點或加入公開原文。</p>')
    if context['notes']:
        parts.append('<details><summary>我的筆記（僅留在本機，不送至模型）</summary>')
        for note in context['notes']:
            parts.append(f'''<p>{e(note['text'])}<br><a href="{e(note['reading_url'])}">筆記綁定原段落</a> ·
                <a href="{safe_href(note['source_url'])}" rel="noopener noreferrer">來源定位</a></p>''')
        parts.append('</details>')
    parts.append('</div></section>')
    return ''.join(parts)


def render_teaching(focal, safe_href, *, inline=False):
    brief, guide = focal.get('reading_brief'), focal.get('teaching_guide')
    parts = []
    if guide:
        identity = '' if inline else ' id="reading-guide"'
        heading = 'h4' if inline else 'h3'
        parts.append(f'''<div class="pick-item"{identity}>
            <p class="tag">策展教學 · Agent 協助／待核對</p>
            <{heading}>{e(guide['title'])}</{heading}><p><strong>讀完要能：</strong>{e(guide['goal'])}</p>
            <h4>問題與機制</h4><p>{e(guide['mechanism'])}</p>
            <h4>教學合成例子（非論文實驗）</h4><p>{e(guide['example'])}</p>
            <h4>串連舊知</h4><p>{e(guide['connections'])}</p>
            <p><strong>限制：</strong>{e(guide['limitations'])}</p>
            <h4>自我檢查</h4><p>{e(guide['question'])}</p>
            <details><summary>展開參考答案</summary><p>{e(guide['answer'])}</p></details>
            <details><summary>核對教學稿的固定版本原文</summary>
            <p>短引用用於定位方法選段，不能單憑逐字匹配認定整篇教學解讀正確。</p>''')
        for cite in guide['evidence']:
            parts.append(f'''<blockquote>{e(cite['quote'])}<br>
                <a href="{e(cite['reading_url'])}">固定版本原文 #{e(cite['anchor'])}</a> ·
                <a href="{safe_href(cite['source_url'])}" rel="noopener noreferrer">來源定位</a></blockquote>''')
        parts.append('</details>')
        if not inline:
            parts.append('''<p><a class="btn btn-secondary" href="#lesson-draft">編輯這份學習稿</a></p>
                <p>下方編輯框已帶入內容，手動保存後才會建立學習包；不會標記為已學會。</p>''')
        parts.append('</div>')
    if brief:
        parts.append('<details><summary>策展導讀：適合的原因與先備知識</summary>' if guide
                     else '<div class="pick-item"><h3>策展導讀</h3>')
        parts.append('<p>以下來自來源卡；不代表已核對全文或即時模型回答。</p>')
        for key, label in [('summary_zh', '內容'), ('why_fit_zh', '適合的原因'),
                           ('prerequisite_concepts', '先備知識'), ('cautions_zh', '閱讀提醒')]:
            value = brief.get(key)
            if value:
                if isinstance(value, list):
                    value = '、'.join(str(v) for v in value)
                parts.append(f'<p><strong>{label}：</strong>{e(value)}</p>')
        parts.append('</details>' if guide else '</div>')
    return ''.join(parts)


def render_journey(journey, safe_href):
    if not journey:
        return ''
    parts = ['<div class="learning-path" id="learning-path"><h3>同頁學習路徑</h3>']
    if journey['mode'] == 'curated':
        parts.append('<p>先補背景，再讀本篇，最後比較與自我檢查。可跳過已熟悉的部分；展開不會記為已掌握。</p>')
    else:
        parts.append('<p>依標題、原文開頭與指定焦點推薦背景；本篇尚無策展教學或經核對的跨篇比較。先讀背景，再帶著問題檢查原文。</p>')
    parts.append('<nav class="learning-nav" aria-label="本頁學習順序">')
    if journey['has_focal_guide']:
        parts.append('<a href="#reading-guide">讀本篇教學</a>')
    else:
        parts.append('<a href="#heading-content">帶著問題讀原文</a>')
    if journey['comparisons']:
        parts.append('<a href="#learning-comparison">比較新舊知識</a>')
    parts.append('<a href="#lesson-draft">整理我的理解</a></nav>')
    if journey['steps']:
        parts.append('<ol class="learning-steps">')
        for step in journey['steps']:
            parts.append(f'''<li id="background-{e(step['source_id'])}">
                <details><summary>{e(step['guide']['title'])}</summary>
                {render_teaching({'teaching_guide': step['guide']}, safe_href, inline=True)}
                </details><p class="learning-reason">{e(step['reason'])}</p>
                <p class="learning-prompt">帶著這個問題：{e(step['prompt'])}</p></li>''')
        parts.append('</ol>')
    elif journey['has_focal_guide'] and not journey['missing']:
        parts.append('<p>這篇可直接從下方教學開始；目前沒有指定先備教材。</p>')
    else:
        parts.append('<p>目前沒有足夠線索或可用教材建立背景路徑；可在深入檢索中指定比較焦點。</p>')
    if journey['missing']:
        parts.append('<details><summary>尚缺可核對版本的背景教材</summary><p>'
                     + e('、'.join(journey['missing'])) + '</p><p>缺少原文或版本不符的教材不顯示為已完成的比較。</p></details>')
    parts.append('</div>')
    return ''.join(parts)


def render_comparisons(journey, safe_href):
    if not journey or not journey['comparisons']:
        return ''
    parts = ['<div id="learning-comparison"><h3>新知放回舊知：差別在哪裡？</h3>',
             '<p>以下是固定版本的策展比較，仍需核對解讀；不是模型即時回答，也不是作者的引用關係。</p>']
    for comparison in journey['comparisons']:
        parts.append(f'<div class="pick-item"><h4>{e(comparison["title"])}</h4><p>{e(comparison["text"])}</p>')
        parts.append('<details><summary>核對兩邊原文</summary>')
        for source in comparison['evidence']:
            parts.append(f'<p><strong>{e(source["title"])}</strong> · 固定版本 #{e(source["version_id"])}</p>')
            for cite in source['evidence']:
                parts.append(f'''<blockquote>{e(cite['quote'])}<br>
                    <a href="{e(cite['reading_url'])}">固定版本 #{e(cite['anchor'])}</a> ·
                    <a href="{safe_href(cite['source_url'])}" rel="noopener noreferrer">來源定位</a></blockquote>''')
        parts.append('</details></div>')
    parts.append('</div>')
    return ''.join(parts)
