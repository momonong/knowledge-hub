"""Reading guide, edition follow-ups, and private capture controls."""
import json

from .study_views import e, hidden


def guide_panel():
    return """<section id="layer-guide" class="paper-guide layer-guide" data-layer-guide data-position aria-labelledby="layer-guide-title">
      <h2 id="layer-guide-title">同頁導讀</h2>
      <p class="meta layer-status" data-layer-guide-status role="status">正在準備這個版本的導讀…</p>
      <div data-layer-guide-content></div>
      <div class="layer-actions guide-controls" data-layer-guide-actions>
        <button type="button" class="button secondary" data-layer-guide-retry hidden>重試導讀</button>
        <button type="button" class="text-action" data-layer-guide-fallback hidden>改用備援帶讀</button>
      </div>
      <noscript><p>開啟 JavaScript 可載入導讀。</p></noscript>
    </section>"""


def conversation(item_id, version_id, csrf, state, history, captures, *,
                 primary_model=False, fallback_model=False):
    payload = json.dumps({'history':list(history), 'captures':list(captures)}, ensure_ascii=False)
    # A closing script tag is significant in HTML even for application/json.
    payload = payload.replace('&',r'\u0026').replace('<',r'\u003c').replace('>',r'\u003e')
    route = '主要模型' if primary_model else '本站原文查讀（依規則選段）'
    return f"""<div class="layer-workspace" data-layer-conversation
      data-layer-primary="{str(bool(primary_model)).lower()}" data-layer-fallback="{str(bool(fallback_model)).lower()}">
      <div class="conversation-head"><h2 id="chat-heading">陪讀追問</h2><a class="text-action" href="#layer-captures">我的沉澱</a></div>
      <p class="meta" data-layer-current-mode>{route}</p>
      <form method="post" action="/items/{e(item_id)}/followup" data-layer-followup>
        {hidden('csrf',csrf)}{hidden('version_id',version_id)}
        <div class="layer-focus" data-layer-focus hidden>
          <p class="meta" data-layer-focus-label>已選原文</p>
          <blockquote data-layer-focus-quote></blockquote>
          <div class="layer-actions"><button type="button" class="text-action" data-layer-clear-focus>清除選取</button></div>
          <div data-layer-tag-options>
            <label for="layer-comment">補一句短評（選填）</label><input id="layer-comment" maxlength="1000">
            <div class="layer-actions"><button type="button" class="button secondary" data-layer-tag="#想法">想法</button>
            <button type="button" class="button secondary" data-layer-tag="#盲點/待查">盲點／待查</button>
            <button type="button" class="button secondary" data-layer-tag="#可試看看">可試看看</button></div>
          </div>
        </div>
        <label for="followup">你想接著問什麼？</label>
        <textarea id="followup" name="question" data-draft="question_draft" rows="3" maxlength="2000" required>{e(state.get('question_draft',''))}</textarea>
        <div class="layer-actions"><button class="button" type="submit" data-layer-send>送出追問</button>
          <button class="button secondary" type="button" data-layer-cancel hidden>停止回覆</button>
        </div>
      </form>
      <p id="draft-status" role="status" class="save-status">私人閱讀草稿</p>
      <p class="meta layer-status" role="status" data-layer-followup-status></p>
      <div class="conversation-log layer-history" role="log" aria-label="此版本的追問" data-layer-history></div>
      <section id="layer-captures" class="layer-captures" data-private aria-labelledby="layer-captures-title">
        <h3 id="layer-captures-title">我的沉澱</h3>
        <p class="meta layer-status" data-layer-capture-status role="status"></p>
        <button type="button" class="text-action" data-layer-captures-refresh>重新讀取紀錄</button>
        <div data-layer-capture-list></div>
      </section>
      <details class="layer-recall" data-private data-layer-recall>
        <summary>找回相關筆記與問題</summary>
        <form data-layer-recall-form>
          <label for="layer-recall-query">搜尋既有理解</label>
          <input id="layer-recall-query" type="search" name="q" maxlength="200">
          <button type="submit" class="button secondary">搜尋</button>
        </form>
        <p class="meta layer-status" role="status" data-layer-recall-status></p>
        <ul data-layer-recall-results></ul>
      </details>
      <script type="application/json" data-layer-initial>{payload}</script>
      <noscript><p>開啟 JavaScript 可使用即時追問與沉澱工具。</p></noscript>
    </div>"""
