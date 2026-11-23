"""Select readable, contiguous spans without rewriting the source evidence."""
from __future__ import annotations

import re
from itertools import islice

_END = re.compile(r'[.!?。！？][\"\'”’」』）\]]*(?=\s|$|[\u3400-\u9fff])')
# Start at the beginning of an intra-line space run. Retrying from every space
# in a wide PDF layout made unrelated article pages stall on corpus retrieval.
_BACKMATTER = re.compile(r'(?im)(?:^[ \t]*(?:\d[.\d]*[ \t]+)?|(?<![ \t])[ \t]{5,})(?:references|bibliography|acknowledg(?:e)?ments|參考文獻|致謝)[ \t]*$')
_RESUME = re.compile(r'(?im)^[ \t]*(?:appendix\b[^\n]*|[A-Z][ \t]{2,}[^\n]+|\d[.\d]*[ \t]+(?:methods?|limitations?|conclusions?)[^\n]*)$')
_ABBREVIATION = re.compile(r'\b(?:al|et|e\.g|i\.e|fig|eq|dr|mr|vs|etc)\.$', re.I)
_MECHANISM = re.compile(r'\b(?:we (?:propose|introduce|use|present)|encod\w*|retriev\w*|generat\w*|method|approach|model)\b|方法|機制|檢索|生成', re.I)
_HEADING = re.compile(r'^(?:\d+(?:\.\d+)*\s+[^.!?。]{2,80}|Abstract|Introduction|Conclusion|Methods?|Reader|摘要|結論)$', re.I)
_EXPLAINS = re.compile(r'\b(?:we (?:propose|introduce|explore|present)|consists? of|is based on|is composed of|first (?:uses?|retrieves?|encodes?))\b|首先|方法是|機制是', re.I)
_WORDS = re.compile(r'\w+')
# Match Python's Unicode IGNORECASE behavior for ASCII query letters.
_ASCII_FOLD = str.maketrans({'İ':'i', 'ı':'i', 'ſ':'s', 'K':'k'})


def _trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def sentence_spans(text):
    """Offsets retain original whitespace, including PDF line breaks."""
    start = 0
    for match in _END.finditer(text):
        end = match.end()
        if match.group()[0] == '.' and _ABBREVIATION.search(text[max(0, match.start()-12):end]):
            continue
        a, b = _trim(text, start, end)
        if a < b:
            yield a, b, True
        start = end
    a, b = _trim(text, start, len(text))
    if a < b:
        yield a, b, False


def _body_ranges(text, skipping=False):
    ranges, start = [], 0
    markers = [(m.start(), True) for m in _BACKMATTER.finditer(text)]
    markers += [(m.start(), False) for m in _RESUME.finditer(text)]
    for position, backmatter in sorted(markers):
        if backmatter and not skipping:
            if position > start:
                ranges.append((start, position))
            skipping = True
        elif not backmatter and skipping:
            start, skipping = position, False
    if not skipping and start < len(text):
        ranges.append((start, len(text)))
    return ranges, skipping


def _noise(text):
    visible = re.sub(r'\s', '', text)
    if not visible:
        return True
    if sum(c.isdigit() for c in visible) / len(visible) > .22:
        return True
    # Citation entries, unlike an in-text citation, usually start a line and
    # contain a year/venue. Never erase a whole page merely for citing a paper.
    if re.match(r'^\s*\[\d+\]', text) and re.search(r'\b(?:19|20)\d\d\b', text):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    entries = sum(bool(re.match(r'(?:\[\d+\]|[A-Z][\w’\'-]+,?\s+(?:[A-Z]\.|[A-Z][a-z]+))', line)) for line in lines)
    return len(lines) >= 3 and entries >= 3 and entries / len(lines) > .45


def _reference_entry(text):
    """Keep author/year context intact before splitting bibliography titles."""
    head = text.lstrip()[:400]
    year = re.search(r'\b(?:19|20)\d\d[a-z]?[).]', head)
    if not year:
        return False
    authors = head[:year.start()].strip()
    if re.match(r'^\[\d+\]', authors):
        return True
    # Require an author-like prefix AND comma-separated names/initials; a
    # normal sentence citing a year must remain available as evidence.
    words = re.findall(r"[^\W\d_]+", authors, re.UNICODE)
    names_only = all(w[0].isupper() or w in {'and', 'et', 'al', 'de', 'van', 'von', 'la', 'del'} for w in words)
    return names_only and ',' in authors and bool(re.match(
        r"^[A-Z][\w’'-]+(?:,\s*(?:[A-Z]\.|[A-Z][a-z]+)|\s+(?:[A-Z][\w’'-]+|[A-Z]\.))", authors))


def _word_window(text, start, end, position, limit=700):
    left = max(start, position - limit // 3)
    right = min(end, left + limit)
    if left > start:
        while left < right and not text[left - 1].isspace() and text[left].isascii():
            left += 1
    if right < end:
        while right > left and not text[right].isspace() and text[right - 1].isascii():
            right -= 1
    return _trim(text, left, right)


def _columns(text, first_prefix=''):
    lines = [line for line in text.splitlines() if line.strip()]
    internal = any(re.search(r'\S\s{5,}\S', line) for line in lines)
    # The first line loses indentation when a sentence starts mid-paragraph.
    # Uniform indentation on following lines is harmless (e.g. an abstract).
    indents = [len(line) - len(line.lstrip()) for line in lines[1:]]
    if not first_prefix or first_prefix.isspace():
        indents.insert(0, len(first_prefix))
    alternating = len(indents) > 1 and max(indents) - min(indents) >= 25
    return internal or alternating


def select_spans(blocks, terms, focus_terms, *, limit=3):
    """Rank complete sentences, with explicit bounded fragments as a fallback."""
    ordered_terms = sorted(terms | focus_terms)
    patterns = [(re.compile((r'(?<!\w)' + re.escape(t) + r'(?!\w)') if t.isascii() else re.escape(t), re.I), t in focus_terms)
                for t in ordered_terms]
    plain = {t for t in ordered_terms if t.isascii() and t == t.lower() and _WORDS.fullmatch(t)}
    special_patterns = [pattern for t, pattern in zip(ordered_terms, patterns) if t not in plain]
    ranked = []
    skipping = False
    for index, block in enumerate(blocks):
        text = block['text']
        body_ranges, skipping = _body_ranges(text, skipping)
        for body_start, body_end in body_ranges:
            # Paragraph boundaries prevent headings, captions and equations from
            # being glued to the next prose sentence by a missing full stop.
            cuts = [body_start] + [body_start + m.end() for m in re.finditer(r'\n\s*\n', text[body_start:body_end])] + [body_end]
            for a, b in zip(cuts, cuts[1:]):
                if _reference_entry(text[a:b]):
                    continue
                caption_paragraph = bool(re.match(r'^\s*(?:Table|Figure|Fig\.|表|圖)\s*\d+', text[a:b], re.I))
                for s, e, complete in sentence_spans(text[a:b]):
                    s, e = a + s, a + e
                    first_line, separator, rest = text[s:e].partition('\n')
                    if separator and rest.strip() and _HEADING.fullmatch(first_line.strip()):
                        s, e = _trim(text, s + len(first_line) + 1, e)
                    raw = text[s:e]
                    if _noise(raw) or len(re.findall(r'[A-Za-z\u3400-\u9fff]', raw)) < 12:
                        continue
                    # Short heading-like tails are not explanatory prose.
                    if not complete and len(raw) < 90 and not focus_terms:
                        continue
                    windows = [(s, e)]
                    if e - s > 700:
                        positions = [s + m.start() for p, is_focus in patterns if is_focus for m in islice(p.finditer(raw), 6)]
                        if not positions:
                            positions = [s + m.start() for p, _ in patterns for m in islice(p.finditer(raw), 2)][:24] or [s]
                        windows = [_word_window(text, s, e, pos) for pos in positions]
                    for left, right in windows:
                        sample = text[left:right]
                        if not sample:
                            continue
                        # A lower-case page continuation is not a full sentence.
                        fragment = not complete or (left, right) != (s, e) or sample[0].islower()
                        plain_hits = plain.intersection(_WORDS.findall(sample.translate(_ASCII_FOLD).lower()))
                        special_hits = [focus for p, focus in special_patterns if p.search(sample)]
                        hits = len(plain_hits) + len(special_hits)
                        focus_hits = len(plain_hits & focus_terms) + sum(special_hits)
                        if focus_terms and not hits:
                            continue
                        # Distinct terms only; repeated keywords and length do
                        # not win over readable prose or an explicit focus.
                        score = min(hits, 12) + min(len(_MECHANISM.findall(sample)), 3) * 2
                        if len(sample) < 65:
                            score -= 3
                        prefix = text[text.rfind('\n', 0, left) + 1:left]
                        columns = _columns(sample, prefix)
                        caption = caption_paragraph or bool(re.match(r'^(?:Table|Figure|Fig\.|表|圖)\s*\d+', sample, re.I))
                        explains = bool(_EXPLAINS.search(sample))
                        score += (4 if explains and hits >= 2 else 0) - (3 if caption else 0)
                        ranked.append(((-bool(focus_hits), columns, fragment, -score, index, left), index, left, right, fragment, columns))
    selected, occupied, seen = [], [], set()
    for _, index, start, end, fragment, columns in sorted(ranked):
        text = blocks[index]['text'][start:end]
        identity = re.sub(r'\s+', ' ', text).strip().casefold()
        if identity in seen or any(i == index and start < b and end > a for i, a, b in occupied):
            continue
        seen.add(identity)
        occupied.append((index, start, end))
        note = '此為有界節錄，原文句子未完整保留；請展開原文核對。' if fragment else ''
        if columns:
            note += 'PDF 可能有雙欄或表格交錯，請核對原始檔的閱讀順序。'
        selected.append((index, text, 'fragment' if fragment else 'sentence', note))
        if len(selected) == limit:
            break
    return selected


def preview(text, limit=320):
    """Return an exact first sentence, or a clearly labeled word-bound excerpt."""
    spans = list(sentence_spans(text))
    if spans and spans[0][2] and spans[0][1] - spans[0][0] <= limit:
        a, b, _ = spans[0]
        return text[a:b], False
    if len(text) <= limit:
        return text, not (spans and spans[-1][2])
    a, b = _word_window(text, 0, len(text), 0, min(limit, 280))
    return text[a:b], True
