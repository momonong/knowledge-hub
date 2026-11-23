"""Bounded, page-level evidence from a fixed arXiv PDF supplied as bytes."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import os
import re
import shutil
import struct
import subprocess
import tempfile
from threading import BoundedSemaphore
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from .sources import ARXIV_HTML_PATH_RE, compute_content_hash
from .visuals import MAX_PANELS

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 120
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_LAYOUT_BYTES = 10 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_RENDER_DIMENSION = 1600
PROCESS_ADDRESS_BYTES = 192 * 1024 * 1024
PROCESS_TIMEOUT = 8
_MAX_CAPTION_LENGTH = 1600
_slot = BoundedSemaphore(1)
_controls = bytes(c for c in range(32) if c not in (9, 10, 13))
_label_pattern = r'(Figure|Fig\.?|Table)\s+([A-Z]?[0-9]+(?:[.-][0-9]+)*|[A-Z]\.[0-9]+|[IVXLC]+)'
_caption_prefix = re.compile(rf'^{_label_pattern}\s*[:.|]\s+(\S.*)$', re.IGNORECASE)
_bare_prefix = re.compile(rf'^{_label_pattern}\s+(\S.*)$', re.IGNORECASE)
_bold_label = re.compile(_label_pattern, re.IGNORECASE)
_punctuated_labels = re.compile(rf'(?<!\w){_label_pattern}\s*[:.|]\s+', re.IGNORECASE)


class PDFEvidenceError(ValueError):
    """A PDF could not be processed. The code is safe for callers to branch on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def pdf_source(key: str, label: str) -> str | None:
    """Return the PDF URL for a canonical, version-pinned arXiv key."""
    if not isinstance(key, str) or not key.startswith('arxiv:') or not key.isascii():
        return None
    if not isinstance(label, str) or not re.fullmatch(r'v[1-9][0-9]*', label):
        return None
    edition = key.removeprefix('arxiv:') + label
    if not ARXIV_HTML_PATH_RE.fullmatch(f'/html/{edition}'):
        return None
    return f'https://arxiv.org/pdf/{edition}'


def _paper_identity(source: str) -> tuple[str, str]:
    if not isinstance(source, str) or not source.isascii():
        raise ValueError('PDF evidence requires a fixed arXiv HTML URL')
    parsed = urlsplit(source)
    match = ARXIV_HTML_PATH_RE.fullmatch(parsed.path)
    if match is None or source != f'https://arxiv.org{parsed.path}':
        raise ValueError('PDF evidence requires a fixed arXiv HTML URL')
    return match.groups()


def _program(name: str) -> str:
    # On the service, resolve installed OS commands rather than a process's
    # cwd, writable PATH entries, or a user-supplied executable location.
    path = shutil.which(name, path=os.defpath)
    if path is None:
        raise PDFEvidenceError('dependency_unavailable', f'PDF processing requires {name}')
    return path


@contextmanager
def _pdf_file(raw: bytes):
    if not isinstance(raw, bytes) or not raw.startswith(b'%PDF-'):
        raise PDFEvidenceError('invalid_pdf', 'PDF signature is missing')
    if len(raw) > MAX_PDF_BYTES:
        raise PDFEvidenceError('input_limit', 'PDF input exceeds 20 MiB')
    if not _slot.acquire(blocking=False):
        raise PDFEvidenceError('busy', 'Another PDF is being processed')
    try:
        with tempfile.TemporaryDirectory(prefix='kh-pdf-') as directory:
            Path(directory, 'source.pdf').write_bytes(raw)
            yield directory
    finally:
        _slot.release()


def _run(directory: str, program: str, args: tuple[str, ...], limit: int) -> bytes:
    command = [
        _program('prlimit'), f'--as={PROCESS_ADDRESS_BYTES}', '--cpu=6',
        f'--fsize={limit}', '--nofile=64', '--core=0',
        '--', _program(program), *args,
    ]
    # File-backed stdout makes RLIMIT_FSIZE bound tool output before Python
    # allocates it. Poppler receives only the private temporary file.
    with tempfile.TemporaryFile(dir=directory) as output:
        try:
            subprocess.run(
                command, cwd=directory, env={'LC_ALL': 'C.UTF-8', 'HOME': directory, 'PATH': os.defpath},
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                timeout=PROCESS_TIMEOUT, check=True,
            )
        except subprocess.TimeoutExpired as err:
            raise PDFEvidenceError('timeout', f'{program} timed out') from err
        except subprocess.CalledProcessError as err:
            raise PDFEvidenceError(
                'processing_failed', f'{program} could not complete within PDF processing limits',
            ) from err
        except OSError as err:
            raise PDFEvidenceError('dependency_unavailable', f'{program} could not run') from err
        output.seek(0)
        data = output.read(limit + 1)
    if len(data) > limit:
        raise PDFEvidenceError('output_limit', f'{program} exceeded its output limit')
    return data


def _pdf_info(directory: str) -> tuple[int, str, str]:
    info = _run(directory, 'pdfinfo', ('source.pdf',), 128 * 1024).decode('utf-8', 'replace')
    pages = re.search(r'^Pages:[ \t]+([0-9]{1,8})$', info, re.MULTILINE)
    if not pages or not 1 <= int(pages[1]) <= MAX_PAGES:
        raise PDFEvidenceError('page_limit', f'PDF requires a readable page count of 1–{MAX_PAGES}')
    if re.search(r'^Encrypted:[ \t]+yes\b', info, re.MULTILINE):
        raise PDFEvidenceError('encrypted_pdf', 'Encrypted PDFs cannot be processed')
    title = re.search(r'^Title:[ \t]*(.*)$', info, re.MULTILINE)
    author = re.search(r'^Author:[ \t]*(.*)$', info, re.MULTILINE)
    return int(pages[1]), _clean(title[1])[:500] if title else '', _clean(author[1])[:500] if author else ''


def _clean(text: str) -> str:
    return ' '.join(''.join(c for c in text if c.isprintable() or c.isspace()).split())


def _page_text(directory: str, page_count: int) -> list[str]:
    data = _run(directory, 'pdftotext', ('-enc', 'UTF-8', '-cropbox', 'source.pdf', '-'), MAX_TEXT_BYTES)
    pages = data.decode('utf-8', 'replace').split('\f')
    if pages[-1] == '':
        pages.pop()
    if len(pages) != page_count:
        raise PDFEvidenceError('page_text_unavailable', 'PDF text does not match the physical page count')
    return [_clean(page) for page in pages]


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    left: float
    top: float
    right: float
    height: float
    linked_prefix: bool = False


def _mark(match: re.Match) -> tuple[str, str]:
    return ('table' if match[1].casefold() == 'table' else 'figure', match[2].casefold())


def _font_layout(data: bytes, page_count: int) -> dict[int, tuple[list[_Line], list[tuple]]]:
    # Poppler's XML export preserves separate display fragments, including
    # a bold label for a caption without punctuation and link references.
    pages = {}
    for _, page in ET.iterparse(BytesIO(data.translate(None, _controls)), events=('end',)):
        if page.tag != 'page':
            continue
        number = len(pages) + 1
        if page.get('number') != str(number):
            raise ValueError('Font layout page numbers are out of order')
        lines, markers = [], []
        for fragment in page.iterfind('text'):
            value = _clean(''.join(fragment.itertext()))
            x, y, width, height = (int(fragment.get(key)) for key in ('left', 'top', 'width', 'height'))
            linked_prefix = (len(fragment) > 0 and fragment[0].tag == 'a'
                             and not (fragment.text or '').strip())
            lines.append(_Line(value, x, y, x + width, height, linked_prefix))
            bold = fragment.find('b')
            label = _clean(''.join(bold.itertext())) if bold is not None else ''
            match = _bold_label.fullmatch(label)
            if match and value.startswith(label) and bold.find('.//a') is None:
                markers.append((*_mark(match), x, y))
        pages[number] = (sorted(lines, key=lambda line: line.top), markers)
        page.clear()
    if len(pages) != page_count:
        raise ValueError('Font layout page count mismatch')
    return pages


def _follow_caption_lines(lines: list[_Line], index: int) -> str:
    first = lines[index]
    parts, previous = [first.text], first
    # Continue only the same caption column with comparable line spacing
    # and font height. Short centered closing lines are also permitted.
    for line in lines[index + 1:]:
        gap = line.top - previous.top
        if gap > first.height * 1.25 or len(parts) >= 12:
            break
        left_aligned = abs(line.left - first.left) <= 3
        centered = abs((line.left + line.right) - (first.left + first.right)) <= 6
        if (gap < first.height * .7 or abs(line.height - first.height) > 2.5
                or line.right > first.right + 12 or not (left_aligned or centered)):
            continue
        if _bare_prefix.match(line.text) or _caption_prefix.match(line.text):
            break
        parts.append(line.text)
        previous = line
    return ' '.join(parts)


def _separate_caption_columns(lines: list[_Line], marks: list[tuple], block: ET.Element) -> list[tuple]:
    # Space-adjacent captions can be combined into a single word-layout
    # block. Recover each from independently placed font fragments only
    # when each original label has one clear match in the affected area.
    top, left, right, bottom = (float(block.get(key)) for key in ('yMin', 'xMin', 'xMax', 'yMax'))
    recovered = {}
    for index, line in enumerate(lines):
        if line.linked_prefix or not (top - 5 <= line.top <= bottom and left - 3 <= line.left <= right):
            continue
        if match := _caption_prefix.match(line.text):
            mark = _mark(match)
            if mark in marks:
                if mark in recovered:
                    return []
                recovered[mark] = (*mark, _follow_caption_lines(lines, index), line.top, line.left)
    return list(recovered.values()) if len(recovered) == len(marks) else []


def _unpunctuated_captions(blocks: list, markers: list[tuple], excluded: set[tuple]) -> tuple[list[tuple], int]:
    lines = []
    for block in blocks:
        for element in block.iterfind('{*}line'):
            value = _clean(' '.join(word.text or '' for word in element.iterfind('{*}word')))
            left, top, right, bottom = (float(element.get(key)) for key in ('xMin', 'yMin', 'xMax', 'yMax'))
            lines.append(_Line(value, left, top, right, bottom - top))
    lines.sort(key=lambda item: item.top)
    results, unresolved = [], 0
    for kind, label, x, y in markers[:2 * MAX_PANELS]:
        if (kind, label) in excluded:
            continue
        matches = []
        for index, line in enumerate(lines):
            if abs(line.left - x) > 3 or abs(line.top - y) > 5:
                continue
            match = _bare_prefix.match(line.text)
            if match and _mark(match) == (kind, label) and not _caption_prefix.match(line.text):
                matches.append(index)
        if len(matches) != 1:
            unresolved += 1
            continue
        index = matches[0]
        first = lines[index]
        caption = _follow_caption_lines(lines, index)
        results.append((kind, label, caption, first.top, first.left))
    return results, unresolved + max(0, len(markers) - 2 * MAX_PANELS)


def _caption_blocks(data: bytes, page_count: int, font_pages: dict[int, tuple]) -> tuple[list[tuple], int]:
    # Poppler can emit glyph control codes into its generated XHTML.
    # Strip the XML-invalid bytes before parsing; the layout is never served.
    candidates = []
    unresolved = 0
    page_number = 0
    for _, page in ET.iterparse(BytesIO(data.translate(None, _controls)), events=('end',)):
        if page.tag.rsplit('}', 1)[-1] != 'page':
            continue
        page_number += 1
        blocks = list(page.iterfind('.//{*}block'))
        page_candidates = []
        font_lines, markers = font_pages.get(page_number, ([], []))
        for block in blocks:
            lines = [
                _clean(' '.join(word.text or '' for word in line.iterfind('{*}word')))
                for line in block.iterfind('{*}line')
            ]
            if not lines or not (match := _caption_prefix.match(lines[0])):
                continue
            caption = ' '.join(lines)
            marks = [_mark(other) for other in _punctuated_labels.finditer(caption)]
            if len(marks) > 1:
                recovered = _separate_caption_columns(font_lines, marks, block)
                unresolved += len(marks) - len(recovered)
                page_candidates.extend(recovered)
                continue
            page_candidates.append((*_mark(match), caption, float(block.get('yMin')), float(block.get('xMin'))))
        if markers:
            extra, ambiguous = _unpunctuated_captions(blocks, markers, {item[:2] for item in page_candidates})
            unresolved += ambiguous
            page_candidates.extend(extra)
        page_candidates.sort(key=lambda item: item[3:])
        candidates.extend((kind, label, page_number, caption) for kind, label, caption, _, _ in page_candidates)
        page.clear()
    if page_number != page_count:
        raise ValueError('Caption layout page count mismatch')
    counts = Counter((kind, label) for kind, label, _, _ in candidates)
    panels = [candidate for candidate in candidates if counts[candidate[:2]] == 1]
    return panels, unresolved + len(candidates) - len(panels)


def _panel(kind: str, label: str, page_number: int, caption: str, source: str) -> dict:
    steps = [f'在原始 PDF 第 {page_number} 頁找這則圖說，再依圖說定位同頁的{"表格" if kind == "table" else "圖形"}。']
    if kind == 'table':
        steps.append('回到原頁核對欄名、列名、單位與註腳後再讀數值。')
    else:
        steps.append('用圖說對照原頁的子圖標記、座標軸或圖例。')
    truncated = len(caption) > _MAX_CAPTION_LENGTH
    return {
        'kind': kind, 'anchor': f'page-{page_number}-{kind}-{label}',
        'caption': caption[:_MAX_CAPTION_LENGTH - 1].rstrip() + '…' if truncated else caption,
        'page_number': page_number, 'page_url': f'{source}#page={page_number}',
        'representation': 'pdf_page', 'images': [], 'rows': [], 'references': [],
        'steps': steps, 'truncated': truncated,
    }


def _visual_manifest(directory: str, source: str, pdf_url: str, pages: list[str]) -> dict:
    state, reason = 'unavailable', 'no_reliable_captions'
    candidates, unresolved = [], 0
    try:
        layout = _run(directory, 'pdftotext', ('-bbox-layout', '-cropbox', 'source.pdf', '-'), MAX_LAYOUT_BYTES)
        try:
            fonts = _run(directory, 'pdftohtml', (
                '-q', '-xml', '-i', '-nomerge', '-zoom', '1', '-stdout', 'source.pdf',
            ), MAX_LAYOUT_BYTES)
            font_pages = _font_layout(fonts, len(pages))
        except (ValueError, ET.ParseError):
            font_pages = {}
            reason = 'caption_layout_unavailable'
        candidates, unresolved = _caption_blocks(layout, len(pages), font_pages)
    except (ValueError, ET.ParseError):
        reason = 'caption_layout_unavailable'
    if candidates:
        state, reason = 'ready', ''
    panels = [_panel(*values, pdf_url) for values in candidates[:MAX_PANELS]]
    return {
        'source': source, 'pdf_url': pdf_url, 'panels': panels,
        'state': state, 'reason': reason, 'page_count': len(pages),
        'missing_text_pages': [number for number, page in enumerate(pages, 1) if not page],
        'unresolved_captions': unresolved, 'truncated': len(candidates) > MAX_PANELS,
    }


def parse(raw: bytes, paper_html_url: str) -> tuple[dict, dict]:
    """Return (parsed entry, visual manifest) for the caller's exact public edition."""
    paper_id, version = _paper_identity(paper_html_url)
    key, label = f'arxiv:{paper_id}', f'v{version}'
    pdf_url = f'https://arxiv.org/pdf/{paper_id}{label}'
    with _pdf_file(raw) as directory:
        page_count, title, author = _pdf_info(directory)
        pages = _page_text(directory, page_count)
        if not any(c.isalpha() for page in pages for c in page):
            raise PDFEvidenceError('no_text', 'This PDF has no extractable text. OCR is unavailable.')
        blocks = [
            {'anchor': f'page-{number}', 'text': text, 'source_url': f'{pdf_url}#page={number}'}
            for number, text in enumerate(pages, 1) if text
        ]
        manifest = _visual_manifest(directory, paper_html_url, pdf_url, pages)
    title = title or f'arXiv {paper_id}{label}'
    scope = 'partial'
    entry = {
        'key': key, 'arxiv_id': paper_id, 'version_num': version, 'source_name': 'arxiv',
        'label': label, 'title': title, 'scope': scope, 'fulltext_status': scope,
        'source_url': pdf_url, 'author': author, 'published_at': '', 'blocks': blocks,
        'content_hash': compute_content_hash(title, scope, label, blocks),
    }
    return entry, manifest


def render_page(raw: bytes, page_number: int) -> bytes:
    """Render one original, 1-based PDF page with a bounded raster size."""
    if type(page_number) is not int or not 1 <= page_number <= MAX_PAGES:
        raise PDFEvidenceError('invalid_page', 'PDF page number is out of range')
    with _pdf_file(raw) as directory:
        page_count, _, _ = _pdf_info(directory)
        if page_number > page_count:
            raise PDFEvidenceError('invalid_page', 'PDF page number is out of range')
        page = str(page_number)
        png = _run(directory, 'pdftoppm', (
            '-f', page, '-l', page, '-cropbox', '-hide-annotations', '-singlefile',
            '-scale-to', str(MAX_RENDER_DIMENSION), '-png', 'source.pdf',
        ), MAX_PNG_BYTES)
    if len(png) < 24 or png[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
        raise PDFEvidenceError('render_unavailable', 'PDF page could not be rendered')
    dimensions = struct.unpack('>II', png[16:24])
    if not all(1 <= size <= MAX_RENDER_DIMENSION for size in dimensions):
        raise PDFEvidenceError('render_unavailable', 'PDF page exceeds raster bounds')
    return png
