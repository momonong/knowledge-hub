"""Structured evidence from a fixed public arXiv HTML version."""
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from . import hive_guide, hive_reader
from .sources import ARXIV_ASSET_PATH_RE, ARXIV_HTML_PATH_RE, SAFE_ANCHOR_RE

MAX_PANELS = 32
MAX_ROWS = 80
MAX_CELLS = 40
MAX_IMAGES = 8
_VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
_SKIP = frozenset(('script', 'style', 'template', 'noscript', 'annotation', 'annotation-xml'))
_CAPTION_LABEL = re.compile(
    r'^\s*(fig(?:ure)?\.?|table)\s+([A-Z]?\d+(?:\.\d+){0,2}[A-Z]?)(?=[:.\s]|$)', re.I
)


def caption_parts(caption):
    label = _CAPTION_LABEL.match(caption)
    if label:
        kind = 'table' if label[1].lower() == 'table' else 'figure'
        return kind, label[2].casefold(), caption[label.end():]
    return None


def paper_url(key, label):
    if not key.startswith('arxiv:'):
        return None
    path = '/html/' + key.removeprefix('arxiv:') + label
    return 'https://arxiv.org' + path if ARXIV_HTML_PATH_RE.fullmatch(path) else None


def anchor(value):
    return value if value and len(value) <= 128 and SAFE_ANCHOR_RE.fullmatch(value) else ''


def image_url(source, raw):
    if not raw or len(raw) > 512 or any(ord(c) < 33 for c in raw):
        return None
    try:
        value = urljoin(source, raw)
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme == 'https' and parsed.netloc == 'arxiv.org' and not parsed.query
            and not parsed.fragment and value.startswith(source + '/') and ARXIV_ASSET_PATH_RE.fullmatch(parsed.path)):
        return value
    return None


def _text(parts, limit):
    value = re.sub(r'\s+', ' ', ''.join(parts)).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def _span(value):
    return min(int(value), MAX_CELLS) if value and re.fullmatch(r'[1-9][0-9]?', value) else 1


class _Paper(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.stack = []
        self.panels = []
        self.references = {}
        self.panel = None
        self.outer_panels = []
        self.paragraph = None
        self.caption = None
        self.table_depth = 0
        self.row = None
        self.cell = None
        self.cell_parts = []
        self.truncated = False
        self.skip = ''

    def handle_starttag(self, tag, attrs):
        if self.skip:
            return
        if tag in _SKIP:
            self.skip = tag
            return
        value = dict(attrs)
        classes = set((value.get('class') or '').split())
        identifier = anchor(value.get('id'))
        if tag not in _VOID:
            if len(self.stack) >= 256:
                raise ValueError('HTML nesting exceeds the evidence parser limit')
            self.stack.append((tag, identifier))
        # LaTeXML can wrap several independently numbered table minipages
        # in a single table float, even when the children use figure classes.
        nested_table_panel = (self.panel is not None and self.panel['kind'] == 'table'
                              and self.caption is None and self.table_depth == 0
                              and 'ltx_figure_panel' in classes)
        if tag == 'figure' and (self.panel is None or nested_table_panel):
            if 'ltx_float' in classes and not classes.intersection({'ltx_figure', 'ltx_table'}):
                return
            if len(self.panels) >= MAX_PANELS:
                self.truncated = True
                return
            if self.panel is not None:
                self.outer_panels.append((self.panel, self.panel_depth))
            self.panel = {'anchor':identifier, 'kind':'table' if 'ltx_table' in classes else 'figure',
                          'caption':'', 'rows':[], 'images':[], 'truncated':False}
            self.panel_depth = len(self.stack)
        if self.panel is not None:
            if tag in ('figcaption','caption') and not self.panel['caption'] and self.caption is None:
                self.caption = []
                self.caption_depth = len(self.stack)
            if tag in ('img','object') and self.panel['kind'] == 'figure':
                url = image_url(self.source, value.get('src' if tag == 'img' else 'data'))
                images = self.panel['images']
                if url and url not in images:
                    if len(images) < MAX_IMAGES:
                        images.append(url)
                    else:
                        self.panel['truncated'] = True
            if tag == 'table' and self.table_depth == 0 and not self.panel['rows'] and 'ltx_equation' not in classes:
                self.table_depth = len(self.stack)
                self.panel['kind'] = 'table'
            if self.table_depth and len(self.stack) > self.table_depth:
                if tag == 'tr' and self.row is None:
                    self.row = []
                    self.row_depth = len(self.stack)
                if self.row is not None and tag in ('td','th') and self.cell is None:
                    self.cell = {'header':tag == 'th' or any(t == 'thead' for t, _ in self.stack),
                                 'colspan':_span(value.get('colspan')),
                                 'rowspan':_span(value.get('rowspan'))}
                    self.cell_depth = len(self.stack)
                    self.cell_parts = []
                elif self.cell is not None:
                    if tag in ('img','object'):
                        self.panel['image_cells'] = True
                    style = (value.get('style') or '').replace(' ', '').lower()
                    if tag in ('b','strong') or 'ltx_font_bold' in classes or 'font-weight:bold' in style:
                        self.cell['bold'] = True
                    if tag == 'u' or 'ltx_underline' in classes or 'text-decoration:underline' in style:
                        self.cell['underline'] = True
            if tag == 'br':
                self.handle_data(' ')
        elif tag == 'p' and self.paragraph is None:
            self.paragraph = (next((ident for _, ident in reversed(self.stack) if ident), ''), [], set())
            self.paragraph_depth = len(self.stack)
        elif self.paragraph and tag == 'a':
            href = value.get('href') or ''
            target = anchor(href[1:]) if href.startswith('#') else ''
            if target and len(self.paragraph[2]) < 16:
                self.paragraph[2].add(target)
        if tag == 'math':
            # LaTeXML exposes a plain-text equivalent for MathML cells.
            # Use it once instead of concatenating every MathML token.
            if value.get('alttext'):
                self.handle_data(value['alttext'])
                self.skip = 'math'
                self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip:
                self.skip = ''
            return
        depth = next((i + 1 for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), 0)
        if not depth:
            return
        if self.caption is not None and depth <= self.caption_depth:
            self.panel['caption'] = _text(self.caption, 1600)
            self.caption = None
        if self.cell is not None and depth <= self.cell_depth:
            self.cell['text'] = _text(self.cell_parts, 400)
            if len(self.row) < MAX_CELLS:
                self.row.append(self.cell)
            else:
                self.panel['truncated'] = True
            self.cell = None
        if self.row is not None and depth <= self.row_depth:
            if self.row and len(self.panel['rows']) < MAX_ROWS:
                self.panel['rows'].append(self.row)
            elif self.row:
                self.panel['truncated'] = True
            self.row = None
        if self.table_depth and depth <= self.table_depth:
            self.table_depth = 0
        while self.panel is not None and depth <= self.panel_depth:
            panel = self.panel
            label = caption_parts(panel['caption'])
            if label:
                # An HTML class describes layout. The author labels the evidence.
                panel['kind'] = label[0]
            if panel['caption'] or panel['images'] or panel['rows']:
                if len(self.panels) < MAX_PANELS:
                    self.panels.append(panel)
                else:
                    self.truncated = True
            if self.outer_panels:
                self.panel, self.panel_depth = self.outer_panels.pop()
            else:
                self.panel = None
        if self.paragraph is not None and depth <= self.paragraph_depth:
            identifier, parts, targets = self.paragraph
            if identifier and targets:
                snippet = _text(parts, 420)
                for target in targets:
                    entries = self.references.setdefault(target, [])
                    if len(entries) < 2 and snippet:
                        entries.append({'anchor':identifier, 'text':snippet})
            self.paragraph = None
        del self.stack[depth - 1:]

    def handle_data(self, value):
        if self.skip:
            return
        if self.caption is not None:
            self.caption.append(value)
        if self.cell is not None:
            self.cell_parts.append(value)
        if self.paragraph is not None:
            self.paragraph[1].append(value)


def extract(raw, source):
    parsed = urlsplit(source)
    if parsed.scheme != 'https' or parsed.netloc != 'arxiv.org' or not ARXIV_HTML_PATH_RE.fullmatch(parsed.path):
        raise ValueError('Figure evidence requires a fixed arXiv HTML version')
    parser = _Paper(source)
    parser.feed(raw.decode('utf-8'))
    parser.close()
    for panel in parser.panels:
        panel['references'] = parser.references.get(panel['anchor'], [])
        guide = hive_guide.GUIDE.get(panel['anchor']) if source == hive_reader.PAPER else None
        if guide:
            panel['guide'] = {k: v for k, v in guide.items() if k != 'steps'}
            panel['steps'] = guide['steps']
        else:
            panel['steps'] = reading_steps(panel)
    return {'panels':parser.panels, 'truncated':parser.truncated, 'source':source}


def reading_steps(panel):
    caption = panel['caption'].lower()
    if panel['kind'] == 'table':
        labels = list(dict.fromkeys(cell['text'] for row in panel['rows'][:2] for cell in row
                                    if cell['text'] and len(cell['text']) <= 30 and re.search(r'[A-Za-z]', cell['text'])))[:6]
        steps = ['先讀表頭' + ('（例如 ' + '、'.join(labels) + '）' if labels else '') + '，分清楚比較對象、指標和單位。']
        if 'ablation' in caption:
            steps.append('找完整方法與拿掉一個元件的列。在其他設定相同時，比較同一欄，判斷該元件是否有貢獻。')
        else:
            steps.append('固定同一資料集和實驗條件，比較兩列的同一欄。從作者解讀核對指標方向及是否還有成本差異。')
        return steps
    if re.search(r'\b(?:overview|architecture|pipeline|framework|workflow)\b', caption):
        return ['先照箭頭找輸入、各階段與輸出。用圖說裡的元件名稱對照圖中標籤。',
                '順著一筆輸入走一遍，再讀作者在正文引用這張圖的段落，確認哪些步驟有證據。']
    return ['先讀圖說和圖例，確認每個子圖在比較什麼。若有座標軸，先確認單位及方向。',
            '固定一個條件，觀察兩組的差異。用作者在正文的解讀核對你看到的趨勢。']


def media_type(url, data):
    """Allow image bytes only. SVG is served with a sandbox and no active content."""
    if url.endswith('.png') and data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if url.endswith(('.jpeg','.jpg')) and data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if url.endswith('.webp') and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if url.endswith('.svg') and re.search(rb'<svg[\s>]', data[:8192]) and not re.search(rb'<!\s*ENTITY\b', data, re.I):
        return 'image/svg+xml'
    raise ValueError('Publisher image format is unavailable')
