"""Curated teaching material, enabled only for its verified source revision."""
from copy import deepcopy
from urllib.parse import quote, urlencode
from .teaching_content import GUIDES as ADDITIONAL_GUIDES


GUIDES = {
    'rag-original-rag': {
        'title': 'RAG：檢索與生成各負責什麼？',
        'source_url': 'https://arxiv.org/pdf/2005.11401v4',
        'content_hash': '8686255a35bc90c298b983ca919357d0d0a67b4a2b748a601b6747b19a11a071',
        'evidence': [
            {'anchor': 'page-3', 'quote': 'RAG-Sequence model uses the same retrieved document'},
            {'anchor': 'page-3', 'quote': 'The retrieval component pη (z|x) is based on DPR'},
        ],
        'goal': '說明檢索器與生成器各負責什麼，並區分原始 RAG 的兩種機率分解。',
        'mechanism': '模型參數不易直接更新或追溯知識，因此論文把外部段落索引接到生成模型。DPR 先取候選段落，生成器依問題與候選內容估計答案；RAG-Sequence 對整段答案共用同一個潛在文件再做邊際化，RAG-Token 則在每個輸出 token 做文件邊際化。這不是「任意拼幾段文章到提示詞」的完整同義詞。',
        'example': '問「某技術何時提出、解決什麼問題？」先找出日期來源與方法說明，再比較兩者如何支持答案。把候選文件權重想成不同參考材料的相關性分配：Sequence 在整段生成機率外加總，Token 在每一步內加總。這是機率分解的說明，不是聲稱模型真的逐字切換瀏覽文件。',
        'connections': '先掌握 DPR 如何挑候選，再問生成是否忠於候選。檢索召回率與回答正確率是不同關卡；Knowledge Hub 目前的詞彙檢索並不等同 DPR。',
        'limitations': '此教材解釋方法選段，未重現訓練或評測，也不保證有引用就沒有幻覺。',
        'question': '找到正確文件卻仍答錯，應優先檢查哪個環節？',
        'answer': '先核對送入生成器的實際片段是否包含所需證據，再檢查生成的主張與引用是否一致；不能只靠提高檢索分數解釋所有錯誤。',
    },
    'rag-dpr': {
        'title': 'DPR：找到段落，還不等於回答問題',
        'source_url': 'https://aclanthology.org/2020.emnlp-main.550.pdf',
        'content_hash': 'd5879c7d0eabd805c7bf23e1d47b4ec78686d920e005490559993bb6bcd190cf',
        'evidence': [
            {'anchor': 'page-3', 'quote': 'we use two independent'},
            {'anchor': 'page-3', 'quote': 'using the dot product of their vectors'},
        ],
        'goal': '能畫出問題編碼、段落預先編碼、向量排序的流程，並說明為何它還不是回答器。',
        'mechanism': '詞彙不重合的問題與段落，仍可能語意相關。DPR 分別把問題與段落編成向量，以內積排序；段落可預先編碼，查詢時只編碼新問題並取回候選。訓練使用正負段落來調整排序，相關性的判定仍取決於資料與任務。',
        'example': '問題寫「如何降低顯存？」而文章寫「memory footprint」。先想像兩者的向量位置靠近，取回文章後再檢查是否真的解釋記憶體需求。這只是跨詞彙匹配的教學例子，不宣稱原論文驗證過中文或顯存問答。',
        'connections': 'BM25 可作詞彙基線；DPR 解決候選檢索，RAG 再把候選交給生成器。取得語意相近段落不代表取得支持答案的證據。',
        'limitations': '原論文的英語開放域問答結果不能直接外推到繁中私人筆記；PDF 雙欄文字亦可能交錯，重要細節應開原檔核對。',
        'question': '為什麼問題與段落要能分開編碼？',
        'answer': '段落向量可以預先建立與索引，避免每次查詢都讓問題和所有段落一起通過昂貴的交互模型；這個效率取捨也限制了逐對細緻互動。',
    },
    'rag-graphrag': {
        'title': 'GraphRAG：從局部事實走向整體統整',
        'source_url': 'https://arxiv.org/pdf/2404.16130v2',
        'content_hash': '211216098c3a9bdb7442fef089f5f16362f1fbcf8889d8715a7bf5906c3b830a',
        'evidence': [
            {'anchor': 'page-2', 'quote': 'generate community-level summaries'},
            {'anchor': 'page-2', 'quote': 'answers queries through map-reduce processing of community summaries'},
        ],
        'goal': '區分找局部事實與統整整批語料，說明圖、社群摘要與回答彙整的角色。',
        'mechanism': '問「整個資料集的共同主題是什麼」時，只取少量相近段落可能漏掉其他群組。這篇 GraphRAG 先抽實體關係，再建立社群及摘要；查詢時各社群摘要產生局部回答，最後彙整為全域回答。重點在跨語料綜整，不是只把搜尋結果畫成圖。',
        'example': '把一週文章分成檢索、模型記憶與互動介面三群。問「這週的方法如何讓系統記住使用者？」各群先提出自己的觀察，再比較重複與衝突之處。這是教學示例；系統尚未對你的知識庫完成該建圖流程。',
        'connections': 'RAG 先幫你理解「取回哪些材料」，這裡再問「材料是否涵蓋整體」。若只問某篇論文的一個定義，直接找到該原文可能更合適，無須把所有問題都改成全域統整。',
        'limitations': '社群摘要可能遺漏少數觀點，抽取與多層摘要可能傳遞錯誤。這是方法風險的教學推論，不是已量測的本系統結果；不能宣稱全面勝過其他檢索方法。',
        'question': '一個答案引用很多社群摘要，是否就代表它完整涵蓋原始資料？',
        'answer': '不一定。先檢查社群覆蓋、摘要是否保留例外與衝突，再回到原始文件；引用數量無法單獨證明涵蓋率。',
    },
}
GUIDES.update(ADDITIONAL_GUIDES)


def build_teaching_guide(source_id, version, item_id):
    # Daily articles have no seed-catalog ID. Resolve only explicitly curated
    # daily lessons, by exact source revision; never infer identity from a title.
    if source_id is None:
        source_id = next((sid for sid, g in GUIDES.items() if sid.startswith('daily-')
                          and g['source_url'] == version.get('source_url')
                          and g['content_hash'] == version.get('content_hash')), None)
    guide = GUIDES.get(source_id)
    if (not guide or version.get('scope') == 'unavailable'
            or version.get('content_hash') != guide['content_hash']
            or version.get('source_url') != guide['source_url']):
        return None
    evidence = []
    for cite in guide['evidence']:
        block = next((b for b in version['blocks']
                      if b['anchor'] == cite['anchor'] and cite['quote'] in b['text']), None)
        if block is None:
            return None
        evidence.append({**cite,
            'source_url': block.get('source_url') or version['source_url'],
            'reading_url': f'/items/{item_id}?' + urlencode({'version': str(version['id'])})
                           + '#' + quote(cite['anchor'], safe='')})
    return {**deepcopy(guide), 'source_id': source_id, 'item_id': item_id,
            'version_id': str(version['id']), 'evidence': evidence}


def lesson_preset(guide, journey=None):
    """An editable suggestion; persistence remains the existing explicit POST."""
    if guide is None and not (journey and journey['steps']):
        return None
    sections = [('學習目標', 'goal'), ('問題與機制', 'mechanism'),
                ('教學合成例子（非論文實驗）', 'example'), ('串連舊知', 'connections'),
                ('限制與待核對', 'limitations'), ('自我檢查', 'question'), ('參考答案', 'answer')]
    if guide:
        title, goal = guide['title'], guide['goal']
        md = f"# {title}\n\nAgent 協助策展教材；待人工核對，不代表已掌握。\n"
        md += ''.join(f'\n## {label}\n\n{guide[key]}\n' for label, key in sections)
        md += f"\n## 固定版本原文依據\n\n來源：{guide['source_url']}\n\n版本：{guide['version_id']}\n"
        for cite in guide['evidence']:
            md += f"\n- {cite['anchor']}：{cite['quote']}\n  原始檔定位：{cite['source_url']}\n"
        md += '\n短引用用於定位方法選段，不能單憑逐字匹配認定整篇教學解讀正確。\n'
    else:
        title = journey['title'][:120] + '｜背景閱讀與待核對問題'
        goal = '先補相關背景，再核對本篇的問題、方法、證據與限制。'
        md = (f"# {title}\n\n本篇尚無策展解說；以下僅是背景教材與待核對問題，不代表本篇結論。\n\n"
              f"來源：{journey['source_url']}\n\n固定版本：{journey['version_id']}；"
              f"資料範圍：{journey['scope']}（abstract 僅摘要；partial 部分解析；full 完整文字解析，圖表仍待核對）。\n\n"
              '## 閱讀本篇時待補\n\n- 它解決什麼問題？\n- 與背景方法的差異，有哪一段原文支持？\n'
              '- 評估設定、反例與不能外推的地方是什麼？\n\n我的理解：待補，尚未核對。\n')
    if journey and journey['steps']:
        md += '\n## 同頁學習路徑\n\n這是學習建議，不代表已讀或已掌握。\n'
        for step in journey['steps']:
            background = step['guide']
            md += (f"\n### {background['title']}\n\n{step['reason']}\n\n"
                   f"{background['mechanism']}\n\n自我檢查：{background['question']}\n\n"
                   f"參考答案：{background['answer']}\n\n"
                   f"固定來源：{background['source_url']}（版本 {background['version_id']}）\n")
        if journey['comparisons']:
            md += '\n## 跨篇比較（策展解讀，待核對）\n'
            for comparison in journey['comparisons']:
                md += f"\n### {comparison['title']}\n\n{comparison['text']}\n"
                for source in comparison['evidence']:
                    cite = source['evidence'][0]
                    md += (f"\n- {source['title']}，版本 {source['version_id']}，"
                           f"{cite['anchor']}：{cite['quote']}\n  {cite['source_url']}\n")
    return {'title': title, 'learning_goal': goal, 'blueprint_md': md, 'origin': 'agent_assisted',
            'description': ('已帶入本篇教學與背景路徑，可編輯後手動保存；解讀仍待核對。' if guide else
                            '已帶入背景教材與待核對問題；本篇尚無策展解說，請閱讀原文後補上理解。')}
