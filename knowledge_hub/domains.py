"""Bilingual research domains and methods backed by the article's own text."""
import re

from .excerpts import _body_ranges, _reference_entry, sentence_spans


_DASHES = str.maketrans('‐‑‒–—−', '------')


DOMAIN_PATTERNS = {
    'Robotics': re.compile(
        r'\b(?:robotic manipulation|robot learning|robot control|robot policies|robotics|robotic|robots?|'
        r'embodied (?:AI|agents?|learning)|physical AI|vision[- ]language[- ]action|vla (?:models?|polic\w*)|sim[- ]to[- ]real)\b'
        r'|機器人|機械臂|具身(?:智慧|智能|學習|代理)|虛實轉移', re.I),
    'World Models': re.compile(
        r'\b(?:world[- ]models?|latent dynamics models?|learned environment dynamics)\b|世界模型|潛在動態模型', re.I),
    'AI Agents': re.compile(
        r'\b(?:(?:AI|LLM|language model|coding|software|tool[- ]using|long[- ]term|long[- ]horizon) agents?|agentic(?: [a-z]+)?|'
        r'agent harness(?:es)?|agents? (?:frameworks?|orchestration|memory)|'
        r'(?:LLM|language model)[- ]based agents?)\b|AI\s*代理|智慧代理|智能代理|代理編排|代理記憶', re.I),
    'RL': re.compile(
        r'\b(?:reinforcement learning|Q[- ]learning|policy gradients?|proximal policy optimization|'
        r'soft actor[- ]critic|(?-i:RL))\b|強化學習|增強學習|策略梯度|政策梯度', re.I),
    '自動駕駛': re.compile(
        r'\b(?:autonomous (?:driving|vehicles?)|self[- ]driving(?: (?:cars?|vehicles?))?|'
        r'end[- ]to[- ]end driving|driverless (?:cars?|vehicles?))\b|自動駕駛|自駕(?:車|系統|規劃)', re.I),
}

_DOMAIN_ONLY = re.compile('|'.join(f'(?:{pattern.pattern})' for pattern in DOMAIN_PATTERNS.values()), re.I)

# These phrases describe methods or concrete technical problems. Domain names
# alone do not qualify as a connection to a saved record.
METHOD_ALIASES = {
    '動作條件式動態模型': ('action conditioned dynamics', 'action conditioned dynamics model',
                         'action conditioned world model', '動作條件化動態模型', '動作條件式世界模型'),
    '潛在空間規劃': ('latent space planning', 'planning in latent space', '潛空間規劃'),
    '模型預測控制': ('model predictive control', '模型預測控制器'),
    '軌跡預測': ('trajectory prediction', 'trajectory forecasting', 'predict future trajectories', '軌跡預報'),
    '閉環規劃': ('closed loop planning', '閉迴路規劃'),
    '閉環評估': ('closed loop evaluation', 'closed loop testing', '閉迴路評估'),
    '行為克隆': ('behavior cloning', 'behavioral cloning', '行為複製', '行為模仿'),
    '虛實轉移': ('sim to real', 'simulation to reality', '模擬到真實'),
    '離線強化學習': ('offline reinforcement learning', 'offline rl'),
    '模型式強化學習': ('model based reinforcement learning', 'model based rl', '基於模型的強化學習'),
    '策略梯度': ('policy gradient', 'policy gradients', '政策梯度'),
    'Q 學習': ('q learning',),
    '近端策略最佳化': ('proximal policy optimization', '近端策略優化'),
    '獎勵塑形': ('reward shaping', '獎勵整形'),
    '記憶鞏固': ('memory consolidation', 'consolidating memories', '記憶整合'),
    '記憶摺疊': ('memory folding', 'folding memory', '記憶折疊'),
    '工具呼叫失敗復原': ('tool failure recovery', 'recovery from tool failures', '工具失敗復原'),
}

_ABSTRACT = re.compile(r'abstract|摘要', re.I)
_METHOD_HEADING = re.compile(
    r'(?:(?:\d+\.)*\d+\s*[.:-]?\s*)?'
    r'(?:method(?:s|ology)?(?: overview)?|(?:our |proposed )?approach|(?:model|system) architecture|'
    r'(?:the )?proposed method|algorithm|preliminaries and method|方法(?:與架構)?|模型架構)'
    r'[.:：]?', re.I)
_OTHER_HEADING = re.compile(
    r'(?:(?:\d+\.)*\d+\s*[.:-]?\s*)?(?:'
    r'introduction|related (?:work|research)|background|prior work|preliminaries|'
    r'experiments?|evaluation|results|discussion|limitations|conclusions?|'
    r'appendix|references|bibliography|acknowledg(?:e)?ments|'
    r'引言|相關研究|相關工作|背景|實驗|結果|討論|限制|結論|附錄|參考文獻'
    r')[.:：]?', re.I)
_REFERENCE = re.compile(r'(?:^bib(?:[.:-]|$)|references|bibliography|related[- _]work|參考文獻|相關研究)', re.I)
_BACKGROUND = re.compile(
    r'\b(?:(?:has|have) been (?:widely |previously )?(?:used|studied|applied)|prior (?:work|studies|research|methods?)|previous (?:work|studies|research|methods?)|'
    r'related work|future work|future applications?|(?:could|might) (?:also )?be applied|'
    r'(?:we|this work) (?:do(?:es)? not|did not) (?:address|study|use|evaluate|target))\b'
    r'|先前(?:研究|工作)|既有研究|相關研究|未來(?:工作|可應用)|本(?:文|研究)未(?:涵蓋|探討|使用)', re.I)
_ASIDE = re.compile(
    r'\b(?:inspired by|such as|including applications? (?:in|to)|whereas|compared (?:with|to))\b'
    r'|例如應用於|受到.+啟發', re.I)
_NEGATED = re.compile(r'\b(?:without|non[- ]|no |not using |does not use )\s*$', re.I)
_BENCHMARK_VARIANT = re.compile(r'bench(?:mark)?s?[-_/][\w./-]+', re.I)


def subject_blocks(blocks):
    """Use abstract, identified methods, or early body text with no section metadata."""
    abstract, methods, opening = [], [], []
    section = ''
    for block in blocks[:250]:
        anchor = block['anchor']
        text = block['text'].strip()
        if _OTHER_HEADING.fullmatch(text):
            section = 'other'
            continue
        if _METHOD_HEADING.fullmatch(text):
            section = 'method'
            continue
        if _REFERENCE.search(anchor) or _reference_entry(text):
            continue
        if _ABSTRACT.fullmatch(text):
            section = 'abstract'
            continue
        if not text:
            continue
        place = (anchor, text[:2000])
        if section == 'abstract' or _ABSTRACT.search(anchor):
            if len(abstract) < 4:
                abstract.append(place)
        elif section == 'method' or _METHOD_HEADING.fullmatch(anchor.partition('.')[0]):
            if len(methods) < 8:
                methods.append(place)
        elif not section and len(text) >= 24 and len(opening) < 3:
            opening.append(place)
    return (abstract or opening), methods


def stated_passages(text):
    """Yield source clauses where related work and bibliography are not evidence."""
    ranges, _ = _body_ranges(text)
    for left, right in ranges:
        body = text[left:right]
        for start, end, _ in sentence_spans(body):
            sentence = body[start:end]
            for passage in re.split(r'[;；]', sentence):
                passage = _ASIDE.split(passage, maxsplit=1)[0].strip()
                if passage and not _BACKGROUND.search(passage) and not _reference_entry(passage):
                    yield passage


def related_domains(title, passages=()):
    """Return each evidenced domain with the matching words from title or body."""
    matches = {}
    evidence = [title]
    for text in passages:
        evidence.extend(stated_passages(text))
    for passage in evidence:
        normalized = passage.translate(_DASHES)
        # A benchmark split names evaluation data. Keep offsets so other matches quote the source.
        normalized = _BENCHMARK_VARIANT.sub(lambda match: ' ' * len(match[0]), normalized)
        hits = []
        for name, pattern in DOMAIN_PATTERNS.items():
            if name in matches:
                continue
            for found in pattern.finditer(normalized):
                if _NEGATED.search(normalized[:found.start()]) or normalized[found.end():].startswith('-free'):
                    continue
                hits.append((found.start(), name, passage[found.start():found.end()]))
                break
        matches.update((name, quote) for _, name, quote in sorted(hits))
    return matches


def article_domains(title, blocks):
    primary, methods = subject_blocks(blocks)
    return related_domains(title, (text for _, text in (*primary, *methods)))


def domain_only(term):
    return _DOMAIN_ONLY.fullmatch(term) is not None


def cacheable_public_abstract(source_name, key, scope, blocks_json):
    return (scope == 'abstract' and source_name in {'arxiv', 'huggingface'} and key.startswith('arxiv:')
            and len(blocks_json) <= 16_384)
