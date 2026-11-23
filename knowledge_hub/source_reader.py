"""Local, extractive guidance over a fixed version and public background excerpts."""
from collections import Counter
from dataclasses import dataclass, replace
import re

from .excerpts import _columns, _noise, _reference_entry, sentence_spans


# Intent determines what kind of evidence can answer a question. Subject terms
# separately determine what that evidence must be about.
_FACETS = {
    "limit": (
        r"限制|侷限|局限|失效|缺點|風險|不能外推|不適用|適用範圍|(?:沒有|未)(?:驗證|評估|測試|涵蓋)|\blimitation\w*\b|\bfailure cases?\b",
        r"\blimit(?:ation\w*|ed to)|\bout of scope\b|\bfuture work\b|\b(?:we|our \w+) (?:only|did not|do not|cannot)\b|\b(?:does|did) not (?:cover|support|address)\b|\b(?:assumes?|fails? when)\b|限制|不適用|未涵蓋|未評估",
        "limitations future work fails assumptions",
    ),
    "compare": (
        r"差別|差異|不同|不一樣|相比|比起|比(?=最強|最佳|基線|[ ，,。？?]|$)|比較|相較|對照|舊方法|基線|基準方法|\b(?:differences?|compar(?:ed|e|ison)|versus|baselines?|vs)\b",
        r"\b(?:baseline\w*|compar(?:ed|es?) (?:with|to|against)|versus|against|unlike|differs?|outperforms?|lead(?:s|ing)?|led|beats?)\b|對照|相較|不同",
        "baseline compared difference against",
    ),
    "cost": (
        r"成本|開銷|資源消耗|節省資源|省資源|實時性|耗時|延遲|吞吐量|速度|顯存|峰值記憶體|記憶體用量|檢索時間|推論時間|\b(?:costs?|overhead|latency|throughput|runtime|memory (?:use|usage|footprint))\b",
        r"\b(?:latency|throughput|runtime|overhead|costs?|footprint|peak (?:memory|ram)|wall.clock time|(?:retrieval|search|ranking) (?:work|passes?|scans?|time)|(?:time|seconds?) per query|memory (?:use|usage)|(?:ms|milliseconds?|seconds?) per (?:query|sample|request)|\d[\d.]*\s*(?:Mi?B|Gi?B|MB|GB)|\d[\d.]*\s*(?:cpu|gpu)[- ]hours?)\b|耗時|資源用量|顯存|延遲|開銷",
        "latency throughput runtime overhead memory footprint CPU GPU",
    ),
    "result": (
        r"量化結果|結果|成效|表現|效果|有多好|提升|改善|下降|降低|減少|節省|降了|數字|分數|\b(?:results?|findings?|improv\w*|reduc\w*|outperform\w*)\b",
        r"\b(?:achiev\w*|outperform\w*|improv\w*|increas\w*|reduc\w*|decreas\w*|exceed\w*|obtain\w*|reach\w*|scor\w*|results? (?:show|indicate)|(?:higher|lower) than|were (?:higher|lower)|gain(?:s|ed)?|highest (?:accuracy|precision|recall|f1|auc)|(?:accuracy|precision|recall|f1|auc)\s+(?:of|is|was|rose|fell))\b|達到|實驗顯示|提升|下降|降低|減少",
        "results achieved outperforms increased reduced accuracy recall",
    ),
    "evaluation": (
        r"評估方式|評測方式|怎麼打分|如何打分|怎麼評分|如何評分|怎麼測|如何測|測試|消融|評估|評測|度量|評分|實驗|資料集|怎麼驗證|如何驗證|怎麼挑|\b(?:evaluat\w*|experiment\w*|benchmark\w*|ablation|datasets?)\b",
        r"\b(?:evalua\w*|experiments?|benchmarks?|datasets?|ablation|test(?:ed|s)? on|measur\w*|samples?|judg\w*|scoring|rated by|majority[- ]of[- ]\d+|voting)\b|消融|評估|實驗|測試",
        "evaluate experiment measured dataset benchmark ablation",
    ),
    "problem": (
        r"解決|難題|動機|卡在哪|為什麼需要|為何需要|問題|challenges?|motivations?|problems?",
        r"\b(?:challeng\w*|struggl\w*|motivat\w*|bottlenecks?|expensive|difficult|insufficient|fail\w*|lacks?|requires? significant|can lose|at odds|(?:ever[- ]growing|growing) (?:store|archive|memory|search space)|suffers?|problem\w*|(?:our|the) (?:main|primary|central) (?:objective|goal)|aim(?:s)? to address|are unable to|cannot)\b|困難|難題|動機|瓶頸|不足",
        "challenge problem motivates difficult suffers bottleneck",
    ),
    "method": (
        r"方法|機制|流程|步驟|怎麼(?:做|用|運作)|如何(?:做|用|運作)|採用|新東西|works?|mechanis\w*|methods?|pipeline",
        r"\b(?:we (?:propose|introduce|present|design|develop|contribute)|(?:our|the) (?:main|key|primary) contributions?|consists? of|is composed of|using (?:a|an|the)|(?:use|uses|apply|applies|integrates?)\s+(?:a|an|the)|compress\w*|encod\w*|cluster\w*|retriev\w*|represent\w*|train\w*|aggregat\w*|rank\w*|select\w*|partition\w*|index\w*|rout\w*|filter\w*|estimat\w*|learn\w*|map\w*|generat\w*|combin\w*|optimiz\w*|factoriz\w*|decompos\w*|resolv\w*|comput\w*|perform\w*|cach\w*|predict\w*|updat\w*|verif\w*)\b|首先|方法|機制|採用|轉換|編碼|結合",
        "method architecture uses computes pipeline consists stages",
    ),
}

_FACETS = {
    key: (re.compile(query, re.I), re.compile(evidence, re.I), expansion)
    for key, (query, evidence, expansion) in _FACETS.items()
}
_CONCEPTS = [
    (r"消融|\bablation\b", r"ablation|without|removed|w/o"),
    (r"延遲|檢索時間|推論時間|\blatency\b", r"latency|response time|time per query|wall.clock|(?:ms|milliseconds?) per query"),
    (r"吞吐量|\bthroughput\b", r"throughput|tokens per second|queries per second"),
    (r"顯存|\bvram\b", r"vram|(?:gpu|accelerator) memory"),
    (r"記憶體用量|峰值記憶體|\bmemory (?:use|usage|footprint)\b", r"memory (?:use|usage|footprint)|peak (?:ram|memory)|gb of ram|mb of ram"),
    (r"壓縮", r"compress\w*|quantiz\w*|prun\w*"),
    (r"索引", r"index\w*"),
    (r"組合|合併|分組", r"group\w*|combin\w*|merg\w*|consolidat\w*|cluster\w*"),
    (r"記錄|紀錄", r"records?|entries"),
    (r"快取", r"cach\w*"),
    (r"訓練", r"train\w*|optimiz\w*"),
    (r"檢索|\b(?:retrieval|search)\b", r"retriev\w*|search\w*"),
    (r"線上更[新動]", r"online updat\w*|incremental updat\w*|online learning"),
    (r"記憶更新", r"memory updates?|updat\w* (?:the )?memor\w*|memory is updated"),
    (r"共享記憶|共用記憶", r"shared memor\w*|share\w* (?:the |a )?memor\w*|memory shar\w*|memory is shared"),
    (r"多代理|多智能體", r"multi[- ]agent|multiple agents"),
    (r"長上下文", r"long[- ]context|context length|long sequence"),
    (r"記憶體", r"memory|ram|vram"),
    (r"推論|推理", r"inference|forward pass|per query|per token"),
]
_CONCEPTS = [(re.compile(zh), re.compile(r"\b(?:" + en + r")\b|" + zh, re.I))
             for zh, en in _CONCEPTS]
_QUERY_STOP = frozenset("""
    about all among and any are as at be based been being by can could did do does each explain
    for from given had has have how in into is it its me much many of one on or our please
    paper point points step steps stage stages versus vs protocol s show some tell than that the their them then there these they this those
    to using used use uses was we were what when where which who why will with would you your
""".split())
_ZH_FILLER = re.compile(
    r"這篇(?:論文|文章|研究)?|這個(?:研究|方法|模型)?|這項(?:研究|方法)?|那篇|那個|"
    r"作者們?|研究者|本篇|論文|研究|我想知道|提到|說到|提及|指出|目前|原始|舊版|舊的|新版|最新|修改|版本|之間|元件|模型|哪一步|哪個步驟|哪個階段|能不能|可不可以|有沒有|還有|"
    r"為什麼|怎麼樣|什麼|哪一些|哪些|哪種|哪個|哪裡|如何|怎麼|怎樣|"
    r"多少|幾(?:個|種|項|倍|成)?|有多|用了|做了|跑了|使用|透過|最?主要|核心(?=\s*(?:是|為|$))|具體|明確|提到|驗證|支援|"
    r"有關|原文|這樣|真的|可能|是否|怎|用|以|和|跟|與|及|或|對|在|於|把|"
    r"嗎|呢|啊|吧|呀|的|了|有|是|會|能|其|它們?|那|該|請"
)
_WORD = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", re.I)
_HAN = re.compile(r"[\u3400-\u9fff]{2,}")
_NUMBER = re.compile(
    r"(?<![\w.])(?:\d+(?:[.,]\d+)?\s*(?:%|×|x\b|ms\b|[kmgt]i?b\b|"
    r"(?:(?:milli|micro|nano)?seconds?|s|μs|us)\b|minutes?\b|hours?\b|tokens?\b|points?\b|f1\b|auc\b|bleu\b|samples?\b|examples?\b|"
    r"tasks?\b|datasets?\b|benchmarks?\b|epochs?\b|layers?\b|倍|個百分點|秒)|"
    r"(?:recall|precision|accuracy|f1|auc)\s*(?:of\s+|=\s*)\d+(?:\.\d+)?)",
    re.I,
)
_QUANTIFIED = re.compile(r"多少|幾(?:個|種|項|倍|成)?|量化|數字|\bhow (?:many|much|fast)\b|\bpercent\b", re.I)
_CHANGE = re.compile(
    r"降低|減少|省資源|節省|提升|下降|更[省快慢高低穩]|improv\w*|reduc\w*|decreas\w*|increas\w*", re.I
)
_SETUP_ZH = re.compile(
    r"評估方式|評測方式|評分方式|度量方式|(?:如何|怎麼)(?:打分|評分|評估|評測|測|驗證|挑)|用什麼資料集",
)
_SETUP_EN = re.compile(r"\bhow\b.{0,120}?\b(?:evaluat\w*|scor\w*|judg\w*)\b", re.I)
_SETUP_TERMS_EN = re.compile(r"\b(?:evaluat\w*|scor\w*|judg\w*)\b", re.I)
_FOLLOW = re.compile(r"那(?:麼|这个|這個|它|他|其|剛才)|它(?:們|的)?|前一題|剛才|(?:^|\W)(?:it|its|that method|those results)(?:$|\W)", re.I)
_WEAK = {"data", "evaluation", "experiment", "method", "model", "novel",
         "paper", "result", "study", "system", "task", "value", "work", "use"}


_METRICS = {
    "準確率": re.compile(r"準確率|正確率|\baccurac(?:y|ies)\b", re.I),
    "精確率": re.compile(r"精確率|精準率|\bprecision\b", re.I),
    "召回率": re.compile(r"召回率|召回|\brecall\b", re.I),
    "完全比對": re.compile(r"完全比對|\bexact[- ]match\b", re.I),
    "F1": re.compile(r"\bf1(?:[- ]score)?\b", re.I),
    "AUC": re.compile(r"\bauc(?:[- ]roc)?\b", re.I),
}
_PERCENT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*%")
_RESULT_SPLIT = re.compile(
    r"\b(?:whereas|while|but|however|and (?=(?:(?:its|the) +)?(?:accuracy|precision|recall|f1|auc)))\b"
    r"|[;；]|但是|而(?:準確率|精確率|召回率)", re.I,
)
_POINT_REQUEST = re.compile(r"百分點|\b(?:percentage )?points\b", re.I)
_STRONG_BASELINE = re.compile(
    r"(?:最強|最佳)(?:的)?(?:基線|基準(?:方法)?)|\b(?:strongest|best|top[- ]performing)\b.{0,25}\bbaseline\b",
    re.I,
)
_GAP = re.compile(
    r"\b(?P<verb>lead(?:s|ing)?|led|beat(?:s|ing)?|outperform\w*|exceed\w*|surpass\w*|trail\w*|lag\w*) "
    r"(?P<baseline>[^,;!?。]{1,100}?)\s+by\s+(?P<gap>\d+(?:\.\d+)?)\s+(?:percentage )?points?\b",
    re.I,
)
_POINTS_ABOVE = re.compile(
    r"(?<![\w.])(?P<gap>\d+(?:\.\d+)?)\s+(?:percentage )?points?\s+"
    r"(?P<verb>above|below|over|under|higher than|lower than)\s+(?P<baseline>[^,;.!?。]{1,100})",
    re.I,
)
_SCORE_ON_DATASET = re.compile(
    r"(?P<value>(?<![\w.])\d+(?:\.\d+)?\s*%)\s+(?:on|for|in)\s+(?:the\s+)?"
    r"(?P<dataset>[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*)", re.I,
)
_SUBJECT_CHANGE = re.compile(
    r"^(?:unlike|compared (?:with|to)|in contrast to)\b[^,]+,\s*"
    r"|[;；]|\b(?:while|whereas|but|(?:reports?|notes?|states?) that)\b", re.I,
)
_REDUCTION = re.compile(r"降低|減少|節省|更省|\b(?:reduc\w*|decreas\w*|lower\w*|cuts?|sav\w*|av[o]id\w*)\b", re.I)
_METRIC_SETUP = re.compile(r"\b(?:we (?:measure|report|use|compute)|evaluat\w*|metric\w*|scor\w*)\b|衡量|評估|指標", re.I)

_RECORD_VERB = r"(?:re-?writ\w*|overwrit\w*|modif\w*|alter\w*|chang\w*|updat\w*|edit\w*|mutat\w*)"
_RECORD_OBJECT = r"(?:(?:any|the|its|our|all|every|each|existing|original|prior|underlying|source|raw|old)\s+){0,5}(?:records?|rows?|entries)\b"
_RECORD_QUERY = re.compile(
    r"(?:(?:會不會|是否|會|能否|能不能)\s*)?(?:動到|改動|改寫|重寫|覆寫|修改|更動|變更|更新|改)"
    r"\s*(?:(?:原本|原有|原始|既有|現有|舊的)的?\s*)?(?:紀錄|記錄|資料列|資料紀錄)"
    rf"|\b{_RECORD_VERB}\s+{_RECORD_OBJECT}", re.I,
)
_RECORD_DECISION = re.compile(
    rf"\b(?P<neg>without|never|(?:does|do|did|will) not|doesn['’]t|don['’]t|didn['’]t|cannot|can['’]t)\s+"
    rf"(?P<denied>{_RECORD_VERB})\s+{_RECORD_OBJECT}"
    rf"|\b(?P<positive>{_RECORD_VERB})\s+(?:the\s+)?(?:original|existing|raw|source|prior)\s+(?:records?|rows?|entries)\b"
    r"|(?P<zh>(?:不會|不|沒有|未曾)?(?:改寫|重寫|覆寫|改動|修改|更動)(?:原本的?|原有的?|原始的?|既有的?)(?:紀錄|記錄|資料列))",
    re.I,
)
_RECORD_PRESERVE = re.compile(
    rf"\b(?:preserv\w* { _RECORD_OBJECT }|(?:leav\w*|keep\w*) {_RECORD_OBJECT}\s+(?:intact|unchanged|unmodified))",
    re.I,
)

_NAME_ATOM = r"[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*|[\u3400-\u9fff]{1,16}"
_NAME = rf"(?:[「『《\"“][^」』》\"”\n]{{1,48}}[」』》\"”]|{_NAME_ATOM})"
_NAMED_PAIR = re.compile(
    rf"(?P<first>{_NAME})\s*(?:和|與|跟|及|、|\band\b|\bvs\.?\b)\s*"
    rf"(?P<second>{_NAME})\s*(?:各自?|分別|\beach\b)", re.I,
)
_LOCUS = re.compile(r"(?<=[A-Za-z0-9\u3400-\u9fff])\s*(?:在|於|針對|\bon\b|\bin\b)\s*(?=[A-Za-z0-9])", re.I)
_EVALUATION_PROTOCOL = re.compile(
    r"\b(?:judg(?:e|es|ed|ing|ment)|evaluator|voting|majority[- ]of[- ]\d+|scoring|rated by"
    r"|measur\w*|test(?:ed|s)? on|evaluat\w* (?:on|using|with|by)|dataset(?:s)? (?:contain|include|consist)s?)\b"
    r"|評分器|評分模型|投票|多數決|採用.{0,24}(?:指標|評分)|以.{0,24}(?:評估|評分|測試)", re.I,
)
_ROADMAP = re.compile(
    r"^(?:(?:it|we|this (?:section|paper)) (?:states?|outlines?|summarizes?) (?:the|our) (?:overall )?(?:problem|requirements?)"
    r"|(?:section|chapter) [\dA-Z.]+ (?:describes?|presents?|details?|covers?))\b", re.I,
)
_PRIOR_ART = re.compile(r"^(?:later|earlier|prior|previous|existing) (?:systems?|methods?|approaches?|studies|work)\b", re.I)
_CORE_PROBLEM = re.compile(
    r"\b(?:our|the|this (?:paper|work)'s) (?:main |central |primary |key )?(?:challenge|problem|objective|goal) (?:is|lies|are)\b"
    r"|\b(?:this (?:paper|work)|we) (?:address(?:es)?|aim(?:s)? to solve)\b|核心問題|主要挑戰", re.I,
)
_CORE_METHOD = re.compile(
    r"\b(?:we (?:introduce|propose|present|develop|design|contribute)|our (?:main |key |primary )?contributions?"
    r"|this (?:paper|work) (?:introduces|proposes|presents))\b|本文(?:提出|設計)|主要貢獻", re.I,
)
_INLINE_CAPTION = re.compile(
    r"\b(?:Figure|Fig\.|Table)\s+[A-Z]?[0-9]+(?:\.[0-9]+)*(?:\s*[:：]|\s+(?=[A-Z][a-z]))",
)
_SIDE_NOTE = re.compile(
    r"\b(?:debug\w*|diagnostic wrapper|fallback wrapper|sanity check|logging|implementation detail)\b|除錯|診斷",
    re.I,
)

_FACETS["records"] = (_RECORD_QUERY, _RECORD_DECISION, "preserve rewrite overwrite original existing source records rows entries")

def _word_form(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _words(text):
    return frozenset(_word_form(x) for x in _WORD.findall(text.casefold()) if x not in _QUERY_STOP)


@dataclass(frozen=True)
class _Question:
    facet: str
    words: frozenset
    phrases: tuple
    concepts: tuple
    numeric: bool
    comparison_facet: str = ""
    change: bool = False
    metrics: tuple = ()
    point_gap: bool = False
    strongest: bool = False
    subjects: tuple = ()
    reduction: bool = False
    protocol: bool = False


def _parse(question, parent=None):
    question = question.strip()
    concepts = tuple(pattern for zh, pattern in _CONCEPTS if zh.search(question))
    metrics = tuple(name for name, pattern in _METRICS.items() if pattern.search(question))
    point_gap = bool(_POINT_REQUEST.search(question))
    strongest = bool(_STRONG_BASELINE.search(question))
    numeric = bool(_QUANTIFIED.search(question) or point_gap)
    protocol = bool(_SETUP_ZH.search(question) or _SETUP_EN.search(question))
    facets = {key for key, (query, _, _) in _FACETS.items() if query.search(question)}
    if metrics and not protocol:
        facets.add("result")
    subjects = ()
    remaining = question
    if names := _NAMED_PAIR.search(question):
        subjects = tuple(re.sub(r'^(?:模型|方法|系統)', '', name.strip('「」『』《》"“”'))
                         for name in names.groups())
        if all(subjects) and subjects[0].casefold() != subjects[1].casefold():
            facets.add("compare")
            remaining = question[:names.start()] + " " + question[names.end():]
        else:
            subjects = ()
    if "records" in facets and not subjects:
        facet = "records"
    elif "limit" not in facets and not subjects and protocol:
        facet = "evaluation"
    else:
        facet = next((key for key in _FACETS if key in facets), "free")
    comparison_facet = next((key for key in ("cost", "result", "evaluation", "method")
                             if key in facets), "") if facet == "compare" else ""
    if subjects and not comparison_facet:
        comparison_facet = "method"
    # Each of these forms describes the question, rather than an additional
    # subject word to locate in the article verbatim.
    remaining = _STRONG_BASELINE.sub(" ", remaining)
    remaining = _POINT_REQUEST.sub(" ", remaining)
    remaining = _SETUP_ZH.sub(" ", remaining)
    if protocol:
        remaining = _SETUP_TERMS_EN.sub(" ", remaining)
    if point_gap:
        remaining = re.sub(r"\b(?:above|below|higher|lower|ahead|behind)\b", " ", remaining, flags=re.I)
    remaining = re.sub(r"比(?=\s)|好(?=幾|多少|\s)|高(?=幾|多少|\s)|低(?=幾|多少|\s)", " ", remaining)
    for query, _, _ in _FACETS.values():
        remaining = query.sub(" ", remaining)
    for pattern in _METRICS.values():
        remaining = pattern.sub(" ", remaining)
    for zh, _ in _CONCEPTS:
        remaining = zh.sub(" ", remaining)
    remaining = _ZH_FILLER.sub(" ", remaining)
    words = _words(remaining)
    phrases = tuple(dict.fromkeys(_HAN.findall(remaining)))
    if parent and _FOLLOW.search(question) and not (words or phrases or subjects):
        earlier_prompt = parent.get("prompt", "") if isinstance(parent, dict) else parent
        if isinstance(earlier_prompt, str) and earlier_prompt:
            earlier = _parse(earlier_prompt)
            # New intent can retain the referent without requiring the earlier
            # benchmark or measure in an otherwise unrelated evidence sentence.
            if facet != "free" and facet != earlier.facet and (locus := _LOCUS.search(earlier_prompt)):
                earlier_subject = _parse(earlier_prompt[:locus.start()])
            else:
                earlier_subject = earlier
            words, phrases = earlier_subject.words, earlier_subject.phrases
            if facet == "free":
                facet, comparison_facet = earlier.facet, earlier.comparison_facet
                subjects = earlier.subjects
            if facet == earlier.facet and not metrics and not concepts:
                metrics, concepts = earlier.metrics, earlier.concepts
    return _Question(
        facet, words, phrases, concepts, numeric, comparison_facet,
        bool(_CHANGE.search(question) or _REDUCTION.search(question)),
        metrics, point_gap, strongest, subjects, bool(_REDUCTION.search(question)), protocol,
    )


def retrieval_query(question, parent=None):
    """Terms for public_context selection; hand the original question to ask."""
    query = _parse(question, parent)
    expansion = _FACETS[query.facet][2] if query.facet != "free" else ""
    secondary = _FACETS[query.comparison_facet][2] if query.comparison_facet else ""
    concept_words = sorted({word for pattern in query.concepts for word in
                            _WORD.findall(pattern.pattern.replace(r"\b", "").replace(r"\w*", ""))
                            if len(word) > 2})
    measure_terms = {"準確率": "accuracy", "精確率": "precision", "召回率": "recall",
                     "完全比對": "exact match", "F1": "f1", "AUC": "auc"}
    return " ".join([question, *query.subjects, *sorted(query.words), *query.phrases, expansion,
                     secondary, *concept_words, *(measure_terms[metric] for metric in query.metrics)]).strip()


@dataclass(frozen=True)
class _Passage:
    source: dict
    excerpt: dict
    text: str
    terms: frozenset
    previous: "_Passage | None" = None
    region: int = 0

    def cite(self):
        return {
            "source_id": self.source["id"], "item_id": self.source["item_id"],
            "version_id": self.source["version_id"], "anchor": self.excerpt["anchor"],
            "quote": self.text, "reading_url": self.excerpt["reading_url"],
            "source_url": self.excerpt["source_url"],
        }


def _passages(source):
    for excerpt in source["excerpts"]:
        raw = excerpt["quote"].strip()
        if (excerpt.get("quality") in {"fragment", "columns", "caption"}
                or "雙欄" in excerpt.get("quality_note", "")
                or _columns(raw) or _noise(raw) or _reference_entry(raw)):
            continue
        sentences = list(sentence_spans(raw))
        previous, region = None, 0
        for index, (start, end, complete) in enumerate(sentences):
            text = raw[start:end]
            if not complete and (index > 0 or len(text) < 32):
                previous = None
                continue
            if (re.match(r"^(?:Table|Fig(?:ure)?\.|Figure|表|圖)\s*\d", text, re.I)
                    or _INLINE_CAPTION.search(text)):
                previous = None
                region += 1
                continue
            if not 24 <= len(text) <= 700:
                previous = None
                continue
            passage = _Passage(source, excerpt, text, _words(text), previous, region)
            yield passage
            previous = replace(passage, previous=None) if complete else None


def _scope(passage):
    role = passage.excerpt.get("section_role")
    if role and role != "other":
        return role
    anchor = passage.excerpt["anchor"].casefold()
    if "abstract" in anchor:
        return "abstract"
    if re.search(r"intro|^s1(?:[.-]|$)|^page-[12]$", anchor):
        return "introduction"
    if "appendix" in anchor:
        return "appendix"
    return ""


def _core_rank(passage, facet):
    explicit = _CORE_PROBLEM if facet == "problem" else _CORE_METHOD
    scope = _scope(passage)
    early = 3 if scope in {"abstract", "introduction"} else 0
    focus = 6 if explicit.search(passage.text) else 0
    peripheral = 4 if scope in {"appendix", "related", "limitations"} or _SIDE_NOTE.search(passage.text) else 0
    return early + focus - peripheral


def _named_metrics(text):
    return tuple(name for name, pattern in _METRICS.items() if pattern.search(text))


def _previous_measure(passage, query):
    previous = passage.previous
    if (previous and query.words and query.words <= previous.terms
            and _named_metrics(previous.text) == query.metrics
            and _METRIC_SETUP.search(previous.text) and not _NUMBER.search(previous.text)):
        return previous
    return None


def _result_span(passage, query):
    """Pair the requested benchmark, metric and change inside one claim."""
    for span in _RESULT_SPLIT.split(passage.text):
        if not query.words <= _words(span):
            continue
        metrics = _named_metrics(span)
        previous = None
        if query.metrics and metrics != query.metrics:
            if metrics or not (previous := _previous_measure(passage, query)):
                continue
        if not _FACETS["result"][1].search(span):
            continue
        gap = (_GAP.search(span) or _POINTS_ABOVE.search(span)) if query.point_gap or query.strongest else None
        if ((query.point_gap and not gap)
                or (query.strongest and (not gap or not _STRONG_BASELINE.search(gap["baseline"])))):
            continue
        value = span[:gap.start()] if gap else span
        outcomes = list(_SCORE_ON_DATASET.finditer(value))
        matching = [o for o in outcomes if _words(o["dataset"]) & query.words]
        if outcomes and not matching and _dataset_mention(span, query.words):
            continue
        if len(outcomes) > 1:
            if len(matching) != 1 or gap and matching[0] is not outcomes[-1]:
                continue
            value = matching[0]["value"]
        if query.numeric and (not _NUMBER.search(value)
                or any(metric.endswith("率") for metric in query.metrics) and not _PERCENT.search(value)):
            continue
        return span, value, previous, gap
    return None


def _record_evidence(text, subjects=frozenset()):
    def attributed(match):
        prefix = _SUBJECT_CHANGE.split(text[:match.start()])[-1]
        return subjects <= _words(prefix)

    for match in _RECORD_DECISION.finditer(text):
        if not attributed(match):
            continue
        if match["positive"] and re.search(r"\b(?:without|not|never)\s+(?:[A-Za-z]+\s+){0,2}$",
                                            text[max(0, match.start() - 28):match.start()], re.I):
            continue
        return match, bool(match["neg"] or match["zh"] and match["zh"].startswith(("不", "沒有", "未曾")))
    if (match := _RECORD_PRESERVE.search(text)) and attributed(match):
        lead = text[max(0, match.start() - 30):match.start()]
        if not re.search(r"\b(?:without|not|never|fails? to)\s+(?:[A-Za-z]+\s+){0,2}$", lead, re.I):
            return match, True
    return None


def _score(passage, query):
    text = passage.text
    facet = query.facet
    matches = query.words & passage.terms
    if (matches != query.words or any(p not in text for p in query.phrases)
            or any(not p.search(text) for p in query.concepts)):
        return 0
    if facet == "free":
        return 3 * len(matches) + 4 * len(query.concepts) + 4 * len(query.phrases)
    if facet == "records":
        return 3 + len(matches) if _record_evidence(text, query.words) else 0
    if facet in {"problem", "method"} and not (query.words or query.phrases):
        if (_scope(passage) == "related" or _ROADMAP.search(text)
                or facet == "method" and _PRIOR_ART.search(text)):
            return 0
    cues = _FACETS[facet][1].findall(text)
    has_quantity = bool(_NUMBER.search(text))
    if not cues or query.numeric and not has_quantity:
        return 0
    if (query.change and facet in {"cost", "result"}
            and not (_FACETS["result"][1].search(text) or _REDUCTION.search(text))):
        return 0
    if query.reduction and facet == "cost" and not _REDUCTION.search(text):
        return 0
    result = None
    if facet == "result" and (query.metrics or query.point_gap or query.strongest):
        result = _result_span(passage, query)
        if result is None:
            return 0
    if facet == "evaluation" and query.protocol and not _EVALUATION_PROTOCOL.search(text):
        return 0
    if query.metrics and facet in {"cost", "evaluation"} and _named_metrics(text) != query.metrics:
        return 0
    if query.comparison_facet and not _score(passage, replace(query, facet=query.comparison_facet,
                                                               comparison_facet="")):
        return 0
    strength = 2 + min(len(cues), 4) + len(matches) * 3
    if facet in {"result", "cost", "evaluation"} and has_quantity:
        strength += 3
    if facet == "evaluation" and query.protocol and (_JUDGE.search(text) or _VOTING.search(text)):
        strength += 5
    if facet in {"method", "problem"} and not (query.words or query.phrases):
        strength += _core_rank(passage, facet)
    if result and query.numeric and not query.change:
        if _FROM_TO.search(result[1]):
            strength -= 5
        elif len(_PERCENT.findall(result[1])) == 1:
            strength += 2
    if facet == "method" and _FACETS["result"][1].search(text):
        strength -= 2
    if facet == "method" and _FACETS["cost"][1].search(text):
        strength -= 2
    if facet in {"cost", "result"} and _FACETS["limit"][1].search(text):
        strength -= 1
    return strength


def _best(passages, query):
    ranked = ((score, passage) for passage in passages if (score := _score(passage, query)) > 0)
    return max(ranked, key=lambda pair: pair[0], default=(0, None))[1]


def _facet(name, *, protocol=False):
    return _Question(name, frozenset(), (), (), False, protocol=protocol)


_TITLES = {
    "problem": "作者要處理的問題", "method": "作者怎麼做",
    "result": "作者報告的結果", "evaluation": "實驗是怎麼做的",
    "cost": "原文中的資源用量", "limit": "適用條件與限制",
    "compare": "論文中的對照", "free": "與問題直接相關的原文",
    "records": "原文交代的紀錄變更",
}
_GUIDANCE = {
    "problem": "先看這個困難影響的對象與情境，再對照後文的方法是否直接處理它。",
    "method": "沿原文的動作順序讀，把操作對象、使用的手段與產生的結果各自找出來。",
    "result": "把原文的數字和對應指標、資料集及條件一起讀。只有引用中明示的比較才有依據。",
    "evaluation": "先找評估對象與衡量方式，再用結果段核對這項設定支持哪個結論。",
    "cost": "以這段列出的硬體、單位與工作量為準。更換硬體、批次或輸入長度時，需要另查測量結果。",
    "limit": "將這項限制當成適用條件。若你的資料或設定超出它的範圍，回到原文找額外驗證。",
    "compare": "這段提供作者報告的對照。讀清楚比較對象、同一項指標和它成立的條件。",
    "free": "引文直接包含問題所指的詞。請核對它在句中的關係和條件，再決定是否足以回答整個問題。",
    "records": "依這句描述的操作與條件解讀紀錄是否保留。",
}


def _section(role, kind, title, text, *passages):
    return {"role": role, "kind": kind, "title": title, "text": text,
            "citations": [p.cite() for p in passages]}


# Describe the actions that actually appear in the sentence, retaining its
# wording and qualifiers next to the short Chinese labels.
_OPERATIONS = [(re.compile(pattern, re.I), label) for pattern, label in (
    (r"\bcompress\w*\b|壓縮", "壓縮"), (r"\bverif\w*\b|驗證", "驗證"),
    (r"\bencod\w*\b|編碼", "編碼"), (r"\bretriev(?:e|es|ed|ing)\b|檢索", "檢索"),
    (r"\baggregat\w*\b|聚合", "聚合"), (r"\bpartition\w*\b|切分", "切分"),
    (r"\brank\w*\b|排序", "排序"), (r"\btrain\w*\b|訓練", "訓練"),
    (r"\bupdat\w*\b|更新", "更新"), (r"\bestimat\w*\b|估計", "估計"),
    (r"\bcombines?\b|結合", "結合"), (r"\bdecompos\w*\b|分解", "分解"),
    (r"\b(?:merg\w*|consolidat\w*)\b|合併", "合併"),
    (r"\b(?:prun\w*|dropping)\b|剪除", "剪除"),
    (r"\b(?:cach\w*|memoiz\w*)\b|快取", "快取"),
    (r"\b(?:filter\w*|screen\w*)\b|篩選", "篩選"),
    (r"\bindex(?:es|ed|ing)\b|建立索引", "建立索引"),
    (r"\b(?:keeps?|retains?|stores?) (?:the )?(?:complete|original|source|raw) records\b|保留紀錄", "保留紀錄"),
    (r"\bcites? (?:the |their |its )?(?:sources?|rows?)\b|回指來源", "回指來源"),
    (r"\b(?:resolv(?:e|es|ed|ing)|match(?:es|ed|ing))\b|匹配", "匹配"),
    (r"\btravers(?:e|es|ed|ing)\b|遍歷", "遍歷"),
)]


_JUDGE = re.compile(
    r"\b(?:uses?|using|with)\s+(?:(?:a|an|the)\s+)?([A-Za-z][\w.-]{1,60})\s+(?:judge|evaluator)\b", re.I,
)
_VOTING = re.compile(r"\bmajority[- ]of[- ]([0-9]+)\b", re.I)
_FROM_TO = re.compile(r"\bfrom\s+(\d+(?:\.\d+)?\s*%)\s+to\s+(\d+(?:\.\d+)?\s*%)", re.I)
_STAGE = (
    (re.compile(r"\b(?:at|during|in) (?:the )?routing\b", re.I), "路由時"),
    (re.compile(r"\bbefore (?:the )?re[- ]?ranking\b", re.I), "重新排序前"),
    (re.compile(r"\b(?:at|during|in) (?:the )?indexing\b", re.I), "建立索引時"),
    (re.compile(r"\b(?:at|during|in) (?:the )?(?:retriev\w*|search)\b", re.I), "檢索時"),
)


def _actions(text):
    found = [(match.start(), name) for rule, name in _OPERATIONS if (match := rule.search(text))]
    return "、".join(dict.fromkeys(name for _, name in sorted(found)))


def _dataset_mention(text, terms):
    # Recover the spelling from the evidence, not the normalized question.
    return next((match[1] for match in re.finditer(
        r"\b(?:on|for|in)\s+(?:the\s+)?([A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*)", text, re.I,
    ) if _words(match[1]) and _words(match[1]) <= terms), "")


def _measurement(passage, query):
    if not query or not (query.metrics or query.point_gap):
        return "", None
    evidence = _result_span(passage, query)
    if not evidence:
        return "", None
    span, value, previous, gap = evidence
    dataset = _dataset_mention(span, query.words)
    context = f"在 {dataset}，" if dataset else ""
    metric = "、".join(query.metrics) if query.metrics else "原文報告的分數"
    percentages = _PERCENT.findall(value)
    if movement := _FROM_TO.search(value):
        result = f"{context}{metric}從 {movement[1]} 變為 {movement[2]}。"
    elif len(percentages) == 1:
        result = f"{context}{metric}為 {percentages[0]}。"
    else:
        result = ""
    if gap:
        baseline = "最強基線" if _STRONG_BASELINE.search(gap["baseline"]) else f"「{gap['baseline']}」"
        direction = "低" if gap["verb"].casefold().startswith(("trail", "lag", "below", "under", "lower")) else "高"
        unit = "個百分點" if percentages else "分"
        result += f"比{baseline}{direction} {gap['gap']} {unit}。"
    return result, previous


def _record_summary(passage, query):
    match, preserved = _record_evidence(passage.text, query.words if query else frozenset())
    verb = (match.groupdict().get("denied") or match.groupdict().get("positive")
            or match.groupdict().get("zh") or "").casefold()
    if "overwrit" in verb or "覆寫" in verb:
        action = "覆寫"
    elif re.search(r"re-?writ|改寫|重寫", verb):
        action = "重寫"
    else:
        action = "更動"
    before = passage.text[:match.start()]
    operation = _actions(before)
    scope = f"就原文的{operation}操作，" if operation else "在原文描述的條件下，"
    return f"{scope}既有紀錄不會被{action}。" if preserved else f"{scope}會{action}既有紀錄。"


def _evaluation_summary(text):
    judge, votes = _JUDGE.search(text), _VOTING.search(text)
    if judge and votes:
        return f"原文使用 {judge[1]} 評分，並以 {votes[1]} 次投票的多數決定結果。"
    if votes:
        return f"原文交代使用 {votes[1]} 次投票的多數決評分。"
    if judge:
        return f"原文指定 {judge[1]} 為評分器。"
    return ""


def _reported(passage, facet, query=None):
    detail, support = "", None
    if facet == "records":
        detail = _record_summary(passage, query)
    elif facet == "method":
        actions = _actions(passage.text)
        if _CORE_METHOD.search(passage.text):
            detail = "這是作者提出本篇做法的句子。"
        if actions:
            detail += f"原文依序描述{actions}。"
    elif facet == "problem" and _CORE_PROBLEM.search(passage.text):
        detail = "作者在這句界定本篇的核心問題。"
    elif facet == "result":
        detail, support = _measurement(passage, query)
    elif facet == "evaluation":
        detail = _evaluation_summary(passage.text)
    if support:
        detail += f"相鄰原句界定指標「{support.text}」"
    return f"{detail}原文「{passage.text}」", support


def _explain(passage, facet, role="answer", query=None):
    kind = "evidence" if facet in {"cost", "result", "compare", "evaluation"} else "author_claim"
    reported, support = _reported(passage, facet, query)
    citations = (support, passage) if support else (passage,)
    return _section(role, kind, _TITLES[facet], reported + " " + _GUIDANCE[facet], *citations)


def _name_requirements(name):
    terms = _words(name)
    return (terms, ()) if terms else (frozenset(), (name,))


def _named_comparison(passages, query):
    names = query.subjects
    sides = [_name_requirements(name) for name in names]
    chosen = []
    facet = query.comparison_facet
    for (words, phrases), (other_words, other_phrases) in zip(sides, reversed(sides)):
        # A source mentioning both names in passing does not independently
        # establish which action belongs to each method.
        side = replace(query, facet=facet, words=query.words | words,
                       phrases=query.phrases + phrases, comparison_facet="", subjects=())
        candidates = [
            p for p in passages
            if not ((other_words and other_words <= p.terms)
                    or any(phrase in p.text for phrase in other_phrases))
        ]
        result = _best(candidates, side)
        if result is None:
            return None
        chosen.append(result)
    descriptions = []
    for name, passage in zip(names, chosen):
        stage = next((label for rule, label in _STAGE if rule.search(passage.text)), "")
        actions = _actions(passage.text)
        detail = stage
        detail += f"採用{actions}" if actions else ""
        lead = f"{name} {detail}。" if detail else f"{name} 的原文。"
        descriptions.append(f"{lead}「{passage.text}」")
    return _section(
        "comparison", "inference", "兩種方法各自的原文",
        " ".join(descriptions) + " 兩段各有自己的操作與適用條件。",
        *chosen,
    )


def _comparison(focal, backgrounds, query):
    if not focal or not backgrounds:
        return None
    candidates = []
    focus_facet = query.comparison_facet
    focus_query = replace(query, facet=focus_facet, words=frozenset(), phrases=(),
                          comparison_facet="", change=False, reduction=False)
    frequency = Counter(word for p in focal + backgrounds for word in p.terms - _WEAK)
    for background in backgrounds:
        title_terms = _words(background.source["title"])
        if query.words and not query.words & (title_terms | background.terms):
            continue
        background_score = _score(background, focus_query) if focus_facet else 0
        if focus_facet and not background_score:
            continue
        for target in focal:
            if _scope(target) == "related" or _PRIOR_ART.search(target.text) or _ROADMAP.search(target.text):
                continue
            shared = (target.terms & background.terms) - _WEAK
            named = query.words & title_terms
            subject = target.terms | background.terms
            if (not shared and not named or not query.words <= subject | title_terms
                    or any(q not in target.text and q not in background.text for q in query.phrases)
                    or any(not (p.search(target.text) or p.search(background.text)) for p in query.concepts)):
                continue
            exact_pair = bool(named and named <= target.terms and _score(target, _facet("compare")))
            if query.numeric and not exact_pair:
                continue
            if focus_facet:
                target_score = _score(target, replace(focus_query, change=query.change, reduction=query.reduction))
                if not target_score:
                    continue
                evidence_score = target_score + background_score
            else:
                first = _score(target, _facet("method"))
                second = _score(background, _facet("method"))
                if not exact_pair and not (first and second):
                    continue
                evidence_score = first + second
            relevance = sum(1 / frequency[word] for word in shared)
            candidates.append((6 * exact_pair + evidence_score + relevance, shared, target, background))
    if not candidates:
        return None
    _, shared, target, background = max(candidates, key=lambda row: row[0])
    keys = sorted(shared, key=lambda word: (frequency[word], word))[:3]
    focus = "交集出現在 " + "、".join(f"「{word}」" for word in keys) + "。" if keys else ""
    return _section(
        "comparison", "inference", "本篇與背景原文的比較起點",
        f"本篇寫道「{target.text}」 背景《{background.source['title']}》寫道「{background.text}」 "
        + focus + "先核對兩段各自的任務與條件。若資料、設定或指標不同，需按各自的實驗條件解讀數字。",
        target, background,
    )


def _limits(context):
    sources = context.get("sources", [])
    scope = sources[0].get("scope") if sources else None
    covered = {"abstract": "目前本篇只有摘要", "partial": "目前本篇只有部分原文",
               "full": "目前只查核已擷取的原文片段"}.get(scope, "目前缺少本篇可核對的原文")
    return covered + "。本機依問題詞語與原句線索選段。圖表、公式與未擷取的證據須回固定版本核對。"


def ask(context, question, parent=None):
    """Rank public excerpts for a question and emit only fixed-source citations."""
    sources = context.get("sources", [])
    query = _parse(question, parent)
    limits = _limits(context)
    if sources:
        focal = list(_passages(sources[0]))
        if query.facet == "compare":
            background = [p for source in sources[1:] for p in _passages(source)]
            comparison = (_named_comparison(focal + background, query) if query.subjects else
                          _comparison(focal, background, query))
            if comparison:
                return {"sections": [comparison], "limitations": limits}
            limits += " 找不到足夠原文核對兩側各自的條件。"
            if query.subjects:
                return {"sections": [], "limitations": limits}
        chosen = _best(focal, query)
        if chosen:
            facet = query.comparison_facet or query.facet
            return {"sections": [_explain(chosen, facet, query=query)], "limitations": limits}
    return {
        "sections": [],
        "limitations": limits + " 目前找不到能直接回應這個問題的原文。可指定論文中的方法名、資料集或指標再查。",
    }


_MEASURED_COST = re.compile(
    r"(?<![\w.])\d+(?:[.,]\d+)?\s*(?:n?s|[μum]s|(?:nano|micro|milli)?seconds?|minutes?|hours?"
    r"|[kmgt]i?b|tokens?|joules?|watts?)\b|\b(?:latency|overhead|runtime|costs?)\s+"
    r"(?:fell|dropped|decreased|decreases?|risen?|rose|increased?)\s+by\s+\d+(?:\.\d+)?\s*%",
    re.I,
)
_COST_PROTOCOL = re.compile(
    r"\b(?:efficiency|resource (?:use|usage)|latency|runtime|costs?) (?:is|are|was|were) "
    r"(?:measured|estimated|recorded) (?:as|by|with|using|on)\b|(?:資源用量|效率|延遲)(?:以|用).{0,30}(?:量測|評估)",
    re.I,
)
_OWN_EVALUATION = re.compile(
    r"\b(?:we (?:evaluat\w*|measur\w*|test\w*|assess\w*|scor\w*)|our (?:experiments?|evaluation|tests?))\b"
    r"|本(?:研究|文)以.{0,25}(?:評估|評分|測試)", re.I,
)
_GOAL_ONLY = re.compile(r"\b(?:has to be|needs? to be|must be|aims? to|we plan to)\b|目標是|必須做到", re.I)


def _lesson_passages(passages, facet):
    for passage in passages:
        text = passage.text
        if facet == "method" and (
            _COST_PROTOCOL.search(text) or not (_CORE_METHOD.search(text) or _actions(text))
        ):
            continue
        if facet == "cost" and (_GOAL_ONLY.search(text)
                or not (_MEASURED_COST.search(text) or _COST_PROTOCOL.search(text))):
            continue
        if facet == "evaluation" and (
            _GOAL_ONLY.search(text) or _COST_PROTOCOL.search(text)
            or not (_scope(passage) == "results" or _OWN_EVALUATION.search(text)
                    or _JUDGE.search(text) or _VOTING.search(text))
        ):
            continue
        yield passage


def _lesson_check(mechanism, focal):
    citations = mechanism["citations"]
    passages = [p for cite in citations for p in focal if p.cite() == cite]
    for passage in passages:
        if actions := _actions(passage.text):
            operation = actions.split("、", 1)[0]
            question = {
                "保留紀錄": "作者完整保留哪些紀錄？引文怎麼說明這些紀錄在後續流程的角色？",
                "建立索引": "作者替哪些資料建立索引？用引用核對索引能連回哪些內容。",
            }.get(operation, f"原文的「{operation}」處理什麼資料？從引用找出操作對象，再到相鄰原文確認結果或下一步。")
            return _section("check", "inference", "用原文驗證自己的理解", question, passage)
    return _section(
        "check", "inference", "用原文驗證自己的理解",
        "試著完整說出作者提出的方法。它接收什麼資料、做了哪些處理，每一步有哪句原文支持？",
        passages[0],
    )


def lesson(context):
    """Build a reading sequence from the focal version's available excerpts."""
    sources = context.get("sources", [])
    limits = _limits(context)
    if not sources:
        return {"sections": [], "limitations": limits}
    focal = list(_passages(sources[0]))
    sections, used = [], set()
    for facet, role in (("problem", "problem"), ("method", "mechanism"), ("result", "answer"),
                        ("evaluation", "answer"), ("cost", "answer"), ("limit", "answer")):
        candidates = _lesson_passages((p for p in focal if p.text not in used), facet)
        candidate = _best(candidates, _facet(facet, protocol=facet == "evaluation"))
        if candidate:
            section = _explain(candidate, facet, role)
            used.add(candidate.text)
            if facet == "cost" and not _MEASURED_COST.search(candidate.text):
                section["title"] = "資源怎麼測量"
            if facet == "method" and _CORE_METHOD.search(candidate.text) and not _actions(candidate.text):
                detail = _best([
                    p for p in focal if p.excerpt is candidate.excerpt and p.region == candidate.region
                    and p.text not in used
                    and _actions(p.text) and not _CORE_METHOD.search(p.text)
                ], _facet("method"))
                if detail:
                    reported, _ = _reported(detail, "method")
                    section["text"] += " " + reported
                    section["citations"].append(detail.cite())
                    used.add(detail.text)
            sections.append(section)
    background = [p for source in sources[1:] for p in _passages(source)]
    mechanism = next((s for s in sections if s["role"] == "mechanism"), None)
    detailed = [p for p in focal if _actions(p.text)]
    paired_background = [p for p in background if _actions(p.text)]
    core = [p for p in detailed if mechanism and p.cite() in mechanism["citations"]]
    comparison = (_comparison(core, paired_background, _facet("compare"))
                  or _comparison(detailed, paired_background, _facet("compare")))
    if comparison:
        sections.append(comparison)
    else:
        limits += " 暫缺能並讀的兩份原文片段。"
    if mechanism:
        sections.append(_lesson_check(mechanism, focal))
    if not sections and focal:
        sections.append(_explain(focal[0], "free"))
    return {"sections": sections, "limitations": limits}
