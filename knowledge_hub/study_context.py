"""Public, reproducible lexical retrieval; no hand-authored paper lessons."""
from collections import Counter
import hashlib
import json
import math
import re

from .connections import tokens, reading_url
from . import excerpts

GENERIC = set('paper propose proposed approach method model models result results study task tasks '
              'data dataset performance training evaluation baseline new based large language '
              'work framework system systems show shows different existing'.split())


def evidence_spans(sources):
    """Protocol v1 references derived only from the persisted, selected originals.

    Keep retrieval and its paragraph quotas unchanged. The wire representation
    splits long paragraphs into exact contiguous spans for the 700-char citation
    contract. Stable IDs cannot turn a pruned excerpt into another citation.
    """
    result = {}
    for source in sources:
        for excerpt in source['excerpts']:
            quote, start = excerpt['quote'], 0
            while start < len(quote):
                end = min(start + 700, len(quote))
                if end < len(quote):
                    boundaries = [start + m.end() for m in re.finditer(r'[.!?]\s', quote[start:end])
                                  if m.end() >= 350]
                    space = quote.rfind(' ', start + 350, end)
                    end = boundaries[-1] if boundaries else space + 1 if space >= 0 else end
                text = quote[start:end]
                identity = [source['id'], source.get('item_id'), source.get('version_id'),
                            source.get('content_hash'), excerpt['anchor'], quote, start, end]
                key = 'E-' + hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                    separators=(',', ':')).encode()).hexdigest()[:24]
                value = {'source_id':source['id'], 'anchor':excerpt['anchor'], 'quote':text}
                if text.strip():
                    if key in result and result[key] != value:
                        raise ValueError('Evidence identity collision')
                    result[key] = value
                start = end
    return result


def words(text):
    return tokens(text) - GENERIC


def readable_blocks(version):
    """Drop bibliography runs for retrieval without rewriting stored evidence."""
    result, skipping = [], False
    for block in version['blocks']:
        # arXiv bibliographic IDs are stronger signals than author names in prose.
        if re.search(r'(?:bib|reference)', block['anchor'], re.I):
            continue
        ranges, skipping = excerpts._body_ranges(block['text'], skipping)
        for start, end in ranges:
            text = block['text'][start:end]
            if text.strip() and not excerpts._reference_entry(text):
                result.append({**block, 'text': text})
    return result


def study_spans(blocks, terms, focus_terms):
    """Reserve evidence from methods/results when the document has headings.

    Ranking an entire article alone over-selects keyword-dense abstracts. Keep
    original spans, while allocating room to explain how it works and how it
    was tested. Documents without detectable headings retain lexical ranking.
    """
    groups = {'overview': [], 'method': [], 'evidence': []}
    group, subsection = 'overview', ''
    for index, block in enumerate(blocks):
        heading = re.fullmatch(r'\s*\d+\s+([^\n.!?]{2,130})\s*', block['text'])
        if heading:
            title = heading[1].casefold()
            group = ('method' if re.search(r'\b(?:methods?|methodology|approach|architecture|framework)\b', title)
                     else 'evidence' if re.search(r'\b(?:experiments?|evaluation|results)\b', title)
                     else 'overview')
            subsection = block['anchor']
        elif re.fullmatch(r'\s*\d+\.\d+\s+[^\n.!?]{2,130}\s*', block['text']):
            subsection = block['anchor']
        groups[group].append((index, block, subsection))
    selected, seen = [], set()
    for name, quota in [('method', 7), ('evidence', 3), ('overview', 2)]:
        entries = groups[name]
        candidates = excerpts.select_spans([b for _, b, _ in entries], terms, focus_terms, limit=64)
        # One representative per subsection first; then fill by lexical score.
        chosen, sections, occupied = [], set(), set()
        for diversify in (True, False):
            for candidate in candidates:
                i = candidate[0]
                if len(chosen) >= quota:
                    break
                if i in occupied or (diversify and entries[i][2] in sections):
                    continue
                chosen.append(candidate)
                occupied.add(i)
                sections.add(entries[i][2])
        for i, text, quality, note in chosen:
            block_text = entries[i][1]['text'].strip()
            # A short intact paragraph supplies the steps around the ranked
            # sentence. Larger/PDF blocks keep the explicitly bounded span.
            if len(block_text) <= 2000 and '\n' not in block_text:
                text, quality, note = block_text, 'paragraph', ''
            selected.append((entries[i][0], text, quality, note))
            seen.add(re.sub(r'\s+', ' ', text).strip().casefold())
    for candidate in excerpts.select_spans(blocks, terms, focus_terms, limit=24):
        identity = re.sub(r'\s+', ' ', candidate[1]).strip().casefold()
        if len(selected) >= 12:
            break
        if identity not in seen:
            selected.append(candidate)
            seen.add(identity)
    return selected


def public_context(store, item_id, version_id, question=''):
    items = store.get_all_items(focal_id=item_id)
    focal_item = next((i for i in items if i['id'] == item_id), None)
    focal = next((v for v in focal_item['versions'] if str(v['id']) == str(version_id)), None) if focal_item else None
    if not focal or focal['scope'] == 'unavailable':
        raise ValueError('找不到可讀的固定版本。')
    with store._connect() as conn:
        titles = {str(r[0]): r[1] for r in conn.execute('SELECT version_id,title FROM version_titles')}
    docs = []
    for item in items:
        if not item['versions']:
            continue
        version = focal if item['id'] == item_id else store.select_default_version(item['versions'])
        blocks = readable_blocks(version)
        if version['scope'] != 'unavailable' and blocks:
            title = titles[version['id']]
            vocab = words(title + ' ' + ' '.join(b['text'] for b in blocks))
            docs.append({'item': item, 'version': version, 'title': title, 'blocks': blocks, 'vocab': vocab})
    target = next((d for d in docs if d['item']['id'] == item_id), None)
    if not target:
        raise ValueError('沒有可引用的正文，請先取得原文。')
    frequency = Counter(t for d in docs for t in d['vocab'])
    idf = {t: math.log(1 + (len(docs) + 1) / (n + .5)) for t, n in frequency.items()}
    intro = ' '.join(b['text'] for b in target['blocks'])[:6000]
    counts = Counter(re.findall(r'[a-z][a-z0-9_-]{2,}', intro.casefold()))
    query = set(sorted(words(intro), key=lambda t: (-idf.get(t, 1) * (1 + math.log(1 + counts[t])), t))[:32])
    title_terms, focus = words(target['title']), words(question)
    query |= title_terms | focus
    weights = {t: idf.get(t, 1) * (5 if t in focus else 3 if t in title_terms else 1) for t in query}
    ranked = []
    for doc in docs:
        if doc is target:
            continue
        matches = query & doc['vocab']
        if len(matches) < 2 and not matches & (title_terms | focus):
            continue
        score = sum(weights[t] for t in matches) / (1 + math.log(1 + len(doc['vocab'])) / 8)
        ranked.append((score, doc, sorted(matches, key=lambda t: (-weights[t], t))[:10]))
    ranked.sort(key=lambda x: (-x[0], x[1]['title']))
    chosen = [(0, target, [])] + ranked[:3]
    sources = []
    for _, doc, matches in chosen:
        current = doc is target
        selected = (study_spans(doc['blocks'], query, focus) if current else
                    excerpts.select_spans(doc['blocks'], query, focus, limit=5))
        if not selected:
            continue
        version = doc['version']
        source = {'id': 'S' + str(len(sources) + 1), 'item_id': doc['item']['id'],
                  'version_id': str(version['id']), 'label': version['label'], 'title': doc['title'],
                  'source_url': version['source_url'], 'content_hash': version['content_hash'],
                  'scope': version['scope'], 'matched_terms': matches,
                  'reason': '目前閱讀的固定版本' if current else '公開原文命中：' + '、'.join(matches),
                  'excerpts': []}
        for index, text, quality, note in selected:
            block = doc['blocks'][index]
            source['excerpts'].append({'anchor': block['anchor'], 'quote': text,
                'source_url': block.get('source_url') or version['source_url'],
                'reading_url': reading_url(doc['item']['id'], version['id'], block['anchor']),
                'quality': quality, 'quality_note': note})
        sources.append(source)
    if not sources or sources[0]['item_id'] != item_id:
        raise ValueError('本篇未找到可讀的原文片段；不以背景代替本篇。')
    return {'sources': sources, 'context_truncated': True,
            'limitations': '詞彙排名是可核對的學習建議，不代表作者引用關係；僅選有限片段，圖表、公式與未送出的段落不在生成證據內。'}
