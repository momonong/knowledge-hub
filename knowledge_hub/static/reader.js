'use strict';
(() => {
  const basePath = document.querySelector('meta[name="kh-base-path"]')?.content || '';
  const appUrl = path => basePath + path;
  const storageLabel = basePath ? '私人知識庫' : '本機';
  const showSavedResult = value => {
    const target = new URL(value, location.href);
    // Hash-only navigation leaves the old pending answer or saved form visible.
    if (target.origin === location.origin && target.pathname === location.pathname && target.search === location.search) {
      history.replaceState(null, '', target.href);
      location.reload();
    } else {
      location.assign(target.href);
    }
  };
  const reader = document.querySelector('[data-reader]');
  const csrf = document.querySelector('#page-csrf')?.value;
  function panelLabel(panel) {
    const number = panel.caption.match(/^(?:Figure|Fig\.|Table)\s+(\d+[a-z]?)/i)?.[1];
    return number ? (panel.kind === 'table' ? '表 ' : '圖 ') + number : '';
  }

  function findGuideEntries(text, panels) {
    const normalize = value => value.normalize('NFKC').toLowerCase().replace(/\s+/g, '');
    const query = normalize(text.split('\n\n').at(-1) || text);
    const candidates = [];
    for (const panel of panels) {
      if (!panel.guide) continue;
      const label = normalize(panelLabel(panel));
      const mentionsPanel = label && query.includes(label);
      for (const entry of [panel.guide, ...(panel.guide.topics || [])]) {
        const exact = query.includes(normalize(entry.question));
        const hits = entry.keywords.map(normalize).filter(word => query.includes(word));
        const score = exact ? 1000 : hits.reduce((n, word) => n + Math.min(6, word.length), 0)
          + (mentionsPanel && entry === panel.guide ? 3 : 0);
        if (score) candidates.push({panel, entry, score, exact});
      }
    }
    candidates.sort((a,b) => b.score - a.score);
    if (candidates[0]?.exact) return candidates.slice(0, 1);
    return candidates.filter(row => row.score >= candidates[0].score - 1).slice(0, 3);
  }

  async function loadVisualEvidence(section, endpoint, ask) {
    const status = section.querySelector('[data-visual-status]');
    const cards = section.querySelector('[data-visual-cards]');
    const source = section.dataset.visualEvidence;
    const add = (parent, tag, text = '') => {
      const child = document.createElement(tag);
      child.textContent = text;
      parent.append(child);
      return child;
    };
    const link = (parent, text, anchor) => {
      const a = add(parent, 'a', text);
      a.href = source + (anchor ? '#' + encodeURIComponent(anchor) : '');
      a.rel = 'noopener noreferrer';
      return a;
    };
    try {
      const response = await fetch(endpoint(''), {credentials:'same-origin', cache:'no-store'});
      if (response.status === 401) {
        status.textContent = '登入已逾時。重新整理並登入後，可繼續看同一篇的圖表。';
        return;
      }
      if (!response.ok) throw new Error('visual request failed');
      const result = await response.json();
      if (result.state === 'unavailable') {
        status.textContent = '這一版的圖表暫時無法擷取。你可以先讀正文或開啟同版 PDF。';
        return;
      }
      if (result.source !== source || !Array.isArray(result.panels)) throw new Error('Unexpected edition');
      const panels = result.panels;
      if (!panels.length) {
        status.textContent = '這個 HTML 版本沒有可辨識的圖表區塊，可對照同版 PDF。';
        return;
      }
      const tables = panels.filter(panel => panel.kind === 'table').length;
      const isPdfPage = panel => panel.representation === 'pdf_page' && Number.isInteger(panel.page_number) && panel.page_number > 0;
      const captionOnly = panels.filter(panel => !panel.images.length && !panel.rows.length && !isPdfPage(panel)).length;
      status.textContent = `找到 ${panels.length - tables} 張圖、${tables} 張表。選一項開始讀。` +
        (captionOnly ? ` 其中 ${captionOnly} 項可讀圖說，內容請對照原文。` : '') +
        (panels.some(isPdfPage) ? ' 由 PDF 圖說定位，展開後可看整頁版面。' : '') +
        (result.truncated ? '已達擷取上限，其餘請看原文。' : '');
      let figuresSeen = 0, tablesSeen = 0;
      panels.forEach((panel, index) => {
        const isTable = panel.kind === 'table';
        const label = panelLabel(panel)
          || (isTable ? '表 ' + (tablesSeen + 1) : '圖 ' + (figuresSeen + 1));
        if (isTable) tablesSeen += 1; else figuresSeen += 1;
        const card = add(cards, 'details');
        card.className = 'visual-card';
        if (panel.anchor) {
          card.id = 'kh-visual-' + panel.anchor;
          card.dataset.position = '';
          card.tabIndex = -1;
        }
        add(card, 'summary', panel.guide ? label + '　' + panel.guide.title : label);
        const body = add(card, 'div');
        body.className = 'visual-card-body';
        const native = panel.pdf_origin?.anchor;
        if (native && native !== panel.anchor && !document.getElementById('kh-visual-' + native)) {
          const alias = add(body,'span');
          alias.id = 'kh-visual-' + native;
          alias.setAttribute('aria-hidden','true');
        }
        if (panel.images.length || isPdfPage(panel)) {
          // Load only after expansion, so unopened figures do not slow reading.
          const media = add(body, 'div');
          const showImages = () => {
            if (!card.open) return;
            card.removeEventListener('toggle', showImages);
            const urls = isPdfPage(panel) ? [endpoint('/pdf/' + panel.page_number)]
              : panel.images.map((_, assetIndex) => endpoint(`/${index}/${assetIndex}`));
            urls.forEach((url, assetIndex) => {
              const img = add(media, 'img');
              img.alt = label + ' ' + (panel.guide?.title || panel.caption) + (panel.images.length > 1 ? `（${assetIndex + 1}）` : '');
              if (isPdfPage(panel)) img.alt = `論文第 ${panel.page_number} 頁原貌，包含 ${label}`;
              img.decoding = 'async';
              img.addEventListener('error', () => {
                img.remove();
                if (isPdfPage(panel)) add(media,'p','原頁預覽暫時無法載入，可用下方連結開啟出版者 PDF。');
                else link(media, '原圖暫時無法載入，回到出版者圖表 ↗', panel.anchor);
              }, {once:true});
              img.src = url;
            });
          };
          card.addEventListener('toggle', showImages);
        }
        if (panel.rows.length) {
          const scroll = add(body, 'div');
          scroll.className = 'visual-table-scroll'; scroll.tabIndex = 0;
          scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', label + '，可左右捲動');
          const table = add(scroll, 'table');
          const rows = add(table, 'tbody');
          panel.rows.forEach(row => {
            const tr = add(rows, 'tr');
            row.forEach(cell => {
              const td = add(tr, cell.header ? 'th' : 'td');
              td.colSpan = cell.colspan; td.rowSpan = cell.rowspan;
              const content = cell.underline ? add(td, 'u') : td;
              if (cell.bold) add(content, 'strong', cell.text);
              else content.textContent = cell.text;
            });
          });
        }
        if (panel.caption) add(body, 'p', panel.caption).className = 'visual-caption';
        if (!panel.images.length && !panel.rows.length && !isPdfPage(panel)) add(body, 'p', '這一項只取得圖說，圖或表請回原文核對。');
        if (panel.truncated) add(body, 'p', '這一項超過擷取上限，完整內容請看原文。');
        add(body, 'h3', '這樣讀');
        const steps = add(body, 'ol');
        panel.steps.forEach(step => add(steps, 'li', step));
        if (panel.references.length) {
          const refs = add(body, 'details');
          add(refs, 'summary', '看作者在正文怎麼解釋');
          panel.references.forEach(ref => {
            const quote = add(refs, 'blockquote');
            add(quote, 'p', ref.text);
            link(quote, '回到引用段落 ↗', ref.anchor);
          });
        }
        const actions = add(body, 'div'); actions.className = 'visual-actions';
        let localAnchor = [...panel.references.map(ref => ref.anchor), panel.guide?.section_anchor,
          isPdfPage(panel) && 'page-' + panel.page_number]
          .find(id => id && document.getElementById(id)?.closest('#panel-original'));
        const excerpts = reader.querySelector('[data-visual-excerpts]');
        if (!localAnchor && panel.references.length && excerpts) {
          const excerpt = add(excerpts, 'section');
          localAnchor = excerpt.id = (panel.guide ? 'hive-citation-' : 'visual-citation-') + (panel.anchor || index);
          add(excerpt, 'h3', label + ' · 作者的解釋');
          for (const ref of panel.references) {
            add(excerpt, 'blockquote', ref.text);
            link(excerpt, '在出版者原文看這一段 ↗', ref.anchor);
          }
        }
        if (localAnchor) {
          const original = add(actions, 'a', '在這頁對照正文');
          original.href = '#' + encodeURIComponent(localAnchor);
        }
        if (isPdfPage(panel)) {
          const original = add(actions,'a',`開啟原版 PDF 第 ${panel.page_number} 頁 ↗`);
          original.href = source.replace('/html/', '/pdf/') + '#page=' + panel.page_number;
          original.rel = 'noopener noreferrer';
        } else link(actions, '在出版者原文看此圖表 ↗', panel.anchor);
        if (panel.guide) add(body, 'p', panel.guide.question).className = 'visual-selfcheck';
        const question = add(body, 'button', panel.guide ? '追問這個問題' : '用這項圖表提問');
        question.type = 'button'; question.className = 'text-action';
        question.addEventListener('click', () => {
          const caption = panel.caption?.trim().slice(0, 220);
          const subject = caption ? `${label}（${caption}）` : label;
          ask(panel.guide?.question || `關於${subject}，作者在這裡展示了什麼？正文有哪一段可以核對？`);
        });
      });
      cards.querySelector('details')?.setAttribute('open', '');
      return panels;
    } catch (_) {
      status.textContent = '圖表暫時無法載入，正文仍可閱讀。也可以開啟同版 PDF。';
    }
  }

  let flush = async () => true;
  let submitting = false;
  let cancelDraftTimer = () => {};
  const daily = document.querySelector('[data-daily]');
  daily?.querySelectorAll('[data-open-edition]').forEach(link => link.addEventListener('click', event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const form = document.getElementById(link.dataset.openEdition);
    if (form) {event.preventDefault(); form.requestSubmit();}
  }));
  if (reader) {
    const status = document.querySelector('#draft-status');
    const roomSave = reader.querySelector('[data-room-save-status]');
    const draftFields = [...reader.querySelectorAll('[data-draft]')];
    let revision = Number(reader.dataset.revision);
    let tab = reader.dataset.tab || 'lesson';
    let anchor = reader.dataset.anchor || '';
    let restoringPosition = Boolean(anchor);
    const room = 'readingRoom' in reader.dataset;
    const desktop = room && window.matchMedia('(min-width: 940px)');
    const notes = room && reader.querySelector('[data-room-notes]');
    const drawer = room && reader.querySelector('[data-room-drawer]');
    const question = reader.querySelector('#followup');
    const evidence = reader.querySelector('[data-visual-evidence]');
    const asks = room ? [...reader.querySelectorAll('[data-room-ask]')] : [];
    let workspaceTab = tab === 'original' ? 'original' : 'lesson';
    let drawerReturn, highlighted, highlightedRow, pendingVisual;
    let visualsLoading = Boolean(evidence);
    const setDrawer = (open, focusQuestion = false, restoreFocus = true) => {
      if (!drawer) return;
      if (open && drawer.hidden) drawerReturn = document.activeElement;
      drawer.hidden = !open;
      asks.forEach(button => button.setAttribute('aria-expanded', String(open)));
      if (open && focusQuestion) question?.focus();
      if (!open && restoreFocus) {
        const origin = drawerReturn?.isConnected && !drawerReturn.closest('[hidden]') ? drawerReturn : asks[0];
        origin?.focus({preventScroll:true});
      }
    };
    const userScroll = () => {restoringPosition = false;};
    window.addEventListener('wheel',userScroll,{passive:true});
    window.addEventListener('touchmove',userScroll,{passive:true});
    window.addEventListener('pointerdown',userScroll,{passive:true});
    window.addEventListener('keydown',event => {
      if (['PageUp','PageDown','ArrowUp','ArrowDown','Home','End',' '].includes(event.key)) userScroll();
    });
    let dirty = false, saving = null, conflicted = false, timer;
    let selectedText = '';
    const endpoint = appUrl(`/items/${reader.dataset.item}/reading`);
    const values = () => ({tab, anchor, ...Object.fromEntries(draftFields.map(el => [el.dataset.draft, el.value]))});
    const say = (message, error = false) => {
      status.textContent = message;
      status.toggleAttribute('data-error', error);
      if (roomSave) roomSave.textContent = error ? message : '';
    };
    const schedule = () => {
      if (submitting) return;
      dirty = true;
      say('尚未保存…');
      clearTimeout(timer);
      if (!conflicted) timer = setTimeout(() => flush(), 700);
    };
    cancelDraftTimer = () => clearTimeout(timer);
    flush = async (leaving = false) => {
      clearTimeout(timer);
      if (conflicted) return false;
      if (saving) { await saving; return dirty ? flush() : !conflicted; }
      if (!dirty) return true;
      const snapshot = values();
      dirty = false;
      say(`正在保存到${storageLabel}…`);
      saving = (async () => {
        try {
          const body = new URLSearchParams({csrf,version_id:reader.dataset.version,revision:String(revision),...snapshot});
          const response = await fetch(endpoint, {
            method:'POST', credentials:'same-origin', cache:'no-store',
            keepalive:leaving && body.toString().length <= 60000, body
          });
          const result = await response.json();
          if (!response.ok) {
            if (response.status === 409) conflicted = true;
            throw new Error(result.error || '未能保存，輸入仍留在畫面。');
          }
          revision = result.revision;
          say(dirty ? '還有新變更待保存…' : `已保存到${storageLabel} · 下次可接續`);
          return true;
        } catch (error) {
          dirty = true;
          say(conflicted ? '另一分頁已有新內容。請先複製這份草稿，再重新載入。' : '尚未保存；請保留此頁，連線恢復後再輸入或離開前重試。', true);
          return false;
        }
      })();
      const ok = await saving;
      saving = null;
      return ok && dirty ? flush() : ok;
    };
    function showTab(value, remember = true) {
      tab = ['lesson','original','notes'].includes(value) ? value : 'lesson';
      if (tab !== 'notes') workspaceTab = tab;
      if (room && tab === 'notes') notes.open = true;
      if (room && tab === 'lesson' && reader.hasAttribute('data-room-focused')) {
        reader.querySelector('[data-room-focus]').click();
      }
      if (tab !== 'lesson') reader.querySelectorAll('video[data-film]').forEach(video => video.pause());
      reader.querySelectorAll('[data-reader-panel]').forEach(el => {
        if (!room) {el.hidden = el.dataset.readerPanel !== tab; return;}
        if (el.dataset.readerPanel === 'notes') return;
        el.hidden = !desktop.matches && el.dataset.readerPanel !== workspaceTab;
        if (desktop.matches) {
          el.removeAttribute('role');
          el.removeAttribute('aria-labelledby');
        } else {
          el.setAttribute('role', 'tabpanel');
          el.setAttribute('aria-labelledby', el.dataset.readerPanel === 'original' ? 'room-source-tab' : 'room-guide-tab');
        }
      });
      reader.querySelectorAll('[data-tab-button]').forEach(el => {
        const active = el.dataset.tabButton === (room ? workspaceTab : tab);
        el.setAttribute('aria-selected', String(active));
        el.tabIndex = active ? 0 : -1;
      });
      if (remember) {pendingVisual = null; anchor = ''; schedule();}
    }
    function markEvidence(target, quote) {
      highlighted?.classList.remove('room-highlight');
      highlightedRow?.classList.remove('room-row-highlight');
      highlightedRow = null;
      highlighted = target;
      target.classList.add('room-highlight');
      // Mark an exact span without editing the source DOM or changing the user's selection.
      globalThis.CSS?.highlights?.delete('room-evidence');
      if (typeof quote !== 'string' || !quote) return;
      for (const row of target.querySelectorAll('tr')) {
        const cells = [...row.cells].map(cell => cell.textContent.trim());
        if (cells.join(' | ').includes(quote)) {
          row.classList.add('room-row-highlight');
          highlightedRow = row;
          if (quote.includes(' | ')) return;
          break;
        }
      }
      if (!globalThis.CSS?.highlights || !globalThis.Highlight) return;
      for (const container of target.querySelectorAll('.prose, .visual-caption, tr, blockquote')) {
        const offset = container.textContent.indexOf(quote);
        if (offset < 0) continue;
        const nodes = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        const finish = offset + quote.length;
        const range = document.createRange();
        let length = 0, startFound = false, node;
        while ((node = nodes.nextNode())) {
          const next = length + node.length;
          if (!startFound && offset < next) {range.setStart(node, offset - length); startFound = true;}
          if (finish <= next) {
            range.setEnd(node, finish - length);
            CSS.highlights.set('room-evidence', new Highlight(range));
            return;
          }
          length = next;
        }
      }
    }
    function landOn(target, panel, focus) {
      requestAnimationFrame(() => {
        if (focus) target.focus({preventScroll:true});
        if (room && desktop.matches && panel && panel.dataset.readerPanel !== 'notes') {
          const bounds = panel.getBoundingClientRect();
          if (focus && (bounds.top < 0 || bounds.top >= innerHeight)) panel.scrollIntoView({block:'start'});
          panel.scrollTop += target.getBoundingClientRect().top - panel.getBoundingClientRect().top - 14;
        } else target.scrollIntoView({block:'start'});
      });
    }
    const visualAnchor = /^(?:kh-visual-|hive-citation-|visual-citation-)/;
    function revealAnchor(id, {quote = '', focus = false, remember = true} = {}) {
      if (restoreVideo(id)) return true;
      const located = id ? document.getElementById(id) : null;
      if (!located || !reader.contains(located)) {
        if (room && visualsLoading && visualAnchor.test(id)) {
          showTab('original', false);
          if (focus) setDrawer(false, false, false);
          pendingVisual = {id, quote, remember, focus};
          restoringPosition = true;
          return true;
        }
        return false;
      }
      const target = (room && located.closest('.visual-card')) || located;
      const panel = target.closest('[data-reader-panel]');
      if (panel) showTab(panel.dataset.readerPanel, false);
      if (drawer?.contains(target)) setDrawer(true);
      const original = room && panel?.dataset.readerPanel === 'original';
      if (original && focus) setDrawer(false, false, false);
      for (let p = target; p && p !== reader; p = p.parentElement) if (p.tagName === 'DETAILS') p.open = true;
      if (original) markEvidence(target, quote);
      if (target.hasAttribute('data-position') && target.id) {
        restoringPosition = true;
        if (remember && target.id !== anchor) {anchor = target.id; schedule();}
      }
      landOn(target, panel, focus);
      return true;
    }
    function revealHash() {
      let id;
      try {id = decodeURIComponent(location.hash.slice(1));} catch (_) {return false;}
      pendingVisual = null;
      return revealAnchor(id);
    }
    function restoreVideo(bookmark) {
      const match = /^hive-(guided|original)-t(\d{1,3})$/.exec(bookmark);
      const video = match && reader.querySelector('video[data-slug="' + match[1] + '"]');
      if (!video) return false;
      showTab('lesson', false);
      const film = video.closest('details');
      film.open = true;
      const seek = () => {video.currentTime = Math.min(Number(match[2]), video.duration);};
      if (video.readyState >= 1) seek();
      else video.addEventListener('loadedmetadata', seek, {once:true});
      requestAnimationFrame(() => film.scrollIntoView({block:'start'}));
      return true;
    }
    if (room) {
      desktop.addEventListener('change', () => showTab(tab, false));
      const focusSource = reader.querySelector('[data-room-focus]');
      focusSource?.addEventListener('click', () => {
        const active = reader.toggleAttribute('data-room-focused');
        focusSource.setAttribute('aria-pressed', String(active));
        focusSource.textContent = active ? '回到並排閱讀' : '專心讀原文';
      });
      asks.forEach(button => button.addEventListener('click', () => setDrawer(true, true)));
      drawer.querySelector('[data-room-close]').addEventListener('click', () => setDrawer(false));
      reader.addEventListener('kh-open-followup', event => setDrawer(true, Boolean(event.detail?.focusQuestion)));
      reader.addEventListener('kh-conversation-ready', event => {
        let id = anchor;
        try {if (location.hash) id = decodeURIComponent(location.hash.slice(1));} catch (_) {return;}
        if (/^(?:l4|capture)-[1-9]\d*$/.test(id) && revealAnchor(id)) event.preventDefault();
      });
      window.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !event.defaultPrevented) {
          if (!drawer.hidden) setDrawer(false);
          else if (reader.hasAttribute('data-room-focused')) focusSource.click();
        }
      });
      notes.querySelector('summary').addEventListener('click', () => {
        if (!notes.open) {
          tab = 'notes';
          anchor = 'study-save';
          schedule();
        } else if (tab === 'notes') showTab(workspaceTab);
      });
      const localTarget = href => {
        try {
          const url = new URL(href, location.href);
          if (url.origin !== location.origin || url.pathname !== location.pathname) return '';
          const versions = url.searchParams.getAll('version');
          if (versions.length && (versions.length !== 1 || versions[0] !== reader.dataset.version)) return '';
          if (url.search !== location.search && [...url.searchParams.keys()].some(key => key !== 'version')) return '';
          return decodeURIComponent(url.hash.slice(1));
        } catch (_) {return '';}
      };
      const openTarget = (id, quote = '') => {
        if (!id) return false;
        pendingVisual = null;
        if (!revealAnchor(id, {quote, focus:true})) return false;
        const url = new URL(location.href);
        url.searchParams.set('version', reader.dataset.version);
        url.hash = encodeURIComponent(id);
        if (location.href !== url.href) history.pushState(null, '', url.href);
        return true;
      };
      reader.addEventListener('click', event => {
        const link = event.target.closest('a[href]');
        if (!link || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey ||
            event.button !== 0 || link.target || link.hasAttribute('download')) return;
        if (openTarget(localTarget(link.href))) event.preventDefault();
      });
      reader.addEventListener('kh-reveal-evidence', event => {
        const {anchor:id, quote, href} = event.detail || {};
        if (typeof id === 'string' && localTarget(href) === id && openTarget(id, quote)) event.preventDefault();
      });
    }
    showTab(tab, false);
    if (!revealHash() && anchor) revealAnchor(anchor, {remember:false});
    window.addEventListener('hashchange', revealHash);
    reader.querySelectorAll('[data-tab-button]').forEach(button => {
      button.addEventListener('click', () => showTab(button.dataset.tabButton));
      button.addEventListener('keydown', event => {
        if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
        event.preventDefault();
        const all = [...reader.querySelectorAll('[data-tab-button]')];
        const next = event.key === 'Home' ? all[0] : event.key === 'End' ? all.at(-1) :
          all[(all.indexOf(button) + (event.key === 'ArrowRight' ? 1 : all.length-1)) % all.length];
        showTab(next.dataset.tabButton); next.focus();
      });
    });
    draftFields.forEach(field => field.addEventListener('input', schedule));
    reader.querySelectorAll('[data-open-notes]').forEach(button => button.addEventListener('click', () => showTab('notes')));
    reader.querySelector('[data-use-summary]')?.addEventListener('click', () => {
      const field = reader.querySelector('[data-draft="reflection"]');
      const suggestion = reader.querySelector('#summary-suggestion').textContent;
      const joined = [field.value.trim(),suggestion].filter(Boolean).join('\n\n');
      if (joined.length > 8000) {say('加入後超過 8000 字；請先精簡草稿，原內容仍保留。', true);return;}
      field.value = joined; field.focus(); schedule();
    });
    const addQuestion = prompt => {
      setDrawer(true);
      const draft = [question.value.trim(), prompt].filter(Boolean).join('\n\n');
      if (draft.length > 2000) {say('問題已接近字數上限，請先精簡。',true);return;}
      question.value = draft; question.focus(); schedule();
    };
    let guidePanels = [];
    if (evidence) {
      const endpoint = suffix => appUrl(`/items/${reader.dataset.item}/visuals${suffix}?version=${encodeURIComponent(reader.dataset.version)}`);
      loadVisualEvidence(evidence, endpoint, addQuestion).then(panels => {
        visualsLoading = false;
        guidePanels = panels || [];
        if (pendingVisual) {
          const {id, ...options} = pendingVisual;
          pendingVisual = null;
          if (!revealAnchor(id, options) && guidePanels.length) {
            const status = evidence.querySelector('[data-visual-status]');
            status.textContent += ' 找不到連結指定的圖表，可對照同版 PDF。';
          }
        } else if (!room && visualAnchor.test(location.hash.slice(1))) revealHash();
        if (guidePanels.length) {
          reader.dataset.visualsReady = 'true';
          reader.dispatchEvent(new Event('kh-visuals-ready'));
        }
      });
    }
    reader.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => addQuestion(button.dataset.question)));
    reader.querySelectorAll('[data-follow-source]').forEach(button => button.addEventListener('click', () => {
      const form = reader.querySelector('[data-question-form]');
      if (!form) {
        const previous = button.closest('article')?.querySelector('.chat-question')?.textContent;
        if (previous) addQuestion(previous);
        return;
      }
      form.elements.local_parent.value = button.dataset.followSource;
      form.querySelector('[data-source-parent]').textContent = '目前接續這一題：' + button.closest('article').querySelector('.chat-question').textContent;
      setDrawer(true);
      question.focus();
    }));
    reader.querySelectorAll('[data-copy-source]').forEach(button => button.addEventListener('click', () => {
      const field = reader.querySelector('[data-draft="' + button.dataset.copySource + '"]');
      const note = button.closest('article').querySelector('[data-source-note]').textContent;
      const combined = [field.value.trim(),note].filter(Boolean).join('\n\n');
      if (combined.length > field.maxLength) {say('超過草稿字數上限，請先精簡你的紀錄。',true);return;}
      field.value = combined; showTab('notes'); field.focus(); schedule(); button.disabled = true;
    }));
    reader.querySelector('[data-guide-search]')?.addEventListener('click', () => {
      const result = reader.querySelector('[data-guide-result]');
      result.replaceChildren();
      const prompt = question.value.trim();
      if (!prompt) {question.reportValidity(); return;}
      const matches = findGuideEntries(prompt, guidePanels);
      if (!matches.length) {
        result.textContent = guidePanels.length ? '這個問題還沒有對應的導讀。問題會留在這篇的草稿，下次可接著查。' : '圖表尚未載入，請看圖表區的讀取狀態。';
        return;
      }
      for (const {panel, entry} of matches) {
        const item = document.createElement('section');
        item.className = 'guide-response';
        const heading = document.createElement('h3');
        heading.textContent = entry.question;
        const answer = document.createElement('p');
        answer.textContent = entry.answer;
        const link = document.createElement('a');
        const label = panelLabel(panel);
        link.href = '#kh-visual-' + panel.anchor; link.textContent = '對照' + label;
        const keep = document.createElement('button');
        keep.type = 'button'; keep.className = 'text-action'; keep.textContent = '加入我的理解';
        keep.addEventListener('click', () => {
          const field = reader.querySelector('[data-draft="reflection"]');
          const note = [prompt, entry.question === prompt ? '' : entry.question, entry.answer, '依據：HiVe-Mem v1 ' + label].filter(Boolean).join('\n');
          const joined = [field.value.trim(), note].filter(Boolean).join('\n\n');
          if (joined.length > field.maxLength) {say('理解草稿已達字數上限，請先整理。', true);return;}
          field.value = joined; showTab('notes'); field.focus(); schedule();
          keep.disabled = true;
        });
        item.append(heading, answer, link, keep); result.append(item);
      }
    });
    const films = [...reader.querySelectorAll('video[data-film]')];
    films.forEach(video => {
      const film = video.closest('details');
      const chapters = [...film.querySelectorAll('[data-seek]')];
      let savedAt = null;
      const rememberTime = () => {
        if (tab !== 'lesson' || !film.open || !video.currentTime) return;
        savedAt = Math.floor(video.currentTime);
        anchor = `hive-${video.dataset.slug}-t${savedAt}`; schedule();
      };
      video.addEventListener('play', () => films.forEach(other => {if (other !== video) other.pause();}));
      video.addEventListener('pause', rememberTime);
      video.addEventListener('seeked', rememberTime);
      video.addEventListener('timeupdate', () => {
        if (!video.paused && (savedAt === null || Math.abs(video.currentTime - savedAt) >= 20)) rememberTime();
      });
      film.addEventListener('toggle', () => {if (!film.open) video.pause();});
      chapters.forEach(chapter => chapter.addEventListener('click', () => {
        const seek = () => {video.currentTime = Number(chapter.dataset.seek);};
        if (video.readyState >= 1) seek();
        else {video.addEventListener('loadedmetadata', seek, {once:true}); video.load();}
      }));
      film.querySelector('[data-ask-time]').addEventListener('click', () => {
        video.pause();
        const seconds = Math.floor(video.currentTime);
        const chapter = chapters.filter(cue => Number(cue.dataset.seek) <= seconds).at(-1);
        const cue = chapter?.closest('li').querySelector('strong')?.textContent || '水平擴展和垂直組合分別解決什麼問題？';
        addQuestion(`${video.dataset.film} ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')} · ${cue}`);
      });
    });
    document.addEventListener('selectionchange', () => {
      selectedText = '';
      const selection = window.getSelection();
      if (!selection?.rangeCount) return;
      const node = selection.anchorNode?.parentElement;
      const panel = node?.closest('[data-reader-panel]');
      const range = selection.getRangeAt(0);
      const end = selection.focusNode?.parentElement?.closest('[data-reader-panel]');
      const touchesPrivate = [...reader.querySelectorAll('[data-private]')].some(el => range.intersectsNode(el));
      if (panel && panel === end && panel.dataset.readerPanel !== 'notes' && !touchesPrivate && selection.toString().trim())
        selectedText = selection.toString().slice(0,1200);
    });
    reader.querySelector('[data-quote-selection]')?.addEventListener('click', () => {
      if (!selectedText) {say('先選取教學或原文的一段文字，再帶入追問。');return;}
      const joined = [question.value.trim(),`關於這段：「${selectedText}」\n我想問：`].filter(Boolean).join('\n\n');
      if (joined.length > 2000) {say('問題超過 2000 字，請縮短選文。',true);return;}
      setDrawer(true); question.value = joined;question.focus();schedule();
    });
    let positionTimer;
    const sidebar = reader.querySelector('.conversation');
    const panes = room ? [...reader.querySelectorAll('.room-pane')] : [];
    function recordScroll(event) {
      const scroller = event.currentTarget;
      const inSidebar = scroller === sidebar;
      const inPane = panes.includes(scroller);
      clearTimeout(positionTimer);
      positionTimer = setTimeout(() => {
        if (submitting || restoringPosition) return;
        let column = inSidebar || inPane ? scroller : reader;
        if (room && column === reader && desktop.matches) {
          // The document can scroll past the private sections. Each main pane tracks its own position.
          if (notes.open && notes.getBoundingClientRect().top < innerHeight * .65) column = notes;
          else return;
        }
        const visible = [...column.querySelectorAll('[data-position]')].filter(el =>
          !el.closest('[hidden]') && (room ? el.getClientRects().length > 0 && (inSidebar || !drawer.contains(el)) :
            inSidebar || !window.matchMedia?.('(min-width:761px)').matches || !el.closest('.conversation')));
        const bounds = inSidebar || inPane ? column.getBoundingClientRect() : {top:0, bottom:innerHeight};
        const start = Math.max(bounds.top, 0), end = Math.min(bounds.bottom, innerHeight);
        const nearby = visible.filter(el => {
          const rect = el.getBoundingClientRect();
          return rect.bottom > start + 24 && rect.top < start + (end - start) * .65;
        });
        const near = nearby.find(el => el.matches('.chat-turn, .saved-understanding[open]')) || nearby[0];
        if (near?.id === 'hive-videos' && anchor.startsWith('hive-') && anchor !== 'hive-videos') return;
        if (near && near.id && near.id !== anchor) {
          const panel = room && near.closest('[data-reader-panel]');
          if (panel) tab = panel.dataset.readerPanel;
          anchor = near.id;
          schedule();
        }
      }, 350);
    }
    window.addEventListener('scroll',recordScroll,{passive:true});
    panes.forEach(pane => pane.addEventListener('scroll',recordScroll,{passive:true}));
    sidebar?.addEventListener('scroll',recordScroll,{passive:true,capture:true});
    // An initial visit is also resumable, without claiming the article was read.
    schedule();
    window.addEventListener('beforeunload', event => {
      if (dirty || saving) {event.preventDefault();event.returnValue='';}
    });
    const flushOnLeave = () => {if (dirty && !submitting) flush(true);};
    document.addEventListener('visibilitychange', () => {if (document.hidden) flushOnLeave();});
    window.addEventListener('pagehide', flushOnLeave);
    document.addEventListener('click', async event => {
      const link = event.target.closest('a');
      if (!link || event.defaultPrevented || (!dirty && !saving) || event.ctrlKey || event.metaKey || link.target === '_blank') return;
      const url = new URL(link.href,location.href);
      if (url.origin !== location.origin || (url.pathname === location.pathname && url.search === location.search && url.hash)) return;
      event.preventDefault();
      if (submitting) return;
      if (await flush()) location.assign(link.href);
    });
  }
  document.querySelectorAll('form[data-busy]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting || !form.reportValidity()) return;
    submitting = true; cancelDraftTimer();
    const drafts = [...document.querySelectorAll('[data-draft]')];
    drafts.forEach(el => el.readOnly = true);
    const unlock = () => {drafts.forEach(el => el.readOnly = false);submitting = false;};
    if (!await flush()) {unlock();return;}
    const button = event.submitter || form.querySelector('button[type="submit"]');
    if (!button) {unlock();return;}
    const oldText = button.textContent;
    const fields = new URLSearchParams(new FormData(form));
    button.disabled = true; button.textContent = '處理中…';
    const notice = document.createElement('p');notice.className = 'form-status';notice.setAttribute('role','status');notice.textContent = form.dataset.busy;
    form.append(notice);
    try {
      const action = button.hasAttribute('formaction') ? button.formAction : form.action;
      const response = await fetch(action, {method:'POST',body:fields,credentials:'same-origin',cache:'no-store',
        headers:form.matches('#study-save, [data-question-form], [data-bridge-form], [data-study-operation]') ? {Accept:'application/json'} : {}});
      if (response.ok && response.headers.get('Content-Type')?.startsWith('application/json')) {
        const result = await response.json();
        if (typeof result.location !== 'string') throw new Error('Missing saved location');
        showSavedResult(result.location);
        return;
      }
      if (response.ok && response.redirected) {
        showSavedResult(response.url);
        return;
      }
      const html = await response.text();
      const page = new DOMParser().parseFromString(html, 'text/html');
      const alert = page.querySelector('[role="alert"]') || page.querySelector('.notice-warn') || page.querySelector('.notice');
      notice.textContent = alert?.textContent || `這次請求未完成（${response.status}），輸入仍保留。`;
      notice.setAttribute('role','alert');
      button.disabled = false; button.textContent = oldText; unlock();
    } catch (_) {
      notice.textContent = '尚未確認這次請求是否完成。請重新載入查看原紀錄，先不要重送；輸入仍保留。';
      notice.setAttribute('role','alert');
      // Keep this submit disabled: a transport failure is not permission to retry.
      unlock();
    }
  }));
  // Only pending work is polled, for one minute. Never submit a replacement,
  // and never automatically query again after an UNKNOWN response.
  const pending = document.querySelector('[data-operation-state="pending"]');
  const pollForm = pending?.querySelector('form');
  if (pollForm) {
    const deadline = Date.now() + 60000;
    const poll = async () => {
      if (Date.now() >= deadline) {pending.querySelector('p').textContent='原工作仍未完成；可稍後查詢，暫不重送。';return;}
      if (submitting) {setTimeout(poll,3000);return;}
      try {
        const fields = new URLSearchParams(new FormData(pollForm));
        fields.delete('inspect_unknown');
        const response = await fetch(pollForm.action,{method:'POST',body:fields,
          credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}});
        if (!response.ok) throw new Error();
        const result = await response.json();
        if (result.state === 'pending') {setTimeout(poll,3000);return;}
        if (result.state === 'unknown') {
          pending.dataset.operationState='unknown';
          pending.querySelector('p').textContent='這次結果尚未確定，已停止自動查詢與新生成。原工作保留。';return;
        }
        if (['completed','failed'].includes(result.state) && await flush()) showSavedResult(result.location);
      } catch (_) {pending.querySelector('p').textContent='暫時無法確認原工作狀態，已停止自動查詢；請保留這個頁面。';}
    };
    setTimeout(poll,3000);
  }
})();
