(function (expectedOrigin, stylesheet) {
    if (window.top !== window || location.origin !== expectedOrigin ||
        !/^\/knowledge(?:\/|$)/.test(location.pathname)) return;

    function prepareSurface() {
        const main = document.querySelector('main.workspace') ||
            (/^\/knowledge\/library(?:\/|$)/.test(location.pathname) && document.querySelector('main.container'));
        if (!main || document.documentElement.hasAttribute('data-kh-mobile')) return;
        document.documentElement.setAttribute('data-kh-mobile', '');
        const style = document.createElement('style');
        style.textContent = stylesheet;
        document.head.append(style);

        const today = main.querySelector('[data-daily]');
        if (today) prepareList(today);
        if (main.querySelector('#library-query')) prepareLibrary(main);

        // On an untouched edition with no generated or curated lesson, open the available
        // source. Saved notes, citations, video bookmarks and existing lessons take priority.
        const article = main.querySelector('article[data-reader]:not([data-reading-room])');
        if (article) {
            const names = {lesson: '導讀', original: '原文', notes: '筆記'};
            for (const button of article.querySelectorAll('.reader-tabs [data-tab-button]')) {
                const name = names[button.dataset.tabButton];
                if (name) {
                    button.setAttribute('aria-label', button.textContent.trim());
                    button.textContent = name;
                }
            }
            const panel = article.querySelector('#panel-original');
            const heading = panel?.querySelector(':scope > h2');
            const first = panel?.querySelector(':scope > .source-paragraph');
            if (heading && first) {
                const tools = document.createElement('details');
                tools.className = 'kh-source-tools';
                const summary = document.createElement('summary');
                summary.textContent = '原文工具';
                tools.append(summary);
                const controls = panel.querySelectorAll(':scope > p, :scope > form, :scope > .source-outline');
                for (const control of controls) tools.append(control);
                heading.after(tools);
            }
        }
        if (article && !location.hash && article.dataset.revision === '0' &&
            !article.dataset.anchor && article.dataset.tab === 'lesson' &&
            article.querySelector('.lesson-start [data-study-provider="unconfigured"]') &&
            article.querySelector('#panel-original .source-paragraph .prose')) {
            article.querySelector('[data-tab-button="original"]')?.click();
        }
    }

    function prepareLibrary(main) {
        const heading = main.querySelector('.reading-shell > h1');
        const toolbar = document.querySelector('header.topbar');
        const tools = toolbar?.querySelector('.library-tools');
        const domain = main.querySelector('#library-domain');
        const filters = domain?.closest('.form-row');
        const priority = main.querySelector('input[name="starter"]')?.closest('label');
        const form = domain?.form;
        if (!heading || !tools || !form || !filters || !priority) return;
        if (heading.textContent.trim() === '初始知識庫') heading.textContent = '知識庫';
        const titleRow = document.createElement('div');
        titleRow.className = 'kh-library-heading';
        heading.before(titleRow);
        titleRow.append(heading, tools);
        toolbar.hidden = true;

        const more = document.createElement('details');
        more.className = 'kh-library-filters';
        const summary = document.createElement('summary');
        summary.textContent = '篩選條件';
        filters.before(more);
        more.append(summary, filters, priority);
        more.open = Boolean(domain.value || priority.querySelector('input:checked'));
        form.classList.add('kh-library-search');
        main.querySelector('#library-query').setAttribute('enterkeyhint', 'search');
        const submit = form.querySelector('button[type="submit"]');
        if (submit) submit.textContent = '搜尋';
    }

    function prepareList(today) {
        const mast = today.querySelector('.page-heading');
        const freshness = today.querySelector('.freshness');
        const refresh = mast?.querySelector('form');
        if (refresh && freshness) {
            const details = document.createElement('details');
            details.className = 'kh-feed-tools';
            const summary = document.createElement('summary');
            const status = freshness.querySelector('[role="status"]');
            function updateLabel() {
                const warning = /失敗|未完成|尚無/.test(status?.textContent || '');
                summary.textContent = warning ? '來源需查看' : '選讀來源';
                details.classList.toggle('kh-warning', warning);
            }
            updateLabel();
            if (status) new MutationObserver(updateLabel).observe(status, {childList:true, subtree:true, characterData:true});
            mast.append(details);
            details.append(summary, freshness, refresh);
        }
        for (const row of today.querySelectorAll('.daily-story:not(.briefing-story)')) {
            const meta = row.querySelector(':scope > .meta');
            const footer = row.querySelector('.story-footer');
            const scope = footer?.querySelector(':scope > span');
            const action = footer?.querySelector('a');
            const title = row.querySelector('h3 a');
            if (meta && scope && action && title && action.href === title.href) {
                meta.append(' · ', scope);
                footer.hidden = true;
            }
        }
        let preview;
        function dismiss() {
            if (!preview) return;
            preview.remove();
            preview = null;
        }
        window.addEventListener('pagehide', dismiss);
        window.addEventListener('pageshow', dismiss);

        today.addEventListener('click', function (event) {
            if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey ||
                event.shiftKey || event.altKey || !window.getSelection()?.isCollapsed || preview) return;
            const target = event.target;
            if (!(target instanceof Element)) return;
            const row = target.closest('.daily-story:not(.daily-story-skipped)');
            if (!row || target.closest('details, form, button, input, textarea, select, label, [contenteditable]')) return;
            const title = row.querySelector('h2 a[href], h3 a[href]');
            if (!title) return;
            const url = new URL(title.href);
            if (url.origin !== expectedOrigin || !/^\/knowledge\/items\/[1-9]\d{0,18}$/.test(url.pathname) ||
                !/^\?version=[1-9]\d{0,18}$/.test(url.search) || url.hash) return;
            const link = target.closest('a');
            if (link && (link.href !== title.href || link.hasAttribute('download') ||
                (link.target && link.target !== '_self'))) return;

            // Everything shown here is already in the authenticated list for this very edition.
            // Let WebView commit the normal document GET, including any login or error response.
            const snippet = row.querySelector('.briefing-lede, .briefing-source-preview, .story-preview');
            const label = snippet?.querySelector('strong')?.textContent.trim();
            const quote = row.querySelector('.discovery-evidence p:not(.meta), .briefing-excerpt p');
            const excerpt = snippet?.matches('.briefing-lede') ? snippet.textContent.trim() :
                (quote?.textContent.trim() || (label && snippet.textContent.trim().slice(label.length).trim()));
            const dialog = document.createElement('dialog');
            if (typeof dialog.showModal !== 'function') {
                if (!link) location.assign(title.href);
                return;
            }
            dialog.className = 'kh-open-article';
            dialog.setAttribute('aria-labelledby', 'kh-opening-title');
            function add(tag, className, text) {
                const element = document.createElement(tag);
                element.className = className;
                element.textContent = text;
                dialog.append(element);
                return element;
            }
            const heading = add('h1', 'kh-reading-title', title.textContent.trim());
            heading.id = 'kh-opening-title';
            heading.tabIndex = -1;
            heading.autofocus = true;
            if (excerpt) {
                add('p', 'kh-passage-label', label || (snippet?.matches('.briefing-lede') ? '速讀摘要' : '來源節錄'));
                add('p', 'kh-passage', excerpt);
            }
            add('p', 'kh-opening-status', '正在開啟完整文章…').setAttribute('role', 'status');
            const back = add('button', 'kh-dismiss', '返回清單');
            back.type = 'button';
            back.addEventListener('click', () => { window.stop(); dismiss(); });
            dialog.addEventListener('cancel', event => { event.preventDefault(); window.stop(); dismiss(); });
            document.body.append(dialog);
            preview = dialog;
            dialog.showModal();
            event.preventDefault();
            // Paint the chosen edition before starting the single normal document navigation.
            requestAnimationFrame(() => requestAnimationFrame(() => {
                if (preview === dialog) location.assign(title.href);
            }));
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', prepareSurface, {once: true});
    } else {
        prepareSurface();
    }
})
