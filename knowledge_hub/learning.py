"""Local, read-only learning paths: curated bridges or explicit topic suggestions.

No model calls, embeddings, inferred mastery or database writes. Backgrounds
are usable only when the exact lesson revision and all evidence are present.
"""
import re

from . import teaching


PREREQUISITES = {
    'rag-original-rag': ['rag-bm25', 'rag-dpr'],
    'rag-dpr': ['rag-bm25'],
    'rag-graphrag': ['rag-original-rag', 'rag-alce'],
    'rag-bm25': [], 'rag-rrf': ['rag-bm25', 'rag-dpr'],
    'rag-alce': ['rag-original-rag'],
    'rag-react': ['rag-original-rag', 'hci-guidelines'],
    'rag-longmemeval': ['rag-original-rag', 'rag-react'],
    'mm-whisper': ['rag-alce'], 'mm-clip': ['rag-dpr'],
    'hci-guidelines': ['rag-alce'], 'sys-roofline': [],
}

HIVE_BRIDGES = [
    ('rag-original-rag', '先拆開取回與閱讀',
     'RAG 幫你區分候選材料與依據材料作答；本篇再拆開「找哪些 episode」及「每個 episode 要給什麼證據」。這是跨篇教學比較，不代表兩者架構或訓練方式相同。'),
    ('rag-graphrag', '兩篇都有圖，但圖的工作不同',
     '這篇 GraphRAG 透過社群摘要回答跨語料的全域問題；HiVe-Mem 的圖節點是記憶 episode，用來從檢索種子擴展相關互動。不能因為都有圖，就把兩者當成同一種全域統整。'),
    ('rag-longmemeval', '記憶方法與記憶評測分開看',
     'LongMemEval 提供時間、更新、拒答等評估問題；HiVe-Mem 提出多模態記憶的取回與證據選擇方法。可以借用前者的問題檢視後者，但這不表示兩篇使用相同 benchmark 或已證明所有能力。'),
    ('mm-clip', '圖文相似與歷史證據分開看',
     'CLIP 學圖文配對的表示；HiVe-Mem 管理互動歷史及每份記憶的證據粒度。找到語意相近的圖，與保留足以回答問題的原圖細節，是兩個關卡；此比較不主張 HiVe-Mem 使用 CLIP。'),
]

# Specific phrases avoid confusing DRAM memory with conversational memory.
# Match only the title, opening source text and explicit user focus, never notes.
TOPICS = [
    ('檢索與證據', r'\bretriev\w*\b|\brag\b|\bbm25\b|檢索|檢索增強',
     ['rag-original-rag', 'rag-dpr', 'rag-bm25', 'rag-rrf'],
     '先問：它改變候選的找法、排序，還是取回後的閱讀？'),
    ('圖文表示', r'\bmultimodal\b|\bcross[- ]modal\b|\bvision[- ]language\b|\bimages? and text\b|多模態|圖文',
     ['mm-clip'], '先問：相似度訊號足以支持這次要回答的細節嗎？'),
    ('跨次互動記憶', r'\blong[- ]term memor\w*\b|\bmemory episodes?\b|\binteraction histor\w*\b|長期記憶|跨日記憶',
     ['rag-longmemeval', 'rag-react'], '先問：舊事實更新、時間範圍與沒有證據時如何處理？'),
    ('行動與回饋', r'\bagentic\b|\bagents?\b|\btool[- ]use\b|工具呼叫|代理行動',
     ['rag-react', 'hci-guidelines'], '先問：哪個外部觀察改變了下一步，何時應停止？'),
    ('引用支持', r'\bcitations?\b|\battribution\b|\bgrounded answers?\b|引用|可驗證',
     ['rag-alce'], '先問：引用存在，和支持整個主張，是否被分開驗證？'),
    ('圖與全域統整', r'\bgraphrag\b|\bknowledge graphs?\b|\bcommunity summar\w*\b|知識圖譜|全域統整',
     ['rag-graphrag'], '先問：圖的節點、連線與查詢粒度分別是什麼？'),
    ('語音來源', r'\bspeech\b|\btranscri\w*\b|\bpodcasts?\b|語音|轉錄|逐字稿',
     ['mm-whisper'], '先問：字幕是否忠於原音，與摘要是否忠於字幕？'),
    ('硬體效能', r'\broofline\b|\bbandwidth\b|\boperational intensity\b|\bquantiz\w*\b|\bfp4\b|\bprecision pretrain\w*\b|頻寬|量化|運算強度',
     ['sys-roofline'], '先理解效能上界；品質、稀有事實與訓練穩定性需要另外的證據。'),
    ('人機互動', r'\bhuman[- ]ai\b|\buser control\b|\busability\b|人機互動|使用者控制',
     ['hci-guidelines'], '先問：讀者能否理解能力邊界、發現錯誤並有效修正？'),
]


def available_guides(mappings, all_items):
    """Keep the newest *verified* snapshot, without relabelling a new revision."""
    available = {}
    for mapping in sorted(mappings, key=lambda m: int(m['version_id']), reverse=True):
        sid = mapping['source_id']
        if sid in available:
            continue
        item = all_items.get(mapping['item_id'])
        version = next((v for v in item['versions'] if str(v['id']) == str(mapping['version_id'])), None) if item else None
        guide = teaching.build_teaching_guide(sid, version, item['id']) if version else None
        if guide:
            available[sid] = guide
    return available


def topic_matches(title, version, focus=''):
    # Only opening prose, no bibliography or private notes. Limit is explicit:
    # this is a topic hint, not full-text semantic understanding.
    opening = ' '.join(b['text'] for b in version.get('blocks', [])
                       if not re.search(r'(^bib[.:-]|references|bibliography)', b['anchor'], re.I))[:6000]
    matches = []
    for label, pattern, ids, prompt in TOPICS:
        for origin, text in [('指定焦點', focus), ('標題', title), ('原文開頭', opening)]:
            found = re.search(pattern, text, re.I)
            if found:
                matches.append({'label': label, 'source_ids': ids, 'prompt': prompt,
                                'reason': f'{origin}出現「{found.group(0)}」，可先補「{label}」。這是主題建議，未判定本篇採用該方法。'})
                break
    return matches


def build_journey(focal, version, mappings, all_items, focus=''):
    available = available_guides(mappings, all_items)
    guide = focal.get('teaching_guide')
    sid = guide['source_id'] if guide else None
    steps, comparisons, missing = [], [], []
    if sid == 'daily-hive-mem':
        mode = 'curated'
        for background_id, title, text in HIVE_BRIDGES:
            background = available.get(background_id)
            if not background:
                missing.append(teaching.GUIDES[background_id]['title'])
                continue
            steps.append({'source_id': background_id, 'guide': background,
                          'reason': title, 'prompt': background['question']})
            comparisons.append({'source_id': background_id, 'title': title, 'text': text,
                                'evidence': [guide, background]})
    elif sid in PREREQUISITES:
        mode = 'curated'
        for background_id in PREREQUISITES[sid]:
            background = available.get(background_id)
            if background:
                steps.append({'source_id': background_id, 'guide': background,
                              'reason': '策展先備：' + background['goal'],
                              'prompt': background['question']})
            else:
                missing.append(teaching.GUIDES[background_id]['title'])
    else:
        mode = 'suggested'
        topics = topic_matches(focal['title'], version, focus)
        seen = set()
        # Round-robin prevents one broad retrieval topic from taking all slots.
        for index in range(max((len(t['source_ids']) for t in topics), default=0)):
            for topic in topics:
                if index >= len(topic['source_ids']):
                    continue
                background_id = topic['source_ids'][index]
                if background_id in seen:
                    continue
                seen.add(background_id)
                background = available.get(background_id)
                if not background:
                    missing.append(teaching.GUIDES[background_id]['title'])
                elif background['item_id'] != focal['item_id'] and len(steps) < 4:
                    steps.append({'source_id': background_id, 'guide': background,
                                  'reason': topic['reason'], 'prompt': topic['prompt']})
        if not steps:
            mode = 'unavailable'
    return {'mode': mode, 'steps': steps, 'comparisons': comparisons, 'missing': missing,
            'published_at': version.get('published_at', ''), 'scope': version['scope'],
            'has_focal_guide': bool(guide), 'title': focal['title'],
            'source_url': version['source_url'], 'version_id': str(version['id'])}


def featured_lessons(all_items):
    """Actual available daily snapshots for the home page, no hardcoded DB IDs."""
    featured = []
    for item in all_items:
        for version in item['versions']:
            guide = teaching.build_teaching_guide(None, version, item['id'])
            if guide:
                featured.append({'guide': guide, 'published_at': version.get('published_at', '')})
    return featured
