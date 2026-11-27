"""One fixed edition's evidence for the reading guide and constrained Q&A."""
import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from urllib.parse import quote, urlsplit

from . import excerpts, lessons, local_study, pdf_evidence, source_reader, study_context, visuals
from .connections import reading_url
from .metric_notation import UNIT_PATTERN as _MEASURE_UNIT, contains_displayed, plain_math as _plain_math
from .sources import SAFE_ANCHOR_RE


MISSING = '原文未交代'
KINDS = ('evidence', 'limits', 'inference')
MAX_EVIDENCE = 24
MAX_QUOTE = 700
MAX_SELECTION = 3000
MAX_CATALOG_TEXT = 9600
MAX_SECTION_TEXT = 2200
_GUIDE_FORMAT = 2
_VALUE = r'[+-]?\d+(?:,\d{3})*(?:\.\d+)?'
_FACTOR = re.compile(
    rf'\bby (?:a |the )?factor of\s+({_VALUE})(?=\s*(?:$|[;:)]|[.,](?!\d)|'
    r'\b(?:compared|relative|versus|over|across|on|under|when|for|and)\b))', re.I,
)
_MEASURE = re.compile(
    rf'(?<![a-z0-9_.])({_VALUE})(?:\s*(?:--?|–|—|→|~|to|至|到)\s*({_VALUE}))?\s*({_MEASURE_UNIT})', re.I,
)
_UNIT = {'percentage point':'pp','percentage points':'pp','百分點':'pp','percent':'%',
         'times':'×','x':'×','倍':'×','milliseconds':'ms','millisecond':'ms','毫秒':'ms',
         'second':'s','seconds':'s','秒':'s','tokens':'token'}
_CJK = re.compile(r'[\u3400-\u9fff]')
_URL = re.compile(
    r'(?:\b(?:https?|ftp|file|data|javascript|mailto)\s*:|//[a-z0-9]|\bwww\.|'
    r'\b(?:[a-z0-9-]+\.)+(?:org|com|net|edu|gov|io|ai|dev|co|me|example|html)(?:\b|/)|'
    r'\]\s*\(\s*[^)]+\))', re.I,
)
_BASELINE = re.compile(
    r'\b(?:compar(?:e|es|ed|ing|ison) (?:with|to|against)|against (?:the |a )?\w+ baseline|'
    r'(?:standard|conventional|previous|prior) (?:baseline|approach|methods?)|'
    r'(?:differs?|unlike) (?:from )?(?:the |a )?\w+)\b|相較於|傳統方法|既有基準', re.I,
)
_VISUAL_PROMPT = re.compile(
    r'^\s*(?:請\s*)?(?:說明|解讀|讀|看|介紹)\s*(?:這|該|此)?(?:張)?(?:圖表|圖說|圖|表)'
    r'\s*(?:[a-z]?\d+)?\s*[。？?！!]?\s*$'
    r'|^\s*(?:這|該|此)?(?:張)?(?:圖表|圖說|圖|表)\s*(?:[a-z]?\d+)?'
    r'\s*(?:怎麼讀|如何讀|說了什麼|是什麼|呈現什麼)\s*[。？?！!]?\s*$',
    re.I,
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _measures(text):
    values = set()
    plain = unicodedata.normalize('NFKC', _plain_math(text)).casefold().replace('−', '-')
    plain = _FACTOR.sub(r'\1×', plain)
    for first, last, unit in _MEASURE.findall(plain):
        for value in (first,last):
            if value:
                values.add((Decimal(value.replace(',','')),_UNIT.get(unit,unit)))
    return values


def _anchor(value):
    return isinstance(value, str) and len(value) <= 200 and bool(SAFE_ANCHOR_RE.fullmatch(value))


def _identity(item, version):
    item_id, version_id = lessons.identifier(item['id']), str(lessons.identifier(version['id']))
    if 'item_id' in version and lessons.identifier(version['item_id']) != item_id:
        raise ValueError('找不到這篇文章的固定版本。')
    if 'versions' in item:
        matched = next((row for row in item['versions'] if str(row['id']) == version_id), None)
        identity = ('label', 'scope', 'content_hash', 'source_url', 'blocks')
        if matched is None or any(matched.get(key) != version.get(key) for key in identity):
            raise ValueError('找不到這篇文章的固定版本。')
    return item_id, version_id


def _public_location(value):
    if (not isinstance(value, str) or not value or len(value) > 2000
            or any(c.isspace() or c in '<>\\' for c in value)):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in {None, 443}):
            return None
        return parsed
    except ValueError:
        return None


def _edition_location(item, version):
    if version['scope'] == 'unavailable':
        return None
    parsed = _public_location(version.get('source_url'))
    if not parsed:
        return None
    base = parsed._replace(fragment='').geturl()
    if item['key'].startswith('arxiv:'):
        html = visuals.paper_url(item['key'], version['label'])
        pdf = pdf_evidence.pdf_source(item['key'], version['label'])
        if not html or base not in {html, pdf, html.replace('/html/', '/abs/', 1)}:
            return None
    return base


def _block_location(block, base):
    direct = block.get('source_url') or base
    parsed = _public_location(direct)
    if not parsed or parsed._replace(fragment='').geturl() != base:
        return None
    fragment = parsed.fragment
    if fragment and not (_anchor(fragment) or re.fullmatch(r'page=[1-9][0-9]{0,4}', fragment)):
        return None
    return direct


def _panels(item, version, panels, requested=''):
    """Accept parsed HTML manifests or PDF panels with verifiable page URLs."""
    html = visuals.paper_url(item['key'], version['label'])
    pdf = pdf_evidence.pdf_source(item['key'], version['label'])
    if not html or not pdf or not _edition_location(item, version):
        return {}
    manifest_source = None
    if isinstance(panels, dict):
        manifest_source = panels.get('source')
        if manifest_source != html:
            return {}
        panels = panels.get('panels', ())
    result = {}
    for panel in panels[:visuals.MAX_PANELS]:
        if not isinstance(panel, dict):
            continue
        anchor = panel.get('anchor')
        kind = panel.get('kind')
        if not _anchor(anchor) or kind not in {'figure','table'} or anchor in result:
            continue
        representation = panel.get('representation')
        origin = panel.get('pdf_origin', {}) if manifest_source == html else {}
        native = origin.get('anchor')
        page = origin.get('page_number')
        if (not _anchor(native) or type(page) is not int or not 1 <= page <= 999
                or not native.startswith(f'page-{page}-{kind}-') or origin.get('page_url') != f'{pdf}#page={page}'):
            origin = {}
        if representation == 'pdf_page':
            page = panel.get('page_number')
            if (type(page) is not int or not 1 <= page <= 999 or
                    not (anchor.startswith(f'page-{page}-{kind}-') or origin.get('page_number') == page)):
                continue
            direct = f'{pdf}#page={page}'
            if panel.get('page_url') != direct:
                continue
        elif representation in {None, 'html'}:
            source = panel.get('source_url', manifest_source)
            direct = f'{html}#{quote(anchor, safe="")}'
            if source not in {html, direct}:
                continue
        else:
            continue
        result[anchor] = (panel, direct)
        if origin and requested == native and native != anchor:
            result[native] = ({**panel,**origin,'representation':'pdf_page','rows':[]},origin['page_url'])
    return result


def _panel_rows(panel):
    for row in panel.get('rows', [])[:visuals.MAX_ROWS]:
        cells = [cell['text'].strip() for cell in row[:visuals.MAX_CELLS] if cell.get('text')]
        value = ' | '.join(cells)
        if value:
            yield value


def _panel_quotes(panel, question):
    caption = panel.get('caption', '').strip()
    if caption:
        yield caption[:MAX_QUOTE].rstrip()
    rows = [row for row in _panel_rows(panel) if len(row) <= MAX_QUOTE]
    if rows:
        words = study_context.words(question)
        rows_after_header = sorted(rows[1:], key=lambda row: -len(words & study_context.words(row)))
        yield from dict.fromkeys([rows[0], *rows_after_header[:3]])


def _bounded_selection(text):
    start = 0
    while start < len(text):
        end = min(start + MAX_QUOTE, len(text))
        if end < len(text):
            space = text.rfind(' ',start + MAX_QUOTE // 2,end)
            if space >= 0:
                end = space + 1
        part = text[start:end].strip()
        if part:
            yield part
        start = end


def _selection_evidence(anchor, selected, blocks, figures, guides, item_id, version_id, base):
    parsed = [(block['text'],reading_url(item_id,version_id,anchor),_block_location(block,base))
              for block in blocks if block['anchor'] == anchor]
    if anchor in figures:
        panel, direct = figures[anchor]
        href = reading_url(item_id,version_id,f'kh-visual-{anchor}')
        parsed.extend((text,href,direct) for text in (panel.get('caption',''), *_panel_rows(panel)) if text)
    for text, href, direct in parsed:
        if selected in text:
            return [(part,href,direct) for part in _bounded_selection(selected)]

    # A reader can select translated guide prose. Use the checked original
    # passages that generated it and keep the selected prose in the app.
    for section in guides:
        if section.get('missing') or not contains_displayed(section.get('text',''), selected):
            continue
        mapped = []
        for cite in section.get('citations',[]):
            text = cite.get('quote')
            if cite.get('anchor') != anchor or not isinstance(text,str) or not text:
                continue
            for original, href, direct in parsed:
                if cite.get('href') == href and cite.get('source_url') == direct and text in original:
                    mapped.extend((part,href,direct) for part in _bounded_selection(text))
                    break
        if mapped:
            return mapped
    raise ValueError('所選文字不屬於這個版本的指定原文或圖表。')


def _selected_spans(block, focus, expanded):
    focused = excerpts.select_spans([block],focus,focus,limit=3) if focus else []
    all_spans = excerpts.select_spans([block],expanded,set(),limit=3)
    spans = list(dict.fromkeys(text for _, text, quality, _ in [*focused,*all_spans]
                              if quality == 'sentence'))
    if spans:
        return spans[:3]
    text = block['text'].strip()
    if len(text) <= MAX_QUOTE:
        return [text]
    return [all_spans[0][1]] if all_spans else []


def _block_spans(blocks, focus, expanded):
    for index, span, quality, _ in excerpts.select_spans(blocks,expanded,focus,limit=MAX_EVIDENCE):
        if quality == 'sentence':
            yield blocks[index], span
    # Preserve the less prominent results and limits for general questions
    # and the guide. The focused candidates above retain priority.
    labels = local_study.section_labels(blocks)
    for role in ('abstract','introduction','method','results','limitations','related','other'):
        group = [block for block, (label, _) in zip(blocks,labels) if label == role]
        for index, span, quality, _ in excerpts.select_spans(group,expanded,set(),limit=4):
            if quality == 'sentence':
                yield group[index], span


def make_catalog(item, version, question='', anchor='', panels=(), *, selected_quote='', guide_sections=()):
    """Create bounded IDs and quotations from the requested edition only.

    Pass a visual manifest bound to the pinned HTML URL. Bare HTML panels
    must carry their own matching source URL. PDF panels carry page_url.
    A requested quote must match the exact parsed source at the anchor or
    previously saved guide prose with independently verified source citations.
    """
    item_id, version_id = _identity(item, version)
    if not isinstance(question,str) or len(question) > 2000:
        raise ValueError('提問超過可查讀的長度。')
    if not isinstance(anchor,str) or anchor and not _anchor(anchor):
        raise ValueError('原文或圖表錨點無效。')
    if (not isinstance(selected_quote,str) or len(selected_quote) > MAX_SELECTION
            or selected_quote and (not anchor or not selected_quote.strip())):
        raise ValueError('所選文字必須從本篇固定版本取得，且不得超過 3000 字。')
    selected_quote = selected_quote.strip()
    base = _edition_location(item, version)
    figures = _panels(item, version, panels, anchor)
    if anchor and anchor not in figures and not any(block['anchor'] == anchor for block in version['blocks']):
        raise ValueError('這個版本沒有指定的原文或圖表錨點。')
    if not base:
        if selected_quote:
            raise ValueError('這個版本還沒有可核對的所選原文。')
        return {}
    blocks = [block for block in study_context.readable_blocks(version)
              if _anchor(block['anchor']) and _block_location(block, base)]
    focus = study_context.words(question)
    expanded = study_context.words(source_reader.retrieval_query(question)) if question else set()
    catalog = {}
    remaining = MAX_CATALOG_TEXT

    def add(origin, text, href, source_url):
        nonlocal remaining
        if not text or len(text) > remaining or len(catalog) >= MAX_EVIDENCE:
            return
        key = 'E-' + _digest((item_id,version_id,version.get('content_hash'),origin,text,source_url))[:24]
        if key not in catalog:
            catalog[key] = {'anchor':origin,'quote':text,'href':href,'source_url':source_url}
            remaining -= len(text)

    def add_panel(origin, limit):
        panel, direct = figures[origin]
        href = reading_url(item_id,version_id,f'kh-visual-{origin}')
        for index, text in enumerate(_panel_quotes(panel,question)):
            if index >= limit:
                break
            add(origin,text,href,direct)

    if selected_quote:
        for text, href, direct in _selection_evidence(anchor,selected_quote,blocks,figures,guide_sections,
                                                      item_id,version_id,base):
            add(anchor,text,href,direct)
    if anchor in figures:
        add_panel(anchor, 5)
    elif anchor:
        # A valid body selection takes precedence even when a visual
        # manifest is available for this same edition.
        for block in blocks:
            if block['anchor'] == anchor:
                for text in _selected_spans(block,focus,expanded):
                    add(anchor,text,reading_url(item_id,version_id,anchor),_block_location(block,base))
    for origin in [origin for origin in figures if origin != anchor][:3]:
        add_panel(origin, 2)
    for block, text in _block_spans(blocks,focus,expanded):
        if len(catalog) == MAX_EVIDENCE:
            break
        origin = block['anchor']
        add(origin,text,reading_url(item_id,version_id,origin),_block_location(block,base))
    return catalog


def check_section(raw, catalog):
    """Validate and resolve model IDs. Links and quotations come from the catalog."""
    if type(raw) is not dict or set(raw) != {'kind','text','ids'}:
        raise ValueError('回答段落格式無效。')
    kind, text, ids = raw['kind'], raw['text'], raw['ids']
    if not isinstance(kind,str) or kind not in KINDS or not isinstance(text,str) or len(text) > MAX_SECTION_TEXT:
        raise ValueError('回答種類或長度無效。')
    text = text.strip()
    visible = unicodedata.normalize('NFKC', text)
    if (not _CJK.search(text) or _URL.search(visible)
            or any(unicodedata.category(c) in {'Cc','Cf','Cs'} for c in text if c not in '\n\t')):
        raise ValueError('回答必須是中文純文字，引用連結由系統產生。')
    if type(ids) is not list or len(ids) > 6 or any(not isinstance(ref,str) or ref not in catalog for ref in ids):
        raise ValueError('回答含有這次未提供的引文。')
    if len(ids) != len(set(ids)):
        raise ValueError('回答含有重複的引文。')
    missing = visible in {MISSING, MISSING + '。', MISSING + '.'}
    if missing == bool(ids) or (MISSING in visible and not missing):
        raise ValueError('有依據的回答須提供引文，缺文時只接受「原文未交代」。')
    claimed = _measures(text)
    if claimed and not claimed.issubset(set().union(*(_measures(catalog[ref]['quote']) for ref in ids))):
        raise ValueError('回答的量化數字或單位未見於所引原文。')
    return {'kind':kind, 'text':MISSING if missing else text,
            'citations':[{'id':ref, **catalog[ref]} for ref in ids], 'missing':missing}


def _context(item, version, catalog):
    roles = {block['anchor']: role for block, (role, _) in
             zip(version['blocks'],local_study.section_labels(version['blocks']))}
    item_id, version_id = _identity(item,version)
    entries = []
    for cite in catalog.values():
        if cite['href'] != reading_url(item_id,version_id,cite['anchor']):
            continue  # A panel has a different display anchor and is not prose.
        entries.append({'anchor':cite['anchor'],'quote':cite['quote'],'source_url':cite['source_url'],
                        'reading_url':cite['href'],'section_role':roles.get(cite['anchor'], 'other')})
    source = {'id':'S1','item_id':item_id,'version_id':version_id,'title':item['title'],
              'scope':version['scope'],'excerpts':entries}
    return {'sources':[source]}


def _missing(kind):
    return {'kind':kind,'text':MISSING,'ids':[]}


def _from_reader(kind, section, catalog):
    ids = []
    for cite in section['citations']:
        ref = next((ref for ref, row in catalog.items()
                    if row['href'] == cite['reading_url'] and row['anchor'] == cite['anchor']
                    and row['source_url'] == cite['source_url'] and cite['quote'] in row['quote']), None)
        if ref is None:
            return _missing(kind)
        ids.append(ref)
    if not ids:
        return _missing(kind)
    text = section['text']
    raw = {'kind':kind,'text':text,'ids':list(dict.fromkeys(ids))}
    try:
        check_section(raw,catalog)
    except ValueError:
        # The copied sentence may contain an external link. Keep that
        # publisher text in the citation and use plain local guidance.
        raw['text'] = '與這一段直接相關的原文如下，可依引文核對上下文。'
    return raw


def local_sections(item, version, question, anchor, catalog):
    """Run local source retrieval, yielding the same protocol as the provider."""
    context = _context(item,version,catalog)
    response = source_reader.ask(context,question)
    evidence, inference = _missing('evidence'), _missing('inference')
    visual_href = reading_url(item['id'],version['id'],f'kh-visual-{anchor}') if anchor else None
    selected = [ref for ref, cite in catalog.items() if cite['anchor'] == anchor and cite['href'] == visual_href]
    if selected and _VISUAL_PROMPT.search(question):
        evidence = {'kind':'evidence','text':'這是所選圖表的可核對文字。讀時先確認作者標示的對象及單位。','ids':selected[:3]}
    elif response['sections']:
        section = response['sections'][0]
        if section['kind'] == 'inference':
            inference = _from_reader('inference', section, catalog)
        else:
            evidence = _from_reader('evidence', section, catalog)
    response = source_reader.ask(context,'作者提到哪些限制？')
    limits = _from_reader('limits',response['sections'][0],catalog) if response['sections'] else _missing('limits')
    return [evidence,limits,inference]


def _guide(item, version, figures):
    manifest = {'source':visuals.paper_url(item['key'],version['label']),
                'panels':[{**panel, 'source_url':direct} for panel,direct in figures.values()]}
    catalog = make_catalog(item,version,panels=manifest)
    context = _context(item,version,catalog)
    sections = []

    def append(title, raw):
        sections.append({'title':title, **check_section(raw,catalog)})

    read = source_reader.lesson(context)
    mechanism = next((section for section in read['sections'] if section['role'] == 'mechanism'), None)
    append('核心機制拆解', _from_reader('evidence',mechanism,catalog) if mechanism else _missing('evidence'))
    for section in read['sections']:
        if section['role'] in {'problem','answer'} and section['kind'] not in {'inference','limitation'}:
            append(section['title'],_from_reader('evidence',section,catalog))
    visual_found = False
    for origin, (panel, _) in list(figures.items())[:3]:
        ids = [ref for ref, cite in catalog.items() if cite['anchor'] == origin and '#kh-visual-' in cite['href']]
        if not ids:
            continue
        visual_found = True
        if panel.get('representation') == 'pdf_page':
            text = f'先看原始 PDF 第 {panel["page_number"]} 頁的圖說，再核對同頁的原圖或表格及其標示。'
        else:
            text = ' '.join(visuals.reading_steps({**panel,'rows':[]}))
        append('核心圖表讀法',{'kind':'evidence','text':text,'ids':ids[:3]})
    if not visual_found:
        append('核心圖表讀法',_missing('evidence'))
    baseline = next((ref for ref, cite in catalog.items() if '#kh-visual-' not in cite['href']
                     and _BASELINE.search(cite['quote'])), None)
    append('既有概念與基準對照',
           {'kind':'evidence','text':'作者在這段對照既有做法或基準。閱讀時先找比較對象，再核對設定及可直接比較的指標。',
            'ids':[baseline]} if baseline else _missing('evidence'))
    limitations = source_reader.ask(context,'作者提到哪些限制？')['sections']
    append('適用條件與限制',_from_reader('limits',limitations[0],catalog) if limitations else _missing('limits'))
    inference = next((s for s in read['sections'] if s['kind'] == 'inference'),None)
    append('推論（明確標註）',_from_reader('inference',inference,catalog) if inference else _missing('inference'))
    return sections


def initialize_guides(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS grounded_guides (
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        version_id INTEGER NOT NULL REFERENCES item_versions(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(item_id,version_id,fingerprint)
    )''')


def cached_guide(store, item, version, panels=()):
    """Build L3 locally once for this persisted edition and visual snapshot."""
    item_id, version_id = _identity(item,version)
    figures = _panels(item,version,panels)
    fingerprint = _digest((_GUIDE_FORMAT,version['content_hash'],list(figures.values())))
    query = 'SELECT payload FROM grounded_guides WHERE item_id=? AND version_id=? AND fingerprint=?'
    key = (item_id,version_id,fingerprint)
    with store._connect() as conn:
        persisted = conn.execute('''SELECT i.key,v.label,v.scope,v.source_url,v.content_hash,v.blocks_json
                                 FROM item_versions v JOIN items i ON i.id=v.item_id
                                 WHERE v.id=? AND v.item_id=?''',
                                 (version_id,item_id)).fetchone()
        if (persisted is None or persisted['key'] != item['key']
                or any(persisted[field] != version[field] for field in ('label','scope','source_url','content_hash'))
                or json.loads(persisted['blocks_json']) != version['blocks']):
            raise ValueError('導讀只能使用這篇文章已保存的固定版本。')
        initialize_guides(conn)
        row = conn.execute(query,key).fetchone()
    if row:
        return json.loads(row['payload'])
    guide = {'item_id':item_id,'version_id':version_id,'mode':'local_source',
             'sections':_guide(item,version,figures)}
    with store._connect() as conn:
        conn.execute('INSERT OR IGNORE INTO grounded_guides VALUES (?,?,?,?)',
                     (*key,json.dumps(guide,ensure_ascii=False)))
        row = conn.execute(query,key).fetchone()
    return json.loads(row['payload'])
