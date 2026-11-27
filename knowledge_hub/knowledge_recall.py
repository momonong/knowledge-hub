"""Local, version-bound recommendations from the catalogue and reading records."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from math import log
import re
import unicodedata
from urllib.parse import urlsplit

from .connections import STOPWORDS, reading_url
from .domains import METHOD_ALIASES, domain_only, related_domains, stated_passages, subject_blocks
from .library import KnowledgeLibrary
from .library_views import source_href
from .sources import SAFE_ANCHOR_RE


_WORD = re.compile(r'[a-z0-9]+|[\u3400-\u9fff]+')
_GENERIC = STOPWORDS | frozenset('''
    able achieve across agent agents ai approach approaches article benchmark benchmarks based
    compare comparison data dataset datasets different effective efficient evaluation existing
    experiment experiments framework frameworks full general generation goal high improve
    improves including information introduce introduces large learning llm llms model modeling
    autonomous driving driverless robotics robotic robots vehicle vehicles
    fine grained language natural reasoning reflection themselves transferable transformers
    models method methods module multiple network neural new novel number open paper performance
    proposed provides result results robust show shows single state system systems task tasks
    test tests text trained training understanding various version work works world
'''.split())
# Prefer explicit bilingual equivalents to implying a relationship from a generic token.
_ALIASES = {
    '相機內參': ('camera intrinsics', 'intrinsic parameters', '相機內部參數'),
    '相機姿態': ('camera pose', 'camera poses', '相機位姿'),
    '立體視覺': ('stereo vision', 'binocular vision', 'stereo matching', 'stereo geometry', '雙目視覺', '雙目幾何'),
    '幾何推理': ('geometric reasoning', 'spatial geometry', '3d spatial reasoning', '三維幾何', '空間幾何推理'),
    '跨視角幾何': ('cross view geometry', 'cross view geometric', '跨視角幾何約束'),
    '深度估計': ('depth estimation', 'metric depth', 'depth prediction', '深度預測'),
    '視覺語言模型': ('vision language model', 'vision language models', 'vlm', 'vlms', '視覺與語言模型'),
    '視覺語言表示': ('vision language representation', 'vision language representations'),
    '視覺語言動作模型': ('vision language action', 'vla'),
    '具身代理': ('embodied agent', 'embodied agents', '具身智慧體'),
    '機器人操作': ('robot manipulation', 'robotic manipulation', 'manipulation tasks', '機械臂操作'),
    '模仿學習': ('imitation learning',),
    '動作分塊': ('action chunking',),
    '擴散策略': ('diffusion policy', 'action diffusion'),
    '知識工程': ('knowledge engineering',),
    '知識圖譜': ('knowledge graph', 'knowledge graphs', '實體關係圖'),
    '本體建構': ('ontology construction', 'ontology engineering', 'ontology building', '本體工程'),
    '本體論': ('ontology', 'ontologies'),
    '資料血緣': ('data lineage',),
    '來源溯源': ('provenance', '證據溯源'),
    '工具使用': ('tool use', 'tool usage', 'tool calling', '工具呼叫', '呼叫工具', 'api 呼叫'),
    '代理評測': ('agent evaluation', 'agent benchmark', 'evaluating llms as agents', 'agent 評測'),
    '情節記憶': ('episodic memory', '經驗記憶'),
    '分層記憶': ('hierarchical memory', 'tiered memory', '多層記憶', '記憶層級'),
    '長期記憶': ('long term memory', 'longterm memory', '長期代理記憶'),
    '文字反思': ('verbal reflection', 'verbal feedback', '文字回饋反思'),
    '不更新模型權重': ('without updating model parameters', 'without updating model weights',
                      'no weight updates', '不更新模型參數'),
    '檢索增強生成': ('retrieval augmented generation', 'rag'),
    '密集檢索': ('dense retrieval', 'dense passage retrieval'),
    '圖檢索': ('graph retrieval', 'graph based retrieval'),
    '多跳問答': ('multi hop question answering', 'multihop qa'),
    '跨語言檢索': ('cross lingual retrieval', 'cross language retrieval', '多語檢索'),
    '少樣本學習': ('few shot learning', 'few shot'),
    '零樣本分類': ('zero shot classification', 'zero shot prediction'),
    '零樣本設定': ('zero shot', '零樣本檢索', '零樣本泛化', '零樣本轉移', '零樣本分類'),
    '多語設定': ('multilingual', 'multiple languages', '多語評測', '多語模型', '多語檢索評測'),
    '面向情感分析': ('aspect based sentiment analysis', 'absa', '方面情感分析'),
    '對比學習': ('contrastive learning', 'contrastive pretraining'),
    'KV 快取': ('kv cache', 'kv caches', 'key value cache', 'key value caches', 'kv 快取', 'kv 緩存',
              '鍵值快取', 'kv states', 'key value kv states', 'kv quantization'),
    '量化': ('quantization', 'quantisation', 'quantized', 'quantised'),
    '訓練後量化': ('post training quantization', 'ptq'),
    '低秩適配': ('low rank adaptation', 'lora'),
    '記憶體頻寬': ('memory bandwidth',),
    '稀疏注意力': ('sparse attention',),
    '注意力機制': ('attention mechanism', 'cross attention', 'self attention'),
    '分頁注意力': ('pagedattention', 'paged attention', '記憶體分頁'),
    '連續批次': ('continuous batching',),
    '因果推論': ('causal inference', '因果推斷'),
    '資料洩漏': ('data leakage',),
    '分布偏移': ('distribution shift', 'distribution shifts', '分佈轉移'),
    '指令微調': ('instruction tuning', 'instruction finetuning', 'instruction fine tuning'),
    '提示注入': ('prompt injection',),
    **METHOD_ALIASES,
}

# Broad but real domains provide useful background at a lower priority than a mechanism.
_BACKGROUND_TERMS = {'視覺語言模型', '視覺語言表示', '多語設定', '零樣本設定'}
_KIND_WEIGHT = {'question': 1.16, 'understanding': 1.15, 'note': 1.10, 'read': 1.01, 'starter': 1.06}


def _normalize(text):
    return ' '.join(_WORD.findall(unicodedata.normalize('NFKC', text).casefold()))


def _phrase(phrases):
    normalized = {_normalize(p) for p in phrases}
    return re.compile('|'.join(
        re.escape(p) if p and ord(p[0]) > 127 else r'(?<![a-z0-9])' + re.escape(p) + r'(?![a-z0-9])'
        for p in sorted(normalized - {''}, key=lambda p: (-len(p), p))
    ))


_PHRASES = {label: _phrase((label, *aliases)) for label, aliases in _ALIASES.items()}


def _terms(text, tags, aliases):
    normalized = _normalize(text)
    names = {label for label, pattern in aliases if pattern.search(normalized)}
    names.update(label for label in tags if label in normalized)
    words = normalized.split()
    usable = [word not in _GENERIC and word.isascii() and not word.isdecimal() and len(word) >= 3 for word in words]
    phrases = {words[i] for i, good in enumerate(usable) if good and len(words[i]) >= 5}
    for width in (2, 3):
        phrases.update(' '.join(words[i:i+width]) for i in range(len(words) - width + 1)
                       if all(usable[i:i+width]))
    return names, {phrase for phrase in phrases if not domain_only(phrase)}


def _weights(fields, tags, aliases=_PHRASES.items()):
    terms = {}
    for field, weight in fields:
        names, phrases = _terms(field, tags, aliases)
        for name in names:
            terms[name] = max(terms.get(name, 0), weight + 1)
        for phrase in phrases:
            terms[phrase] = max(terms.get(phrase, 0), weight)
    return terms


def _publication_key(key, url, title):
    ids = {('title', _normalize(title))} if title.strip() else set()
    if key:
        ids.add(('key', key.casefold()))
    try:
        parts = urlsplit(url)
    except ValueError:
        return ids
    if not parts.hostname:
        return ids
    if parts.hostname in ('arxiv.org', 'export.arxiv.org'):
        paper = re.fullmatch(r'/(?:abs|html|pdf)/(.+?)(?:v\d+)?(?:\.pdf)?/?', parts.path)
        if paper:
            ids.add(('key', 'arxiv:' + paper[1].casefold()))
    ids.add(('url', (parts.hostname.removeprefix('www.'), parts.path.rstrip('/'))))
    return ids


@dataclass
class _Candidate:
    kind: str
    title: str
    href: str
    identities: set
    fields: tuple
    snippets: tuple
    evidence_hrefs: tuple = ()


def _library_candidates(conn, focal_id, focal_identities):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='library_records'").fetchone():
        return [], [], set()
    records = defaultdict(list)
    for row in conn.execute("""SELECT category,payload FROM library_records
                             WHERE category IN ('source','starter','concept') ORDER BY category,id"""):
        records[row['category']].append(json.loads(row['payload']))
    starters = {row['source_id']: row for row in records['starter']}
    concept_tags = defaultdict(list)
    for concept in records['concept']:
        for sid in concept['supporting_source_ids']:
            concept_tags[sid].append(concept['title'])
    linked = defaultdict(set)
    for row in conn.execute('SELECT d.source_id,i.id,i.key FROM library_documents d JOIN items i ON i.id=d.item_id'):
        linked[row['source_id']].update({('item', row['id']), ('key', row['key'].casefold())})
    candidates, focal_tags, tags = [], [], set()
    for card in records['source']:
        sid = card['id']
        title = card['title']
        identities = _publication_key(KnowledgeLibrary._item_key(card), card.get('primary_url', ''), title) | linked[sid]
        topics = tuple(card.get('topics', ()))
        for label in (*topics, *concept_tags[sid]):
            if 3 <= len(label) <= 24 and re.fullmatch(r'[\u3400-\u9fff]+', label):
                tags.add(label)
        if identities & focal_identities or ('item', focal_id) in identities:
            focal_tags.extend((*topics, *concept_tags[sid]))
            focal_identities.update(identities)
            continue
        starter = starters.get(sid)
        summary = card.get('summary_zh', '')
        context = starter.get('learning_goal_zh', '') if starter else ''
        candidates.append(_Candidate(
            'starter' if starter else 'source', title, source_href(sid), identities,
            ((title, 4), (' '.join(topics), 3), (summary, 2), (' '.join(concept_tags[sid]), 1), (context, 1)),
            (summary, context, '、'.join(topics), title),
        ))
    return candidates, focal_tags, tags


def _read_candidates(conn, focal_id):
    rows = conn.execute("""
        WITH versions AS (
            SELECT v.id,v.item_id,v.source_url,i.key,t.title,r.anchor AS reading_anchor,
                   row_number() OVER (PARTITION BY i.id ORDER BY (r.updated_at IS NOT NULL) DESC,
                       r.updated_at DESC,CASE v.scope WHEN 'full' THEN 2 WHEN 'partial' THEN 1 ELSE 0 END DESC,
                       v.id DESC) AS choice
              FROM items i JOIN item_versions v ON v.item_id=i.id JOIN version_titles t ON t.version_id=v.id
              LEFT JOIN reading_sessions r ON r.version_id=v.id AND r.item_id=v.item_id
                 AND (r.anchor<>'' OR r.reflection<>'' OR r.open_questions<>'')
             WHERE i.id<>? AND v.scope<>'unavailable' AND (i.status='read' OR r.updated_at IS NOT NULL)
        )
        SELECT row.*, v.blocks_json FROM versions row JOIN item_versions v ON v.id=row.id
         WHERE row.choice=1 ORDER BY row.id DESC LIMIT 160
    """, (focal_id,))
    for row in rows:
        blocks = json.loads(row['blocks_json'])
        primary, methods = subject_blocks(blocks)
        primary = tuple((anchor, passage) for anchor, text in (*primary, *methods)
                        for passage in stated_passages(text))
        terms = tuple((text, 2) for _, text in primary)
        href = reading_url(row['item_id'], row['id'], row['reading_anchor'] or '')
        evidence_hrefs = (*(reading_url(row['item_id'], row['id'], anchor)
                            if SAFE_ANCHOR_RE.fullmatch(anchor) else href for anchor, _ in primary), href)
        yield _Candidate('read', row['title'], href,
                         _publication_key(row['key'], row['source_url'], row['title']) | {('item', row['item_id'])},
                         ((row['title'], 4), *terms), (*(text for _, text in primary), row['title']),
                         evidence_hrefs)


_PRIVATE = """
WITH saved(item_id,version_id,anchor,text,kind,stamp,record_order) AS (
    SELECT n.item_id,n.version_id,'note-'||n.id,
        CASE WHEN n.text LIKE '#盲點/待查%' THEN trim(substr(n.text,length('#盲點/待查')+1))
             ELSE n.text END,
        CASE WHEN n.text LIKE '#盲點/待查%' THEN 'question' ELSE 'note' END,n.created_at,1
      FROM notes n WHERE NOT EXISTS (SELECT 1 FROM capture_records c WHERE c.note_id=n.id)
    UNION ALL
    SELECT item_id,version_id,'capture-'||id,CASE WHEN kind='tag' THEN
                 COALESCE(NULLIF(trim(comment),''),quote) ELSE text END,
           CASE WHEN tag='#盲點/待查' OR kind='missing' THEN 'question'
                WHEN kind='understanding' THEN 'understanding' ELSE 'note' END,updated_at,4
      FROM capture_records WHERE state='saved'
    UNION ALL
    SELECT item_id,version_id,'question-'||id,question,'question',created_at,2 FROM questions
    UNION ALL
    SELECT item_id,version_id,'l4-'||id,question,'question',created_at,2 FROM followup_turns
     WHERE state='completed' AND NOT EXISTS (
           SELECT 1 FROM capture_records c WHERE c.turn_id=followup_turns.id AND c.kind='missing')
    UNION ALL
    SELECT item_id,version_id,'source-turn-'||id,prompt,'question',created_at,2 FROM source_turns
      WHERE trim(prompt)<>''
    UNION ALL
    SELECT item_id,version_id,'open-questions',open_questions,'question',updated_at,1 FROM reading_sessions
    UNION ALL
    SELECT item_id,version_id,'progress-'||id,questions,'question',created_at,3 FROM study_progress
    UNION ALL
    SELECT item_id,version_id,'progress-'||id,understanding,'understanding',created_at,3 FROM study_progress
)
SELECT s.*,t.title,i.key,v.source_url FROM saved s
JOIN items i ON i.id=s.item_id JOIN item_versions v ON v.item_id=s.item_id AND v.id=s.version_id
JOIN version_titles t ON t.version_id=s.version_id
WHERE s.item_id<>? AND trim(s.text)<>''
ORDER BY s.stamp DESC,record_order DESC,s.item_id,s.version_id,s.anchor LIMIT 600
"""


def _private_candidates(conn, focal_id):
    for row in conn.execute(_PRIVATE, (focal_id,)):
        body = row['text'][:8000].strip()
        if body:
            yield _Candidate(row['kind'], row['title'], reading_url(row['item_id'], row['version_id'], row['anchor']),
                             _publication_key(row['key'], row['source_url'], row['title']) | {('item', row['item_id'])},
                             ((body, 4),), (body,))


def _related_terms(left, right):
    if left in right or right in left:
        return True
    for first, second in ((left, right), (right, left)):
        single = _normalize(second)
        for alias in _ALIASES.get(first, ()):
            normalized = _normalize(alias)
            if single == normalized or (single.isascii() and single in normalized.split()):
                return True
    return False


def _matches(focus, candidate, counts, tags):
    names = _PHRASES.keys() | tags
    shared = focus.keys() & candidate.keys()
    scored = []
    for label in shared:
        phrase = ' ' in label
        weight = min(focus[label], candidate[label])
        if label not in names and not phrase and (focus[label] < 5 or candidate[label] < 4 or counts[label] > 3):
            continue
        specificity = 2.8 if label in _PHRASES else 2.3 if label in tags or phrase else 1.15
        background = .72 if label in _BACKGROUND_TERMS else 1
        scored.append((weight * specificity * background / (1 + log(counts[label]) * .23), label))
    selected = []
    for score, label in sorted(scored, key=lambda entry: (-entry[0], -len(entry[1]), entry[1])):
        if selected and score < selected[0][0] * .55:
            continue
        # "kv cache" and the bilingual "KV 快取" should contribute just once.
        if any(_related_terms(label, previous) for _, previous in selected):
            continue
        selected.append((score, label))
        if len(selected) == 2:
            break
    return selected


def _window(text, match, limit):
    if len(text) <= limit:
        return text
    patterns = [re.escape(alias).replace(r'\ ', r'[\s\-‐‑–—]+') for alias in (match, *_ALIASES.get(match, ()))]
    found = re.search('|'.join(patterns), text, re.IGNORECASE)
    start = max(0, found.start() - limit // 3) if found else 0
    stop = start + limit
    selection = text[start:stop]
    if start and re.match(r'[A-Za-z0-9]{2}', text[start-1:start+1]):
        selection = re.sub(r'^[A-Za-z0-9]+', '', selection)
    if len(text) > stop and re.match(r'[A-Za-z0-9]{2}', text[stop-1:stop+1]):
        selection = re.sub(r'[A-Za-z0-9]+$', '', selection)
    return ('…' if start else '') + selection.strip() + ('…' if len(text) > stop else '')


def _excerpt(snippets, match):
    pattern = _PHRASES.get(match) or _phrase((match,))
    parts = [(index, part.strip()) for index, snippet in enumerate(snippets)
             for part in re.split(r'[\n]+|(?<=[。！？!?])|(?<=\.)\s+', snippet) if part.strip()]
    if not parts:
        return '', 0
    index, line = next(((index, part) for index, part in parts if pattern.search(_normalize(part))), parts[0])
    return _window(line, match, 236), index


def _reason(kind, match, title_focus, excerpt, shared_domains):
    context = next((label for label in title_focus if not _related_terms(label, match)), match)
    pattern = _PHRASES.get(match) or _phrase((match,))
    clauses = [part.strip() for part in re.split(r'[，；：]', excerpt) if part.strip()]
    clause = next((part for part in clauses if len(part) > len(match) + 5
                   and pattern.search(_normalize(part))), excerpt)
    clause = clause.strip('「」…。？！.!? ')
    cap = 48 if re.search(r'[\u3400-\u9fff]', clause) else 86
    quotation = f'「{_window(clause, match, cap)}」' if clause else ''
    lead = (f'兩篇都涉及 {"、".join(shared_domains)}，從「{match}」切入'
            if shared_domains else f'讀這篇時，從「{context}」切入')
    bridge = f'對照「{match}」' if context != match and not shared_domains else '作為對照'
    if kind == 'question':
        return f'{lead}，可帶著你先前的待查或提問{quotation}查對原文。'
    if kind == 'understanding':
        return f'{lead}，可用你保存的理解{quotation}{bridge}。'
    if kind == 'note':
        return f'{lead}，可用你存下的筆記{quotation}{bridge}。'
    location = {'read': '你有閱讀紀錄的這篇文章', 'starter': '這份核心教材', 'source': '這張來源卡'}[kind]
    if quotation:
        return f'{lead}，可用{location}中的{quotation}{bridge}。'
    return f'{lead}，可對照{location}裡的「{match}」。'


def recommend(store, item, version):
    """Return at most four distinct recommendations using only local records."""
    title = version.get('title') or item['title']
    identities = _publication_key(item['key'], version.get('source_url', ''), title) | {('item', item['id'])}
    with store._connect() as conn:
        conn.execute('BEGIN')
        candidates, focal_tags, tags = _library_candidates(conn, item['id'], identities)
        candidates.extend(_read_candidates(conn, item['id']))
        candidates.extend(_private_candidates(conn, item['id']))
        explicit = [row[0] for row in conn.execute(
            'SELECT concept FROM relations WHERE item_id=? AND version_id=? ORDER BY id DESC LIMIT 64',
            (item['id'], version['id']))]
    for label in explicit:
        if 3 <= len(label) <= 24 and re.fullmatch(r'[\u3400-\u9fff]+', label):
            tags.add(label)
    focal_tags.extend(explicit)
    primary, methods = subject_blocks(version.get('blocks', []))
    focus = _weights(((title, 5), (' '.join(focal_tags), 4),
                      *((passage, 3) for _, text in (*primary, *methods)
                        for passage in stated_passages(text))), tags)
    focus_domains = related_domains(title, (text for _, text in (*primary, *methods)))
    # Ranking only compares terms on both sides. Keep all possible matches
    # while skipping catalogue aliases absent from the current article.
    shared_tags = tags & focus.keys()
    shared_aliases = tuple((label, pattern) for label, pattern in _PHRASES.items() if label in focus)
    profiles = [(candidate, _weights(candidate.fields, shared_tags, shared_aliases)) for candidate in candidates
                if not candidate.identities.intersection(identities)]
    counts = Counter(label for _, terms in profiles for label in terms.keys() & focus.keys())
    ranked = []
    for candidate, profile in profiles:
        matches = _matches(focus, profile, counts, tags)
        score = sum(weight for weight, _ in matches)
        if score < 4:
            continue
        candidate_domains = related_domains(candidate.title, candidate.snippets)
        shared_domains = tuple(label for label in focus_domains if label in candidate_domains)
        match = matches[0][1]
        weight = _KIND_WEIGHT.get(candidate.kind, 1) + .07 * bool(shared_domains)
        ranked.append((score * weight, candidate, match, shared_domains))
    normalized_title = _normalize(title)
    title_terms = [(label, hit.end() - hit.start()) for label, pattern in _PHRASES.items()
                   if (hit := pattern.search(normalized_title))]
    title_focus = [label for label, _ in sorted(title_terms, key=lambda row: (-row[1], row[0]))]
    recommended = []
    for _, candidate, match, shared_domains in sorted(ranked, key=lambda row: (-row[0], row[1].title.casefold(), row[1].href)):
        if candidate.identities.intersection(identities):
            continue
        identities.update(candidate.identities)
        excerpt, index = _excerpt(candidate.snippets, match)
        href = candidate.evidence_hrefs[index] if candidate.evidence_hrefs else candidate.href
        recommended.append({'kind': candidate.kind, 'title': candidate.title, 'href': href,
                            'reason': _reason(candidate.kind, match, title_focus, excerpt, shared_domains),
                            'excerpt': excerpt})
        if len(recommended) == 4:
            break
    return recommended
