"""Private, evidence-based ranking of a fixed public candidate pool."""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import math
import json
import re

from .connections import STOPWORDS, reading_url
from .daily import attached_versions
from .domains import article_domains, cacheable_public_abstract, related_domains, stated_passages, subject_blocks
from .excerpts import preview, sentence_spans


# A small bilingual vocabulary bridges a saved Chinese question and an English
# abstract. Exact uncommon terms still work outside these categories.
THEMES = {
    'retrieval': ('檢索與引用', r'\b(?:rag|graphrag|retrieval|rerank\w*|grounding|citations?)\b|檢索|重排|引用|來源定位'),
    'memory': ('跨次互動記憶', r'long.term memory|agent memory|memory episodes?|interaction histor|記憶更新|跨次記憶|長期記憶|互動歷史'),
    'agent': ('代理工具與工作流程', r'agentic|agents? framework|agent harness|tool[ -](?:use|call)|multi[ -]agent|coding agents?|代理|工具呼叫|派工'),
    'eval': ('評測與可靠性', r'benchmark|evaluation|calibration|reproducib|ablation|評估|評測|校準|消融|可重現|驗收'),
    'inference': ('推論資源與路由', r'\b(?:inference|quantiz\w*|token budget|model routing|gpu (?:kernels?|deployment|serving)|kv[ -]cache|speculative decoding|pagedattention|flashattention)\b|推論|模型路由|量化|序列解碼'),
    'vision': ('圖文與視覺理解', r'vision[ -]language|multi[ -]?modal|object tracking|visual evidence|computer vision|多模態|視覺|圖文'),
    'robotics': ('Robotics', r'\b(?:robot(?:ics?)?|embodied ai|physical ai|robotic manipulation)\b|機器人|機械臂|具身'),
    'audio': ('音訊與語音', r'speech|transcri|audio|whisper|語音|逐字稿|轉錄|音訊'),
    'finance': ('量化研究與回測', r'backtest|quantitative financ|market microstructure|portfolio|股票|回測|量化交易|投資組合|市場微結構'),
    'learning': ('教學與閱讀', r'knowledge hub|educational|tutoring|learning companion|scientific reading|教學|學習助理|學習流程|論文閱讀|圖表導讀'),
    'video': ('影片分析', r'video segment|video understanding|temporal localization|highlight detection|影片分析|影片分段|自動剪輯|精彩片段'),
    'privacy': ('資料隱私與安全', r'privacy|data leakage|secure inference|隱私|資料不外送|資料隔離'),
    'training': ('訓練與資料策展', r'data flywheels?|data curation|test[ -]time (?:training|scaling)|資料飛輪|資料策展|測試時訓練'),
}
PATTERNS = {key:re.compile(pattern,re.I) for key,(_,pattern) in THEMES.items()}
_GENERIC = STOPWORDS | frozenset(
    'use uses using used data approach method proposed propose introduce based paper study task tasks results '
    'model models show shows state system systems significant significantly training performance experiment experiments '
    'different framework large language learning novel new dataset datasets benchmark benchmarks evaluation first existing '
    'demonstrate present general improves improve improved ours compared method baseline baselines '
    'accuracy report reported support among better challenges practical analysis scale work many'.split()
)
_TERM = re.compile(r'[a-z][a-z0-9_-]{2,35}|[\u3400-\u9fff]{2,}')
_EVALUATION = re.compile(r'\b(?:benchmark|ablation|evaluation|dataset|baseline|experiments?|accuracy|latency|throughput)\w*\b|評測|準確率|消融|實驗|延遲', re.I)
_QUANTITY = re.compile(r'\d+(?:\.\d+)?\s*(?:%|×|x\b|milliseconds?|ms\b|seconds?|tokens?|points?|examples?|tasks?|datasets?|benchmarks?|kernels?)', re.I)

# Eligibility requires evidence of a technical subject. Private reading
# history can change the order of eligible candidates.
@dataclass(frozen=True)
class Topic:
    category: str
    axis: str
    pattern: re.Pattern
    angle: str
    signal_topics: frozenset


_TOPICS = (
    Topic('推論最佳化與 ML 系統', 'core', re.compile(
        r'\b(?:kv[ -]cache|pagedattention|flashattention|speculative decoding|llm (?:inference|serving)|'
        r'inference (?:engines?|servers?|optimization|acceleration|scheduling|runtime)|'
        r'(?:model|weight|activation|post.training|low.bit) quantiz\w*|'
        r'quantized (?:inference|decoding|llms?)|(?:gpu|cuda) (?:kernels?|runtime|scheduling)|'
        r'(?:edge|on.device) inference|low.latency (?:sequence )?(?:decoding|inference)|continuous batching)\b'
        r'|推論最佳化|推論加速|序列解碼|模型量化|邊緣推論', re.I),
        '可比較推論的資源、延遲或吞吐量取捨', frozenset({'inference'})),
    Topic('多模態 AI', 'core', re.compile(
        r'\b(?:multi[ -]?modal|cross[ -]modal|vision[ -]language (?:models?|alignment|pretraining)|'
        r'vision language models?|visual reasoning|video[ -]language|audio[ -]visual|vlms?)\b'
        r'|多模態|跨模態|圖文對齊|音視融合', re.I),
        '可追蹤不同模態的表示、對齊或融合方式', frozenset({'vision','video','audio'})),
    Topic('AI Agents、記憶與檢索', 'core', re.compile(
        r'\b(?:graph[ -]?rag|rag|retrieval[ -]augment\w*|long[ -]term memory|agent memory|'
        r'(?:multi[ -]agent|agentic|llm agents?)|model context protocol|tool[ -](?:use|calling|calls)|'
        r'knowledge graphs?|neuro[ -]symbolic|(?:dense|episode|document|memory) retrieval|'
        r'(?:llm|model) (?:safety|faithfulness|hallucinations?)|hallucination (?:detection|mitigation)|'
        r'citation verifi\w*)\b|多代理|長期記憶|知識圖譜|神經符號|生成式檢索|工具呼叫|幻覺抑制', re.I),
        '可比較代理協作、記憶檢索或答案可靠性的做法', frozenset({'retrieval','memory','agent','privacy'})),
    Topic('具身與空間推理', 'frontier', re.compile(
        r'\b(?:embodied reasoning|physical simulators?|interactive simulation|spatial reasoning)\b|物理建模|空間推理', re.I),
        '可看模型如何表示環境、預測互動或規劃動作', frozenset()),
    Topic('模型能力與訓練典範', 'frontier', re.compile(
        r'\b(?:data flywheels?|(?:automated|adaptive|iterative) data curation|training data mixtures?|'
        r'test[ -]time (?:scaling|training|adaptation)|long[ -]horizon reasoning|human[ -]ai collaboration|'
        r'(?:foundation model|transformer) architectures?|mixture[ -]of[ -]experts|state[ -]space models?|'
        r'(?:language models?|llms?) (?:pretraining|post[ -]training|reasoning)|'
        r'reinforcement learning with verifiable rewards)\b|資料飛輪|自動化資料策展|測試時訓練|人機協作', re.I),
        '可檢查架構、訓練或資料流程如何改變模型能力', frozenset({'training'})),
)
_DOMAIN = re.compile(
    r'\b(?:clinical|medical|medicine|health[ -]?care|patients?|radiolog\w*|oncolog\w*|'
    r'cognitive (?:science|psychology|modeling)|reading (?:psychology|comprehension|behavior|fluency)|'
    r'psycholinguistics|eye[ -](?:tracking|movements?)|gaze[ -]tracking)\b|臨床|醫療|眼動|認知科學|閱讀心理', re.I)
_CONTRIBUTION = re.compile(
    r'\b(?:we|this (?:work|paper|method|study))\s+(?:propos\w*|introduc\w*|present\w*|develop\w*|'
    r'design\w*|build\w*|evaluat\w*|stud\w*|demonstrat\w*|train\w*|releas\w*|'
    r'improv\w*|show\w*|explor\w*|investigat\w*|implement\w*)\b', re.I)
_METHOD = re.compile(
    r'\b(?:we|this (?:work|paper|method|study))\s+(?:propos\w*|introduc\w*|develop\w*|design\w*|build\w*|present\w*)\b', re.I)
_TRANSFER_VALIDATION = re.compile(
    r'\b(?:we\s+(?:evaluat\w*|test\w*|validat\w*|benchmark\w*|demonstrat\w*)|'
    r'(?:was|were) (?:evaluated|validated|tested)|experiments (?:[^.!?]{1,180} )?(?:show|demonstrate))\b', re.I)
_TRANSFER_LIMIT = re.compile(
    r'\b(?:not|never|will|future)\s+(?:yet\s+)?(?:test\w*|evaluat\w*|validat\w*)\b|'
    r'\b(?:limited|restricted|confined) to\b', re.I)
_EXTERNAL_DOMAINS = re.compile(
    r'\b(?:non[ -](?:medical|clinical)|general[ -]domain|open[ -]domain|legal|finance|financial|'
    r'software engineering|robotics)\b|非醫療|非臨床|開放領域|法律|金融', re.I)
_FRAMEWORKS = {'pytorch':'PyTorch','vllm':'vLLM','sglang':'SGLang','tensorrt-llm':'TensorRT-LLM',
               'transformers':'Hugging Face Transformers','huggingface-hub':'Hugging Face Hub'}
_ENGINEERING_CHANGE = re.compile(
    r'\b(?:introduc\w*|add\w*|support\w*|implement\w*|accelerat\w*|reduc\w*|'
    r'redesign\w*|optimi[sz]\w*|enable\w*|overhaul\w*|improv\w*|remov\w*)\b', re.I)
_ENGINEERING_DETAIL = re.compile(
    r'\b(?:compiler backends?|graph breaks?|compil(?:er|ation) (?:pipelines?|cache|backend)|'
    r'kernel (?:fusion|launch overhead)|cuda graphs?|(?:distributed|disaggregated|multi.node) (?:inference|serving|execution)|'
    r'prefix caching|kv[ -]cache(?: sharing| reuse| offloading)?|continuous batching|speculative decoding|'
    r'(?:fp8|int4|int8|fp4|awq|gptq) (?:kernels?|quantiz\w*|inference)|'
    r'(?:breaking|backward[ -]incompatible) (?:change|api)|attention backends?|'
    r'(?:tensor|pipeline|expert|context) parallelism|dynamic shapes?|'
    r'(?:gpu|accelerator|cuda|rocm) (?:kernel|runtime|architecture|backend)s?|'
    r'weight streaming|model sharding|memory (?:offloading|allocator)|'
    r'engine architecture|fused attention|grouped gemm|torch\.compile)\b', re.I)
_MAINTENANCE = re.compile(
    r'\b(?:typos?|docs[ -]only|documentation only|dependency bumps?|refactor[ -]only)\b|'
    r'\b(?:adds?|updates?|fixes?|clarifies?|improves?) (?:the )?(?:documentation|docs|changelog|release notes)\b', re.I)
_FINANCE = re.compile(
    r'\b(?:quantitative financ\w*|backtest\w*|algorithmic trading|asset pricing|'
    r'market microstructure|financial forecasting|portfolio (?:construction|optimization|management))\b'
    r'|量化交易|量化研究|回測|市場微結構', re.I)
_FINANCE_VALIDATION = re.compile(r'out[ -]of[ -]sample|walk[ -]forward|樣本外|滾動驗證', re.I)
_FINANCE_COSTS = re.compile(
    r'\b(?:account\w* for|adjust\w* for|net of|after|includ\w*|incorporat\w*|model\w*|with|under) '
    r'(?:realistic |estimated |actual )?(?:transaction costs?|slippage|market impact)\b'
    r'|(?:扣除|計入|納入|考慮)(?:交易成本|滑價)', re.I)
_FINANCE_LIMIT = re.compile(
    r'\b(?:no|without) (?:any )?(?:out[ -]of[ -]sample|walk[ -]forward)\b|'
    r'\b(?:ignor\w*|exclud\w*|without (?:accounting for|including)) (?:transaction costs?|slippage|market impact)\b'
    r'|未(?:計入|納入|考慮)(?:交易成本|滑價)', re.I)
_FINANCE_COMPARISON = re.compile(
    r'\b(?:baselines?|drawdown|sharpe|risk[ -]adjusted|market regimes?)\b|基準|回撤|風險調整', re.I)
_TREND = re.compile(
    r'\b(?:on[ -]device (?:ai|models?)|inference hardware|ai accelerators?|chiplet\w*|open[ -]weight models?)\b', re.I)
_PRODUCTION = re.compile(
    r'\b(?:in production|production (?:deployment|traffic|workloads?)|'
    r'deployed (?:at|to|on)|commercial rollout)\b', re.I)
_BLIND_SPOT_TAG = '#盲點/待查'
_DOMAIN_ANGLES = {
    'Robotics': '可比較感知、控制或操作策略在不同任務上的取捨',
    'World Models': '可檢查模型如何預測環境變化及支援後續規劃',
    'AI Agents': '可比較代理如何呼叫工具、分工或處理失敗',
    'RL': '可核對策略如何從回饋習得，以及評估使用的條件',
    '自動駕駛': '可追蹤系統如何預測其他用路者、規劃軌跡或評估閉環表現',
}


def _domain_topic(label, quote):
    axis = 'core' if label == 'AI Agents' else 'frontier'
    topics = {'retrieval', 'memory', 'agent'} if label == 'AI Agents' else set()
    return label, axis, f'「{quote}」對應{label}，{_DOMAIN_ANGLES[label]}。', frozenset(topics)


def _spectrum(item, opening, domains):
    title = item['title']
    text = f'{title} {opening}'
    sentences = [opening[start:end] for start,end,_ in sentence_spans(opening)]
    contributions = [sentence for sentence in sentences if _CONTRIBUTION.search(sentence)]

    framework = _FRAMEWORKS.get(item['source_name'])
    if framework and not item['key'].startswith('arxiv:'):
        release_notes = (part for sentence in [title, *sentences] for part in re.split(r'\s+(?=[*-]\s)', sentence))
        for sentence in release_notes:
            detail = _ENGINEERING_DETAIL.search(sentence)
            if detail and _ENGINEERING_CHANGE.search(sentence) and not _MAINTENANCE.search(sentence):
                return ('工程工具與框架', 'radar',
                        f'{framework} 的更新說明提到「{detail[0]}」，可評估對既有執行環境或部署方式的影響。',
                        frozenset({'inference','agent','training'}))
        return None

    if _DOMAIN.search(text):
        if not (any(_METHOD.search(sentence) and (
                    related_domains('', [sentence]) or any(topic.pattern.search(sentence) for topic in _TOPICS))
                    for sentence in contributions)
                and any(_TRANSFER_VALIDATION.search(sentence) and _EXTERNAL_DOMAINS.search(sentence)
                        and not _TRANSFER_LIMIT.search(sentence) for sentence in sentences)):
            return None

    finance = _FINANCE.search(title) or next(
        (match for sentence in contributions if (match := _FINANCE.search(sentence))), None)
    if finance:
        if (all(check.search(opening) for check in (_FINANCE_VALIDATION,_FINANCE_COSTS,_FINANCE_COMPARISON))
                and not _FINANCE_LIMIT.search(opening)):
            return ('量化研究與市場結構', 'radar',
                    f'「{finance[0]}」同時交代樣本外驗證及交易摩擦，可檢查結果是否有實作價值。',
                    frozenset({'finance'}))
        return None

    # Prefer the title and then the authors' stated contribution. This avoids
    # treating a passing mention in the abstract as the primary subject.
    for passage in [title, *contributions]:
        supported = {label: quote for label, quote in related_domains('', [passage]).items() if label in domains}
        if supported:
            return _domain_topic(*next(iter(supported.items())))
        matches = [(topic, match) for topic in _TOPICS if (match := topic.pattern.search(passage))]
        if matches:
            topic, match = min(matches, key=lambda pair: (
                pair[1][0].casefold() in {'multimodal','multi-modal','cross-modal','多模態','跨模態'},
                pair[1].start(),
            ))
            return topic.category, topic.axis, f'「{match[0]}」對應{topic.category}，{topic.angle}。', topic.signal_topics

    if domains:
        return _domain_topic(*next(iter(domains.items())))

    trend = _TREND.search(text)
    if trend and _PRODUCTION.search(opening) and _QUANTITY.search(opening):
        return ('新興技術與產業落地', 'radar',
                f'「{trend[0]}」已提供正式部署與量化資訊，可評估採用條件。',
                frozenset({'inference','training'}))
    return None


def themes(text):
    return frozenset(key for key,pattern in PATTERNS.items() if pattern.search(text))


def terms(text):
    result = set()
    for word in _TERM.findall(text.casefold()):
        if word.isascii():
            if word.strip('_-') and word not in _GENERIC:
                result.add(word)
        else:
            # Chinese compounds can be matched without a tokenizer dependency.
            result.update(word[i:i+3] for i in range(max(1,len(word)-2)))
    return frozenset(result)


@dataclass(frozen=True)
class Signal:
    kind: str
    label: str
    href: str
    topics: frozenset
    vocabulary: frozenset


def _signal(kind, title, href, text, domains):
    return Signal(kind, title, href, themes(text) | domains.keys(), terms(text))


def _opening(blocks, limit=4500):
    parts, left = [], limit
    for block in blocks:
        if left <= 0:
            break
        if re.search(r'(?:^bib[.:-]|references|bibliography)',block['anchor'],re.I):
            continue
        parts.append(block['text'][:left]);left -= len(parts[-1]) + 1
    return ' '.join(parts)


def _signals(store):
    signals = []
    with store._connect() as db:
        reads = db.execute('''SELECT i.id AS item_id,v.id AS version_id,t.title,v.blocks_json,
            COALESCE(r.updated_at,i.updated_at) AS seen FROM items i JOIN item_versions v ON v.item_id=i.id
            JOIN version_titles t ON t.version_id=v.id
            LEFT JOIN reading_sessions r ON r.item_id=i.id AND r.version_id=v.id
            WHERE (r.revision>0 OR i.status='read')
            ORDER BY seen DESC,v.id DESC LIMIT 24''').fetchall()
        questions = db.execute('''WITH q AS (
              SELECT item_id,version_id,open_questions AS text,updated_at AS when_saved,'' AS anchor FROM reading_sessions
                WHERE trim(open_questions)<>''
              UNION ALL SELECT item_id,version_id,question_draft,updated_at,'' FROM reading_sessions WHERE trim(question_draft)<>''
              UNION ALL SELECT item_id,version_id,questions,created_at,'' FROM study_progress WHERE trim(questions)<>''
              UNION ALL SELECT item_id,version_id,prompt,created_at,'' FROM study_records WHERE mode='qa' AND trim(prompt)<>''
              UNION ALL SELECT item_id,version_id,question,created_at,'' FROM questions WHERE trim(question)<>''
              UNION ALL SELECT item_id,version_id,trim(substr(text,:after_tag)),created_at,anchor FROM notes
                WHERE substr(text,1,:tag_length)=:tag AND substr(text,:after_tag,1) IN ('',' ',char(9),char(10),char(13))
            ) SELECT q.item_id,q.version_id,t.title,q.anchor,
                CASE WHEN q.anchor<>'' AND trim(q.text,char(9)||char(10)||char(13)||' ')='' THEN (
                    SELECT json_extract(value,'$.text') FROM json_each(v.blocks_json)
                    WHERE json_extract(value,'$.anchor')=q.anchor LIMIT 1
                ) ELSE q.text END AS text
            FROM q JOIN version_titles t ON t.version_id=q.version_id
            JOIN item_versions v ON v.id=q.version_id ORDER BY q.when_saved DESC LIMIT 20''',
            {'tag':_BLIND_SPOT_TAG,'tag_length':len(_BLIND_SPOT_TAG),'after_tag':len(_BLIND_SPOT_TAG)+1}).fetchall()
    seen = set()
    for row in reads:
        if row['item_id'] in seen:
            continue
        seen.add(row['item_id'])
        primary, methods = subject_blocks(json.loads(row['blocks_json']))
        passages = [body for _, body in (*primary, *methods)]
        text = row['title'] + ' ' + ' '.join(passage for body in passages for passage in stated_passages(body))[:2500]
        if not _DOMAIN.search(text):
            signals.append(_signal('reading', row['title'], reading_url(row['item_id'], row['version_id']),
                                   text, related_domains(row['title'], passages)))
    for row in questions:
        # Questions affect ranking locally. Their text never reaches UI evidence
        # snippets, the refresh fetcher, or a model payload.
        text = (row['text'] or '')[:2500]
        if text and not _DOMAIN.search(text):
            href = reading_url(row['item_id'],row['version_id'],row['anchor'])
            signals.append(_signal('question', row['title'], href, text, related_domains('', [text])))
    return signals, seen | {r['item_id'] for r in questions}


def _evidence(text):
    candidates = []
    for start,end,complete in sentence_spans(text):
        sentence = text[start:end].strip()
        if not 45 <= len(sentence) <= 550:
            continue
        score = 2*bool(_QUANTITY.search(sentence)) + bool(_EVALUATION.search(sentence))
        if score:
            candidates.append((score, complete, -start,sentence))
    if candidates:
        score, _, _, sentence = max(candidates)
        return score, sentence
    return 0, preview(text, 300)[0]


def _date(value):
    try:
        result = datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo:
            return result
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            return result.replace(tzinfo=timezone.utc)
        return None
    except (AttributeError, ValueError):
        return None


def _pool(store, earliest, latest):
    # Discovery needs only public version metadata. Avoid loading every note,
    # question and original when HF adds another batch to the candidate pool.
    with store._connect() as db:
        rows = db.execute(f'''SELECT i.id,i.key,i.source_name,i.status,v.id AS vid,v.scope,v.label,
              v.published_at,v.fetched_at,v.source_url,t.title,v.blocks_json
            FROM items i JOIN item_versions v ON v.id=(SELECT id FROM item_versions
              WHERE item_id=i.id AND scope<>'unavailable'
                AND id NOT IN ({attached_versions(db)})
                AND julianday(published_at) BETWEEN julianday(?) AND julianday(?)
              ORDER BY CASE WHEN label GLOB 'v[1-9]*' THEN CAST(substr(label,2) AS INTEGER) ELSE 0 END DESC,
                CASE scope WHEN 'full' THEN 4 WHEN 'partial' THEN 3 WHEN 'abstract' THEN 2 ELSE 1 END DESC,id DESC LIMIT 1)
            JOIN version_titles t ON t.version_id=v.id WHERE i.source_name<>'library' AND i.status<>'read'
            ''',
            (earliest.isoformat(),latest.isoformat())).fetchall()
        candidates = []
        for row in rows:
            version = {'id':str(row['vid']), 'scope':row['scope'], 'label':row['label'], 'published_at':row['published_at'],
                       'fetched_at':row['fetched_at'], 'source_url':row['source_url'], 'blocks':json.loads(row['blocks_json'])}
            item = {key:row[key] for key in ('id','key','source_name','status','title')}
            candidates.append({'item':item,'version':version})
        return candidates


def _version_priority(version):
    edition = re.fullmatch(r'v([1-9][0-9]*)',version['label'])
    return (int(edition[1]) if edition else 0,
            {'full':4,'partial':3,'abstract':2}.get(version['scope'],1))


def _diverse(scored, limit):
    chosen = set()
    categories = Counter()
    if limit >= 2:
        # Core and frontier share the same scoring model and each get a place
        # when qualified material from both is available.
        for axis in ('core','frontier'):
            best = next((row for row in scored if row[3] == axis), None)
            if best:
                chosen.add(best[2])
                categories[best[4]['category']] += 1
    for cap in (1, 2):
        for _, _, key, _, pick in scored:
            if len(chosen) >= limit:
                break
            category = pick['category']
            if key not in chosen and categories[category] < cap:
                categories[category] += 1
                chosen.add(key)
    return [pick for _, _, key, _, pick in scored if key in chosen]


def _domains_json(title, blocks_json):
    return article_domains(title, json.loads(blocks_json))


def make_public_domain_cache():
    # Scoped to one app. The stored title and bytes are part of the cache key.
    return lru_cache(maxsize=32)(_domains_json)


def classify_cached_picks(store, picks, public_domains=None):
    """Resolve domain evidence against the fixed edition shown on each cached card."""
    version_ids = [pick['version']['id'] for pick in picks]
    if not version_ids:
        return picks
    placeholders = ','.join('?' for _ in version_ids)
    with store._connect() as db:
        sources = {str(row['id']): row for row in db.execute(
            f"""SELECT v.id,v.item_id,v.scope,i.key,i.source_name,t.title,v.blocks_json
                  FROM item_versions v JOIN items i ON i.id=v.item_id
                  JOIN version_titles t ON t.version_id=v.id WHERE v.id IN ({placeholders})""", version_ids)}
    result = []
    for pick in picks:
        source = sources.get(str(pick['version']['id']))
        domains = {}
        if source and source['item_id'] == pick['item']['id']:
            raw = source['blocks_json']
            analyze = _domains_json
            if public_domains is not None and cacheable_public_abstract(
                    source['source_name'], source['key'], source['scope'], raw):
                analyze = public_domains
            domains = dict(analyze(source['title'], raw))
        result.append({**pick, 'domains': domains})
    return result


def rank(store, projects=(), *, extra=(), limit=5, now=None, only_new=False, exclude_keys=()):
    """Score locally. The legacy projects argument is accepted but carries no ranking weight."""
    if limit <= 0:
        return []
    today = now or datetime.now(timezone.utc)
    earliest, latest = today-timedelta(days=45), today+timedelta(hours=36)
    signals, seen = _signals(store)
    by_key = {} if only_new else {row['item']['key']:row for row in _pool(store,earliest,latest)}
    if extra:
        with store._connect() as db:
            existing = {r['key']:r for r in db.execute('SELECT id,key,status FROM items')}
        for entry in extra:
            key = entry['key']
            stored = existing.get(key)
            date = _date(entry.get('published_at'))
            if not date or not earliest <= date <= latest or entry['scope'] == 'unavailable':
                continue
            if stored and (only_new or stored['status'] == 'read' or stored['id'] in seen):
                continue
            previous = by_key.get(key)
            if previous and _version_priority(previous['version']) >= _version_priority(entry):
                continue
            by_key[key] = {'item':{'id':None,'key':key,'source_name':entry['source_name'],'title':entry['title'],'status':'unread'},
                           'version':entry, 'new_entry':entry}
    excluded = frozenset(exclude_keys)
    features = []
    for row in by_key.values():
        item, version = row['item'], row['version']
        if item['id'] in seen or item['key'] in excluded or item['source_name'] == 'library':
            continue
        date = _date(version.get('published_at'))
        if not date or not earliest <= date <= latest:
            continue
        blocks = version['blocks']
        primary, methods = subject_blocks(blocks)
        domains = related_domains(item['title'], (text for _, text in (*primary, *methods)))
        opening = (' '.join(text for _, text in primary or methods)[:4500]
                   if item['key'].startswith('arxiv:') else _opening(blocks))
        if not opening:
            continue
        topic = _spectrum(item, opening, domains)
        if not topic:
            continue
        text = item['title'] + ' ' + ' '.join(stated_passages(opening))
        features.append((row, date, opening, topic, domains, (themes(text) & topic[3]) | domains.keys(), terms(text)))
    counts = Counter(term for *_, vocabulary in features for term in vocabulary)
    scored = []
    for row,date,opening,(category,axis,spectrum_reason,_),domains,paper_themes,paper_terms in features:
        reasons = {}
        for signal in signals:
            shared = (paper_themes & signal.topics) - {'privacy'}
            if not shared:
                continue
            common = paper_terms & signal.vocabulary
            useful = sorted((t for t in common if counts[t] <= max(2,len(features)*0.24)),key=lambda t:(counts[t],t))[:3]
            similarity = 3*len(shared) + sum(min(2,math.log2(1+len(features)/counts[t])/2) for t in useful)
            if signal.kind not in reasons or similarity > reasons[signal.kind][0]:
                labels = [THEMES[t][0] if t in THEMES else t for t in sorted(shared)[:2]]
                reasons[signal.kind] = (similarity,{'kind':signal.kind,'title':signal.label,'href':signal.href,'matches':labels})
        evidence_strength, evidence = _evidence(opening)
        score = sum(min(6, value[0]) / 3 * {'question':4,'reading':2}[key] for key,value in reasons.items())
        score += 2*evidence_strength + max(0, 8-(today-date).days)/2
        ordered = [reasons[k][1] for k in ('question','reading') if k in reasons]
        scored.append((score,date,row['item']['key'],axis,{**row,'reasons':ordered,'evidence':evidence,
                      'evidence_strength':evidence_strength,'personalized':bool(ordered),
                      'category':category,'spectrum_reason':spectrum_reason,'domains':domains}))
    scored.sort(key=lambda x:x[:3],reverse=True)
    return _diverse(scored,limit)
