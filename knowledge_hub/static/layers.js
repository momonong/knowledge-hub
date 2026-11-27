'use strict';
(() => {
  const kindNames = {evidence:'原文依據', limits:'限制與邊界', inference:'延伸推論'};
  const tagNames = {'#想法':'想法', '#盲點/待查':'盲點／待查', '#可試看看':'可試看看'};
  function safeHref(value, base, origin, allowPublisher = false) {
    if (typeof value !== 'string' || /[\s\\]/u.test(value)) return null;
    try {
      if (value.startsWith('/') && !value.startsWith('//')) {
        const target = new URL(value, origin);
        const path = base && target.pathname.startsWith(base + '/')
          ? target.pathname.slice(base.length) : target.pathname;
        if (target.origin === origin && /^\/(?:items\/[1-9]\d*|library\/[\w.-]+)$/.test(path))
          return base + path + target.search + target.hash;
      } else if (allowPublisher && value.startsWith('https://')) {
        const target = new URL(value);
        if (!target.username && !target.password) return target.href;
      }
    } catch (_) {}
    return null;
  }

  async function readEvents(stream, dispatch) {
    if (!stream?.getReader) throw new Error('無法接收串流回覆。');
    const input = stream.getReader();
    const decoder = new TextDecoder('utf-8', {fatal:true});
    let pending = '';
    try {
      while (true) {
        const {done, value} = await input.read();
        pending += decoder.decode(value, {stream:!done});
        let boundary;
        while ((boundary = /\r?\n\r?\n/.exec(pending))) {
          if (boundary.index > 131072) throw new Error('串流段落過長。');
          const frame = pending.slice(0,boundary.index);
          pending = pending.slice(boundary.index + boundary[0].length);
          let kind = '';
          const data = [];
          for (const line of frame.split(/\r?\n/)) {
            if (line.startsWith('event:')) kind = line.slice(6).trim();
            if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /,''));
          }
          if (kind && data.length) dispatch(kind, JSON.parse(data.join('\n')));
        }
        if (pending.length > 131072) throw new Error('串流段落過長。');
        if (done) {
          if (pending.trim()) throw new Error('串流在段落傳完前中斷。');
          break;
        }
      }
    } finally {
      input.releaseLock();
    }
  }

  if (typeof module === 'object' && module.exports) module.exports = {readEvents, safeHref};
  if (typeof document === 'undefined') return;
  const reader = document.querySelector('[data-reader]');
  if (!reader) return;
  const workspace = reader.querySelector('[data-layer-conversation]');
  const guide = reader.querySelector('[data-layer-guide]');
  const base = document.querySelector('meta[name="kh-base-path"]')?.content || '';
  const {item, version} = reader.dataset;
  const endpoint = path => `${base}/items/${encodeURIComponent(item)}/${path}`;
  const versioned = (path, query = {}) => endpoint(path) + '?' + new URLSearchParams({version,...query});
  const config = {credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer'};
  const make = (parent, tag, text = '', className = '') => {
    const element = document.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    parent.append(element);
    return element;
  };
  const button = (parent, label, click, className = 'button secondary') => {
    const element = make(parent,'button',label,className);
    element.type = 'button';
    element.addEventListener('click',click);
    return element;
  };
  const link = (parent, label, path, publisher = false) => {
    const href = safeHref(path,base,location.origin,publisher);
    if (!href) return;
    const anchor = make(parent,'a',label);
    anchor.href = href;
    if (publisher) anchor.rel = 'noopener noreferrer';
    return anchor;
  };
  function say(element, text, error = false) {
    element.textContent = text;
    element.toggleAttribute('data-error',error);
  }
  const requestID = () => [...crypto.getRandomValues(new Uint8Array(18))]
    .map(byte => byte.toString(16).padStart(2,'0')).join('');
  const currentAnchor = () => {
    try {return decodeURIComponent(location.hash.slice(1));} catch (_) {return '';}
  };
  const citationsOf = section => Array.isArray(section.citations) ? section.citations : [];
  const validAnchor = value => typeof value === 'string' && /^[\w.:-]{1,200}$/.test(value);
  const visualCitation = cite => typeof cite.href === 'string' && cite.href.includes('#kh-visual-');
  function sameEdition(path, anchor) {
    const href = safeHref(path,base,location.origin);
    try {
      if (!href || !validAnchor(anchor)) return null;
      const url = new URL(href,location.origin);
      const editions = url.searchParams.getAll('version');
      if (url.pathname === endpoint('').slice(0,-1) && editions.length === 1
          && editions[0] === version && decodeURIComponent(url.hash.slice(1)) === anchor) return href;
    } catch (_) {}
    return null;
  }
  function evidenceLink(parent, label, path, anchor, quote) {
    const href = sameEdition(path,anchor);
    if (!href) return null;
    const control = link(parent,label,href);
    control.addEventListener('click',event => {
      if (event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const reveal = new CustomEvent('kh-reveal-evidence',{cancelable:true,detail:{anchor,quote,href}});
      if (!reader.dispatchEvent(reveal)) event.preventDefault();
    });
    return control;
  }
  const openFollowup = focusQuestion => reader.dispatchEvent(new CustomEvent('kh-open-followup',{detail:{focusQuestion}}));
  const tagged = new Map();
  let markFrame = 0;
  function markSelections() {
    if (!tagged.size || markFrame) return;
    markFrame = requestAnimationFrame(() => {
      markFrame = 0;
      const guides = [...reader.querySelectorAll('[data-layer-guide] [data-layer-quote]')];
      const names = {'#想法':'kh-idea','#盲點/待查':'kh-question','#可試看看':'kh-try'};
      const ranges = new Map();
      for (const tag of tagged.values()) {
        const paragraph = document.getElementById(tag.anchor)?.querySelector('.prose');
        for (const el of [paragraph,...guides.filter(g=>g.dataset.anchor === tag.anchor)]) {
          const original = el === paragraph || el?.hasAttribute('data-layer-citation-quote');
          const quote = original ? tag.quote : tag.display_quote ?? tag.quote;
          if (!el || !quote) continue;
          const node = el.firstChild;
          const i = node?.nodeType === 3 ? node.textContent.indexOf(quote) : -1;
          if (i < 0) continue;
          const range = document.createRange();
          range.setStart(node,i);
          range.setEnd(node,i+quote.length);
          const name = names[tag.tag];
          if (!name) continue;
          if (!ranges.has(name)) ranges.set(name,[]);
          ranges.get(name).push(range);
          if (!el.parentElement.querySelector('[data-mark-for="' + tag.id + '"]')) {
            const badge = link(el.parentElement,tag.tag,'/items/'+item+'?version='+version+'#capture-'+tag.id);
            if (badge) {badge.dataset.markFor = String(tag.id); badge.className = 'source-tag';}
          }
        }
      }
      if (globalThis.CSS?.highlights && globalThis.Highlight) {
        for (const [name,list] of ranges) CSS.highlights.set(name,new Highlight(...list));
      }
    });
  }
  function showCitation(parent, cite, choose) {
    if (!validAnchor(cite.anchor) || typeof cite.quote !== 'string') return;
    const visual = visualCitation(cite);
    const target = visual ? 'kh-visual-' + cite.anchor : cite.anchor;
    if (!sameEdition(cite.href,target)) return;
    const block = make(parent,'blockquote','','layer-citation');
    const text = make(block,'p',cite.quote);
    text.dataset.layerQuote = '';
    text.dataset.anchor = cite.anchor;
    if (visual) {
      text.dataset.layerVisual = '';
      text.dataset.layerCitationQuote = '';
    }
    const actions = make(block,'div','','layer-actions');
    evidenceLink(actions,visual ? '看同版圖表' : '看同版段落',cite.href,target,cite.quote);
    link(actions,'出版者原文',cite.source_url,true);
    if (choose) button(actions,'問這段',() =>
      choose({anchor:cite.anchor,quote:cite.quote,visual},true),'text-action');
  }

  const followup = workspace ? setupWorkspace(workspace) : null;
  if (guide) setupGuide(guide,followup);

  function setupGuide(root, followup) {
    const state = root.querySelector('[data-layer-guide-status]');
    const content = root.querySelector('[data-layer-guide-content]');
    const retry = root.querySelector('[data-layer-guide-retry]');
    const fallback = root.querySelector('[data-layer-guide-fallback]');
    const hasFallback = workspace?.dataset.layerFallback === 'true';
    const roles = ['mechanism','evidence','boundary'];
    const handled = new Set();
    let loading = false, currentRoute = 'primary', retryAllowed = false;
    let pendingAppQuestion = null, readyChips = null;

    function finishAppQuestion() {
      if (!pendingAppQuestion || !readyChips) return;
      const {role,question} = pendingAppQuestion;
      pendingAppQuestion = null;
      const chip = readyChips.get(role);
      const matches = chip?.question === question;
      if (matches && chip.ask()) return;
      followup.review(question,matches
        ? '另一則追問仍在處理。你可以先調整這個問題，再手動送出。'
        : '目前的同版導讀無法核對這個建議追問。請檢查或修改問題，再手動送出。');
    }

    if (followup && item && version) {
      reader.addEventListener('kh-app-followup',event => {
        if (event.target !== reader || !event.cancelable || event.defaultPrevented) return;
        const {item: sentItem,version: sentVersion,role,question} = event.detail || {};
        if (sentItem !== item || sentVersion !== version || !roles.includes(role)
            || typeof question !== 'string' || question.length > 2000 || !question.trim()) return;
        const key = JSON.stringify([role,question]);
        if (handled.has(key)) {
          event.preventDefault();
          return;
        }
        if (pendingAppQuestion) return;
        event.preventDefault();
        handled.add(key);
        pendingAppQuestion = {role,question};
        finishAppQuestion();
      });
      reader.dataset.khAppFollowup = '1';
    }

    function prose(parent, section) {
      const paragraph = make(parent,'p',section.missing ? '原文未交代' : section.text,'prose');
      const citations = citationsOf(section).filter(cite =>
        sameEdition(cite.href,visualCitation(cite) ? 'kh-visual-' + cite.anchor : cite.anchor));
      const connected = citations.find(cite => !visualCitation(cite)) || citations[0];
      if (connected && !section.missing) {
        paragraph.dataset.layerQuote = '';
        paragraph.dataset.anchor = connected.anchor;
        if (visualCitation(connected)) paragraph.dataset.layerVisual = '';
      }
      if (citations.length) {
        const details = make(parent,'details','','guide-citations');
        make(details,'summary','核對原文');
        for (const cite of citations) showCitation(details,cite,followup?.focus);
      }
    }

    function visualCard(parent, visual) {
      if (!visual || typeof visual.text !== 'string' || typeof visual.label !== 'string') return;
      const target = 'kh-visual-' + visual.anchor;
      if (!sameEdition(visual.href,target)) return;
      const card = make(parent,'div','','guide-visual-card');
      const focus = typeof visual.focus === 'string' ? visual.focus : '';
      const jump = evidenceLink(card,'查看 ' + visual.label,visual.href,target,focus);
      jump.dataset.guideVisualLink = '';
      make(card,'p',visual.text,'guide-visual-instruction');
      if (focus) make(card,'blockquote',focus,'guide-visual-quote');
    }

    function renderModel(result) {
      const byRole = new Map(result.sections.map(section => [section.role,section]));
      function stage(role, title) {
        const node = make(content,'section','','guide-stage');
        node.dataset.guideStage = role;
        make(node,'h3',title);
        return node;
      }
      const hook = stage('hook','從這裡切入');
      prose(hook,byRole.get('hook'));
      const mechanism = stage('mechanism','一起拆解機制');
      prose(mechanism,byRole.get('mechanism'));
      visualCard(mechanism,result.visual);
      const check = byRole.get('mechanism').check;
      if (check?.question && check.answer) {
        const exercise = make(mechanism,'section','','guide-tradeoffs');
        make(exercise,'h4','先自己推一次');
        make(exercise,'p',check.question,'prose');
        const reveal = make(exercise,'details','','guide-citations');
        make(reveal,'summary','看推理過程');
        prose(reveal,{...byRole.get('mechanism'),text:check.answer});
      }
      const evidence = stage('evidence','數據與代價');
      prose(evidence,byRole.get('evidence'));
      const limits = make(evidence,'div','','guide-tradeoffs');
      make(limits,'h4','適用條件與限制');
      prose(limits,byRole.get('limits'));

      const questions = stage('chips','接著追問');
      const ready = make(questions,'div','','guide-chips');
      const chips = Array.isArray(result.chips) ? result.chips : [];
      const verified = new Map();
      for (const role of roles) {
        const chip = chips.find(entry => entry?.role === role);
        if (typeof chip?.question !== 'string' || !chip.question.trim()) continue;
        const cite = citationsOf(chip).find(candidate => candidate.anchor === chip.anchor &&
          sameEdition(candidate.href,visualCitation(candidate) ? 'kh-visual-' + candidate.anchor : candidate.anchor));
        if (!cite || typeof cite.quote !== 'string' || !followup) continue;
        const ask = () => followup.ask(chip.question,cite,result.model.route);
        const action = button(ready,chip.question,ask,'guide-chip');
        action.dataset.guideChip = role;
        verified.set(role,{question:chip.question,ask});
      }
      if (!ready.childElementCount) make(questions,'p','這個版本目前沒有可直接送出的建議追問。','meta');
      return verified;
    }

    function renderLocal(result) {
      for (const section of result.sections) {
        const step = make(content,'details','','layer-guide-step source-explanation');
        if (section.title === '核心圖表讀法') step.dataset.visualGuide = '';
        step.open = content.childElementCount === 1;
        make(step,'summary',section.title);
        prose(step,section);
      }
    }

    async function loadGuide(route = 'primary', explicitRetry = false) {
      if (loading) return;
      loading = true;
      readyChips = null;
      let verified = new Map();
      currentRoute = route;
      retry.hidden = fallback.hidden = true;
      content.setAttribute('aria-busy','true');
      say(state,'正在整理這個版本的帶讀…');
      try {
        const response = await fetch(versioned('guide',{...(route === 'fallback' ? {route} : {}),
          ...(explicitRetry ? {retry:'1'} : {})}),config);
        if (!response.ok) throw new Error();
        const result = await response.json();
        if (String(result.version_id) !== version || !Array.isArray(result.sections)
            || (result.item_id !== undefined && String(result.item_id) !== item)) throw new Error();
        const generated = result.mode === 'model' && result.model?.state === 'ready';
        if (generated && !['hook','mechanism','evidence','limits'].every(role =>
          result.sections.some(section => section.role === role && typeof section.text === 'string'))) throw new Error();
        content.replaceChildren();
        if (generated) {
          verified = renderModel(result);
          say(state,result.scope === 'abstract' ? '本版只有摘要，以下是先讀提要。取得全文後可讀完整導讀。' :
            '可以一段段讀，也可以先作答再展開推理。');
        } else {
          renderLocal(result);
          const model = result.model || {};
          const descriptions = {
            unavailable:'帶讀模型尚未設定。下方是同版原文摘錄。',
            running:'模型正在整理逐段導讀。下方可先讀同版原文摘錄。',
            blocked:'目前缺少可供帶讀的同版證據。下方列出已取得的段落。',
            failed:'這次逐段導讀暫時無法完成。下方可先讀同版原文摘錄。',
          };
          const description = descriptions[model.state] || '本站依同版原文整理。';
          say(state,model.state === 'failed' && model.message ? description + model.message : description,
            model.state === 'failed');
          retryAllowed = model.state !== 'running';
          retry.hidden = !['failed','running'].includes(model.state);
          retry.textContent = model.state === 'running' ? '查看帶讀進度' : '重試帶讀';
          fallback.hidden = !hasFallback || route !== 'primary' || !['failed','unavailable'].includes(model.state);
        }
        markSelections();
      } catch (_) {
        say(state,'帶讀暫時無法載入。可以先閱讀同版原文。',true);
        retryAllowed = true;
        retry.textContent = '重試帶讀';
        retry.hidden = false;
        fallback.hidden = !hasFallback || route !== 'primary';
      } finally {
        loading = false;
        content.removeAttribute('aria-busy');
        readyChips = verified;
        finishAppQuestion();
      }
    }
    retry.addEventListener('click',()=>loadGuide(currentRoute,retryAllowed));
    fallback.addEventListener('click',()=>loadGuide('fallback'));
    loadGuide();
  }

  function setupWorkspace(root) {
    const find = name => root.querySelector(`[data-layer-${name}]`);
    const form = find('followup');
    const question = form.elements.question;
    const csrf = form.elements.csrf.value;
    const status = find('followup-status');
    const captureStatus = find('capture-status');
    const log = find('history');
    const captureList = find('capture-list');
    const send = find('send');
    const cancel = find('cancel');
    const comment = root.querySelector('#layer-comment');
    const canFallback = root.dataset.layerFallback === 'true';
    const placeholder = make(log,'p','問題和已核對的回覆會留在這個版本。','chat-empty');
    const capturesEmpty = make(captureList,'p','這個版本還沒有標籤或理解。','meta');
    let focus = null, selection = null, active = null;
    const turns = new Map(), captureNodes = new Map(), mutationIds = new Map(), pendingMutations = new Set();
    const toolbar = document.createElement('div');
    toolbar.className = 'layer-selection-tools layer-actions';
    toolbar.hidden = true;
    toolbar.setAttribute('role','group');
    toolbar.setAttribute('aria-label','選取文字的動作');
    reader.prepend(toolbar);
    const useSelection = button(toolbar,'問這段',() => {
      if (selection) setFocus(selection,true);
    },'button');
    const selectionTags = Object.entries(tagNames).map(([tag,name]) =>
      button(toolbar,name,() => tagSelection(tag)));
    button(toolbar,'關閉',clearSelection,'text-action');
    const inlineNotice = make(toolbar,'p','','layer-selection-notice');
    inlineNotice.setAttribute('role','status');
    inlineNotice.hidden = true;
    const receipt = make(reader,'p','','layer-selection-receipt');
    receipt.setAttribute('role','status');
    let rangeAtSelection = null, positionFrame = 0, selectionSaving = false;

    function clearSelection() {
      selection = null;
      rangeAtSelection = null;
      toolbar.hidden = true;
      inlineNotice.hidden = true;
    }

    function positionSelection() {
      if (positionFrame || toolbar.hidden || !rangeAtSelection) return;
      positionFrame = requestAnimationFrame(() => {
        positionFrame = 0;
        if (toolbar.hidden || !rangeAtSelection) return;
        const bounds = rangeAtSelection.getBoundingClientRect();
        const viewport = window.visualViewport;
        const leftEdge = viewport?.offsetLeft || 0;
        const topEdge = viewport?.offsetTop || 0;
        const width = viewport?.width || window.innerWidth;
        const height = viewport?.height || window.innerHeight;
        const toolbarBounds = toolbar.getBoundingClientRect();
        if (bounds.bottom <= topEdge || bounds.top >= topEdge + height) {
          clearSelection();
          return;
        }
        const gap = 8;
        const left = Math.min(leftEdge + width - toolbarBounds.width - gap,
          (bounds.left + bounds.right - toolbarBounds.width) / 2);
        const above = bounds.top - toolbarBounds.height - gap;
        const top = above >= topEdge + gap ? above : Math.min(bounds.bottom + gap,
          topEdge + height - toolbarBounds.height - gap);
        toolbar.style.left = `${Math.max(leftEdge + gap,left)}px`;
        toolbar.style.top = `${Math.max(topEdge + gap,top)}px`;
        toolbar.dataset.selectionPlaced = '';
      });
    }

    function setFocus(value, move = false) {
      focus = value;
      clearSelection();
      find('focus').hidden = !focus;
      find('tag-options').hidden = !!focus?.visual && !focus.taggable;
      find('focus-label').textContent = focus?.visual ? '已選同版圖表' : '已選原文或導讀';
      find('focus-quote').textContent = focus?.quote || (focus?.visual ? '請對照作者圖表。' : '');
      comment.value = '';
      if (move) {
        openFollowup(true);
        question.focus();
      }
    }

    function sourceSelection(node) {
      const element = node?.nodeType === 1 ? node : node?.parentElement;
      if (!element || element.closest('[data-private]')) return null;
      return element.closest('#panel-original .source-paragraph[data-anchor] > .prose, [data-layer-guide] [data-layer-quote]');
    }

    document.addEventListener('selectionchange',() => {
      // Native touch handle interactions can collapse the DOM selection
      // before click. The immutable anchor and quote survive that gesture.
      const displayed = window.getSelection();
      if (!displayed?.rangeCount || displayed.isCollapsed) return;
      const range = displayed.getRangeAt(0);
      const start = sourceSelection(range.startContainer);
      const end = sourceSelection(range.endContainer);
      const anchor = start?.dataset.anchor || start?.parentElement?.dataset.anchor;
      const quote = displayed.toString().trim().slice(0,3000);
      if (!start || start !== end || !reader.contains(start) || !validAnchor(anchor) || !quote) {
        clearSelection();
        return;
      }
      const visual = start.hasAttribute('data-layer-visual');
      const taggable = !start.hasAttribute('data-layer-citation-quote');
      selection = {anchor,quote,visual,taggable};
      rangeAtSelection = range.cloneRange();
      selectionTags.forEach(tag => {tag.hidden = !taggable;});
      inlineNotice.hidden = true;
      toolbar.hidden = false;
      positionSelection();
    });
    toolbar.addEventListener('pointerdown',event => {
      if (event.pointerType === 'mouse') event.preventDefault();
    });
    document.addEventListener('pointerdown',event => {
      if (!toolbar.contains(event.target)) clearSelection();
    });
    document.addEventListener('keydown',event => {
      if (event.key === 'Escape' && !toolbar.hidden) {
        clearSelection();
        event.preventDefault();
      }
    });
    document.addEventListener('scroll',positionSelection,true);
    window.addEventListener('resize',positionSelection);
    window.visualViewport?.addEventListener('resize',positionSelection);

    async function tagSelection(tag) {
      const selected = selection;
      if (!selected?.taggable || selectionSaving) return;
      selectionSaving = true;
      selectionTags.forEach(control => {control.disabled = true;});
      say(inlineNotice,'正在保存…');
      inlineNotice.hidden = false;
      positionSelection();
      try {
        const record = await saveCapture({action:'tag',tag,anchor:selected.anchor,quote:selected.quote,comment:''});
        if (record) say(receipt,`已保存 ${tag} 標記。`);
        if (selected !== selection) return;
        if (record) clearSelection();
        else {
          say(inlineNotice,captureStatus.textContent,true);
          positionSelection();
        }
      } finally {
        selectionSaving = false;
        selectionTags.forEach(control => {control.disabled = false;});
      }
    }

    // The reader's existing figure button prepares the question. Capture its
    // actual versioned anchor before that button's own click handler runs.
    reader.addEventListener('click',event => {
      const control = event.target.closest?.('.visual-card-body > button.text-action');
      const card = control?.closest('details[id^="kh-visual-"]');
      const anchor = card?.id.slice('kh-visual-'.length);
      if (card && validAnchor(anchor)) setFocus({anchor,visual:true,
        quote:card.querySelector('.visual-caption')?.textContent?.trim().slice(0,3000) || ''});
    },true);

    find('clear-focus').addEventListener('click',() => setFocus(null));
    root.querySelectorAll('[data-layer-tag]').forEach(tag => tag.addEventListener('click',() =>
      saveTag(tag.dataset.layerTag,focus,comment.value)));
    function saveTag(tag, selected, note) {
      if (!selected || (selected.visual && !selected.taggable)) return;
      if (!focus || selected.anchor !== focus.anchor || selected.quote !== focus.quote)
        setFocus(selected);
      return saveCapture({action:'tag',tag,anchor:selected.anchor,quote:selected.quote,comment:note});
    }

    async function jsonRequest(url, options = {}) {
      const response = await fetch(url,{...config,...options});
      let data;
      try {data = await response.json();} catch (_) {
        const error = new Error('暫時無法讀取回覆，請再試一次。');
        error.status = response.status;
        throw error;
      }
      if (!response.ok) {
        const error = new Error(typeof data.error === 'string' ? data.error : '請求未完成，請再試一次。');
        error.status = response.status;
        throw error;
      }
      return data;
    }

    const saveSize = body => new TextEncoder().encode(body.toString()).length;
    async function saveCapture(fields, {keepalive = false, onRecord, onConflict} = {}) {
      const key = JSON.stringify(fields);
      if (pendingMutations.has(key)) return false;
      if (!mutationIds.has(key)) mutationIds.set(key,requestID());
      pendingMutations.add(key);
      say(captureStatus,'正在保存…');
      try {
        const body = new URLSearchParams({csrf,version_id:version,request_id:mutationIds.get(key),...fields});
        const {record} = await jsonRequest(endpoint('captures'),{method:'POST',body,keepalive:keepalive && saveSize(body) < 60000});
        if (!record || !(onRecord ? onRecord(record) : renderCapture(record)))
          throw new Error('未能確認保存結果，請再試一次。');
        mutationIds.delete(key);
        say(captureStatus,record.state === 'draft' ? '理解草稿已保存。' : '已保存在這個版本。');
        return record;
      } catch (error) {
        if (error.status >= 400 && error.status < 500) mutationIds.delete(key);
        if (error.status === 409) onConflict?.();
        const message = error.status === 409 ? '另一個裝置已有更新。請先保留這裡的草稿，再重新讀取紀錄。' :
          (error.message || '未能確認保存結果，請再試一次。');
        say(captureStatus,message,true);
        return false;
      } finally {
        pendingMutations.delete(key);
      }
    }

    function createEditor(parent, initial) {
      let record = initial;
      let timer, saving = null, snapshot = null, conflicted = false, confirming = false;
      const editor = make(parent,'form');
      const field = make(editor,'textarea');
      field.value = record.text || '';
      field.rows = 4;
      field.maxLength = 8000;
      field.setAttribute('aria-label','編輯我的理解草稿');
      const editorState = make(editor,'p','','meta layer-status');
      editorState.setAttribute('role','status');
      const actions = make(editor,'div','','layer-actions');
      const save = button(actions,'儲存草稿',() => flush());
      const confirm = make(actions,'button','確認為我的理解','button');
      confirm.type = 'submit';
      const copy = button(actions,'複製目前草稿',async () => {
        try {
          await navigator.clipboard.writeText(field.value);
          say(editorState,'已複製這份草稿。重新開啟後可核對最新版本。');
        } catch (_) {
          field.focus();
          field.select();
          say(editorState,'目前草稿已選取。請使用裝置的複製功能，再重新開啟。');
        }
      });
      copy.hidden = true;
      const dirty = () => field.value.trim() !== (record.text || '');
      const fields = (action,text) => ({action,id:String(record.id),revision:String(record.revision),text});
      function conflict() {
        conflicted = true;
        save.disabled = true;
        confirm.disabled = true;
        copy.hidden = false;
        say(editorState,'這個版本在另一處已有更新。目前輸入留在這裡，可以先複製。',true);
      }
      function reconcile(updated, fromSave = false) {
        if (String(updated.id) !== String(record.id) || String(updated.version_id ?? version) !== version
            || Number(updated.revision) < Number(record.revision)) return false;
        if (!fromSave && dirty() && Number(updated.revision) > Number(record.revision)) {conflict(); return true;}
        if (!fromSave && !dirty()) field.value = updated.text || '';
        record = updated;
        const saved = captureNodes.get(String(updated.id));
        if (saved) {
          saved.revision = updated.revision;
          saved.text = updated.text;
        }
        return true;
      }
      function schedule() {
        clearTimeout(timer);
        if (!conflicted && !confirming) timer = setTimeout(() => flush(),700);
      }
      async function flush(action = 'edit', keepalive = false) {
        clearTimeout(timer);
        if (conflicted || (confirming && action !== 'confirm')) return false;
        if (saving) {
          const previous = await saving;
          if (!previous || conflicted) return false;
          return flush(action,keepalive);
        }
        const text = field.value.trim();
        if (action === 'edit' && !dirty()) return true;
        if (!text) {say(editorState,'請保留至少一行理解再保存。',true); return false;}
        snapshot = text;
        say(editorState,'正在保存草稿…');
        const options = {keepalive,onConflict:conflict};
        options.onRecord = action === 'edit' ? updated => reconcile(updated,true) :
          updated => renderCapture(updated,true);
        const pending = saveCapture(fields(action,text),options);
        saving = pending;
        const saved = await pending;
        saving = null;
        snapshot = null;
        if (saved && action === 'edit') {
          say(editorState,dirty() ? '還有新修改待保存…' : '草稿已保存。');
          if (dirty()) schedule();
        } else if (!saved && !conflicted) {
          say(editorState,'未能確認保存，輸入仍保留。可手動再試。',true);
        }
        return !!saved;
      }
      field.addEventListener('input',() => {
        if (conflicted) return;
        say(editorState,'有修改尚未保存。');
        schedule();
      });
      editor.addEventListener('submit',async event => {
        event.preventDefault();
        if (conflicted || confirming) return;
        confirming = true;
        field.readOnly = true;
        save.disabled = true;
        confirm.disabled = true;
        try {
          if (await flush('confirm')) document.getElementById(`capture-${record.id}`)?.querySelector('h4')?.focus();
        } finally {
          confirming = false;
          field.readOnly = false;
          if (!conflicted) {
            save.disabled = false;
            confirm.disabled = false;
          }
        }
      });
      // A concurrent older response cannot overwrite text typed while it was
      // saving. An in-flight snapshot leaves only its own revision unblocked.
      return {reconcile,dirty,flush,needsLeaveWarning:() => dirty() &&
        (conflicted || !field.value.trim() || (saving && snapshot !== field.value.trim())
          || saveSize(new URLSearchParams(fields('edit',field.value))) >= 58000)};
    }

    function renderCapture(record, confirmed = false) {
      const id = String(record.id);
      if (!/^[1-9]\d*$/.test(id) || String(record.version_id ?? version) !== version) return false;
      const previous = captureNodes.get(id);
      if (previous && Number(previous.revision) > Number(record.revision)) return true;
      if (previous?.editor && record.state === 'draft' && record.kind === 'understanding')
        return previous.editor.reconcile(record);
      if (previous?.editor?.dirty() && record.state === 'saved' && !confirmed) {
        // A remote confirmation may race with a local edit. Keep the old form
        // until the user has copied its unsaved text.
        previous.editor.reconcile(record);
        return true;
      }
      const node = document.createElement('article');
      node.id = `capture-${id}`;
      node.setAttribute('data-position','');
      if (record.kind === 'tag') {tagged.set(id,record); markSelections();}
      node.className = 'layer-capture';
      node.dataset.captureKind = record.kind;
      if (record.turn_id) {
        node.dataset.originTurn = String(record.turn_id);
      }
      const title = record.kind === 'understanding'
        ? (record.state === 'draft' ? '我的理解草稿' : '我的理解')
        : record.kind === 'missing' ? '盲點／待查' : tagNames[record.tag] || '段落標籤';
      make(node,'h4',title);
      const editor = record.state === 'draft' && record.kind === 'understanding' ? createEditor(node,record) : null;
      if (!editor) {
        if (record.quote) make(node,'blockquote',record.quote,'capture-quotation');
        if (record.comment) make(node,'p',record.comment,'capture-comment');
        if (record.text) make(node,'p',record.text,'prose');
        node.querySelector('h4').tabIndex = -1;
        link(node,'回到原文或追問',record.href);
      }
      if (previous) previous.element.replaceWith(node);
      else captureList.prepend(node);
      captureNodes.set(id,{element:node,revision:record.revision,text:record.text,editor});
      capturesEmpty.hidden = true;
      return true;
    }

    const refresh = find('captures-refresh');
    refresh.addEventListener('click',async () => {
      const unsaved = [...captureNodes.values()].some(({editor}) => editor?.dirty());
      if (unsaved) {
        say(captureStatus,'請先保存或複製正在編輯的草稿，再重新讀取紀錄。',true);
        return;
      }
      refresh.disabled = true;
      try {
        const data = await jsonRequest(versioned('captures'));
        if (!Array.isArray(data.records)) throw new Error();
        for (const record of [...data.records].reverse()) renderCapture(record);
        say(captureStatus,'已讀取這個版本的紀錄。');
      } catch (_) {say(captureStatus,'暫時無法讀取，已顯示的紀錄仍保留。',true);}
      finally {refresh.disabled = false;}
    });
    function leaveEdits() {
      for (const {editor} of captureNodes.values()) {
        if (editor?.dirty()) editor.flush('edit',true);
      }
    }
    window.addEventListener('pagehide',leaveEdits);
    document.addEventListener('visibilitychange',() => {if (document.hidden) leaveEdits();});
    window.addEventListener('beforeunload',event => {
      const warn = [...captureNodes.values()].some(({editor}) => editor?.needsLeaveWarning());
      leaveEdits();
      if (warn) {event.preventDefault(); event.returnValue = '';}
    });

    function makeTurn(questionText, route) {
      placeholder.hidden = true;
      const node = make(log,'article','','chat-turn layer-turn');
      const identity = make(node,'p','','meta');
      make(node,'p',questionText,'chat-question');
      const answer = make(node,'div','','layer-answer');
      const turnStatus = make(node,'p','','meta layer-status');
      const actions = make(node,'div','','layer-actions');
      return {node,answer,identity,turnStatus,actions,route,sections:new Set()};
    }
    function identify(turn, id, mode) {
      if (/^[1-9]\d*$/.test(String(id))) {
        turn.id = Number(id);
        turn.node.id = `l4-${id}`;
        turn.node.setAttribute('data-position','');
        const prior = turns.get(turn.id);
        if (prior && prior !== turn) prior.node.remove();
        turns.set(turn.id,turn);
      }
      turn.identity.textContent = mode === 'local' ? '本站原文查讀（依規則選段）' :
        turn.route === 'fallback' ? '備援模型' : '主要模型';
    }
    function showSection(turn, section) {
      if (!Object.hasOwn(kindNames,section.kind) || turn.sections.has(section.kind)) return;
      turn.sections.add(section.kind);
      const block = make(turn.answer,'section','','layer-answer-section');
      block.dataset.layerKind = section.kind;
      if (section.missing) block.dataset.layerMissing = '';
      make(block,'h4',kindNames[section.kind]);
      make(block,'p',section.missing ? '原文未交代' : section.text,'prose');
      for (const cite of citationsOf(section)) showCitation(block,cite,setFocus);
      if (section.missing) turn.missing = true;
      if (section.kind === 'evidence' && !section.missing && citationsOf(section).length) turn.hasEvidence = true;
    }
    function captureActions(turn) {
      if (!turn.id) return;
      const add = (action,label) => {
        button(turn.actions,label,async event => {
          const control = event.currentTarget;
          const type = action === 'draft' ? 'understanding' : 'missing';
          const savedCapture = () => [...captureNodes.values()].find(({element}) =>
            element.dataset.originTurn === String(turn.id) && element.dataset.captureKind === type)?.element;
          if (savedCapture()) {
            (savedCapture().querySelector('textarea, h4') || find('captures-refresh')).focus();
            return;
          }
          control.disabled = true;
          try {
            if (await saveCapture({action,turn_id:String(turn.id)})) {
              (savedCapture()?.querySelector('textarea, h4') || find('captures-refresh')).focus();
            }
          } finally {control.disabled = false;}
        });
      };
      if (turn.missing) add('missing','轉存待查');
      if (turn.hasEvidence) add('draft','留下這份理解');
    }
    const errorMessages = {
      first_token_timeout:'模型在 15 秒內未開始回覆。',
      total_timeout:'模型在 45 秒內未完成回覆。',
      invalid_citation:'引文未通過這個版本的核對。',
      source_not_public:'此版本缺少可供模型使用的公開原文。',
      invalid_selection:'所選文字與這個版本無法對應，請重新選取。',
      interrupted:'上次的串流已中斷。',
      running:'這筆提問仍在處理。',
    };
    function retryActions(turn, snapshot, error, replay = false) {
      if (replay || error.retryable) {
        button(turn.actions,replay ? '核對這次回覆' : '重試這題',() =>
          run({...snapshot,request_id:replay ? snapshot.request_id : requestID()},turn));
      }
      if (error.fallback_available && canFallback) {
        button(turn.actions,'切換備援模型',() =>
          run({...snapshot,route:'fallback',request_id:requestID()}));
      }
    }
    async function run(request, previousTurn) {
      if (active) return;
      const {preserveDraft,...snapshot} = request;
      if (!preserveDraft) question.dispatchEvent(new Event('input',{bubbles:true}));
      previousTurn?.actions.replaceChildren();
      const turn = makeTurn(snapshot.question,snapshot.route);
      const controller = new AbortController();
      active = controller;
      openFollowup(false);
      send.disabled = true;
      guide?.setAttribute('data-followup-pending','');
      cancel.hidden = false;
      log.scrollTop = log.scrollHeight;
      say(status,'正在查找同版原文…');
      say(turn.turnStatus,'等待回覆…');
      let terminal;
      try {
        const response = await fetch(endpoint('followup'),{...config,method:'POST',signal:controller.signal,
          headers:{Accept:'text/event-stream'},body:new URLSearchParams({csrf,version_id:version,...snapshot})});
        if (!response.ok || !response.headers.get('content-type')?.startsWith('text/event-stream')) {
          if (response.status >= 400 && response.status < 500) {
            terminal = true;
            const message = '提問未送出。請確認仍已登入，或重新開啟此版本。';
            say(turn.turnStatus,message,true);
            say(status,message,true);
            return;
          }
          throw new Error();
        }
        await readEvents(response.body,(kind,data) => {
          if (terminal) return;
          if (kind === 'meta') {
            identify(turn,data.turn_id ?? data.id,data.mode);
            if (previousTurn && previousTurn !== turn && turn.id === previousTurn.id) previousTurn.node.remove();
          } else if (kind === 'section') {
            showSection(turn,data);
            log.scrollTop = log.scrollHeight;
            say(status,'正在接收回覆…');
            say(turn.turnStatus,'已收到部分回覆。');
          } else if (kind === 'done') {
            terminal = true;
            say(turn.turnStatus,'回覆已保存。');
            say(status,'回覆已保存。');
            captureActions(turn);
            if (!preserveDraft && question.value.trim() === snapshot.question) {
              question.value = '';
              question.dispatchEvent(new Event('input',{bubbles:true}));
              if (focus?.anchor === snapshot.anchor) setFocus(null);
            }
          } else if (kind === 'error') {
            terminal = true;
            const message = data.message || '這次回覆未完成。';
            say(turn.turnStatus,message,true);
            say(status,message,true);
            retryActions(turn,request,data,data.reason === 'running');
          }
        });
        if (!terminal) throw new Error();
      } catch (_) {
        if (!terminal) {
          const message = controller.signal.aborted ? '已停止接收，可核對這次回覆是否已保存。' :
            '連線中斷，可先核對這次回覆是否已保存。';
          say(turn.turnStatus,message,true);
          say(status,message,true);
          retryActions(turn,request,{},true);
        }
      } finally {
        active = null;
        send.disabled = false;
        guide?.removeAttribute('data-followup-pending');
        cancel.hidden = true;
      }
    }
    form.addEventListener('submit',event => {
      event.preventDefault();
      const prompt = question.value.trim();
      if (!prompt) {question.focus(); return;}
      run({question:prompt,anchor:focus?.anchor || '',quote:focus?.quote || '',route:'primary',request_id:requestID()});
    });
    cancel.addEventListener('click',() => active?.abort());

    function restoreTurn(record) {
      if (String(record.version_id ?? version) !== version) return;
      const turn = makeTurn(record.question,record.route);
      identify(turn,record.id,record.mode);
      for (const section of record.sections || []) showSection(turn,section);
      if (record.state === 'completed') return captureActions(turn);
      say(turn.turnStatus,errorMessages[record.reason] || (record.state === 'running' ?
        '這筆提問仍在處理。' : '上次的回覆未完成。'),true);
      const unsafeRetry = ['invalid_request','request_conflict','not_found','invalid_config','route_unavailable','source_not_public','invalid_selection'];
      retryActions(turn,{question:record.question,anchor:record.anchor || '',quote:record.quote || '',
        route:record.route,request_id:record.request_id},{
        retryable:!unsafeRetry.includes(record.reason),
        fallback_available:record.route === 'primary' && record.state === 'failed' && !['source_not_public','invalid_selection'].includes(record.reason),
      },record.state === 'running');
    }

    const recall = find('recall');
    const recallForm = find('recall-form');
    let recallSequence = 0, loadedRecall = false;
    async function loadRecall() {
      const current = ++recallSequence;
      const result = find('recall-results');
      const state = find('recall-status');
      say(state,'正在查找…');
      try {
        const data = await jsonRequest(versioned('recall',{q:recallForm.elements.q.value.trim()}));
        if (current !== recallSequence) return;
        if (!Array.isArray(data.results)) throw new Error();
        result.replaceChildren();
        for (const row of data.results) {
          const hit = make(result,'li','','layer-recall-hit');
          const title = safeHref(row.href,base,location.origin);
          if (title) link(hit,row.title || '回到來源',row.href);
          else make(hit,'strong',row.title || '既有紀錄');
          if (row.text) make(hit,'p',row.text,'prose');
        }
        loadedRecall = true;
        say(state,data.results.length ? '' : '目前沒有相符的筆記或問題。');
      } catch (_) {
        if (current === recallSequence) say(state,'暫時無法查找，請再試一次。',true);
      }
    }
    recall.addEventListener('toggle',() => {if (recall.open && !loadedRecall) loadRecall();});
    recallForm.addEventListener('submit',event => {event.preventDefault(); loadRecall();});

    try {
      const restored = JSON.parse(find('initial').textContent);
      for (const record of restored.history) restoreTurn(record);
      for (const record of [...restored.captures].reverse()) renderCapture(record);
    } catch (_) {say(status,'此版本的既有紀錄暫時無法顯示。',true);}
    const bookmark = document.getElementById(currentAnchor() || reader.dataset.anchor || '');
    if (bookmark && /^(?:l4|capture)-\d+$/.test(bookmark.id)
        && reader.dispatchEvent(new Event('kh-conversation-ready',{cancelable:true}))) {
      openFollowup(false);
      requestAnimationFrame(() => bookmark.scrollIntoView({block:'start'}));
    }
    return {
      focus:setFocus,
      ask(prompt, cite, route) {
        if (active) {openFollowup(false); return false;}
        run({question:prompt,anchor:cite.anchor,quote:cite.quote,request_id:requestID(),
          route:route === 'fallback' && canFallback ? 'fallback' : 'primary',preserveDraft:true});
        return true;
      },
      review(prompt, message) {
        const review = make(root,'form','','notice');
        review.dataset.appFollowupReview = '';
        form.before(review);
        const notice = make(review,'p',message,'meta');
        notice.setAttribute('role','status');
        const label = make(review,'label','從應用程式帶來的問題');
        const field = make(label,'textarea');
        field.required = true;
        field.maxLength = question.maxLength;
        field.rows = question.rows;
        field.value = prompt;
        const actions = make(review,'div','','layer-actions');
        make(actions,'button','送出這個問題','button').type = 'submit';
        button(actions,'捨棄帶來的問題',() => {
          review.remove();
          question.focus();
        },'text-action');
        review.addEventListener('submit',event => {
          event.preventDefault();
          const edited = field.value.trim();
          if (!edited) {field.focus(); return;}
          if (active) {say(notice,'請等目前的追問結束，再送出這個問題。'); return;}
          review.remove();
          run({question:edited,anchor:'',quote:'',route:'primary',request_id:requestID(),preserveDraft:true});
        });
        openFollowup(false);
      },
    };
  }
})();
