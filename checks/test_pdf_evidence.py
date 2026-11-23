"""Offline tests for fixed arXiv PDF extraction using Poppler and stdlib fixtures."""

import shutil
import struct
import subprocess
import unittest
from unittest.mock import patch

from knowledge_hub import pdf_evidence
from knowledge_hub.sources import compute_content_hash

HTML = 'https://arxiv.org/html/2611.12872v1'
PDF = 'https://arxiv.org/pdf/2611.12872v1'
TOOLS_AVAILABLE = all(shutil.which(tool) for tool in ('pdfinfo', 'pdftotext', 'pdftoppm', 'prlimit'))


def _text(x, y, text, *, bold=False):
    escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    return f'BT /F{2 if bold else 1} 11 Tf {x} {y} Td ({escaped}) Tj ET\n'


def _pdf(*pages):
    """Build a deterministic PDF, including its cross-reference byte offsets."""
    kids = ' '.join(f'{5 + index * 2} 0 R' for index in range(len(pages)))
    bold_font_id = 5 + len(pages) * 2
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        f'<< /Type /Pages /Count {len(pages)} /Kids [{kids}] >>'.encode(),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Title (A Two-Page Study) /Author (A. Researcher) >>',
    ]
    for index, body in enumerate(pages):
        content_id = 6 + index * 2
        objects.append(
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 432 576] '
            f'/Resources << /Font << /F1 3 0 R /F2 {bold_font_id} 0 R >> >> '
            f'/Contents {content_id} 0 R >>'.encode()
        )
        data = body.encode('ascii')
        objects.append(f'<< /Length {len(data)} >>\nstream\n'.encode() + data + b'endstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>')
    result = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for number, item in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f'{number} 0 obj\n'.encode() + item + b'\nendobj\n')
    xref = len(result)
    result.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        result.extend(f'{offset:010d} 00000 n \n'.encode())
    result.extend(
        f'trailer\n<< /Root 1 0 R /Info 4 0 R /Size {len(offsets)} >>\n'
        f'startxref\n{xref}\n%%EOF\n'.encode()
    )
    return bytes(result)


PAGE_1 = _text(45, 510, 'A Two-Page Study') + _text(45, 470, 'Figure 9 shows a missing reference.')
PAGE_2 = (
    '45 392 200 100 re S\n'
    + _text(45, 380, 'Figure 1: Distribution of measured values')
    + _text(45, 368, 'in the independent test split.')
    + _text(45, 268, 'Table 2: Error rates before and after calibration.')
    + _text(45, 245, 'Before  0.28     After  0.12')
    + _text(45, 192, 'The following paragraph should not enter the caption.')
)


class FixedPDFSourceTests(unittest.TestCase):
    def test_derives_a_versioned_arxiv_pdf_only(self):
        self.assertEqual(pdf_evidence.pdf_source('arxiv:2611.12872', 'v1'), PDF)
        self.assertEqual(
            pdf_evidence.pdf_source('arxiv:hep-th/9901001', 'v2'),
            'https://arxiv.org/pdf/hep-th/9901001v2',
        )
        for key, label in [
            ('arxiv:2611.12872', 'hf-index'), ('arxiv:2611.12872', 'v0'),
            ('arxiv:2611.12872', 'v1\n'), ('arxiv:2611.12872', 'v01'),
            ('arxiv:../2611.12872', 'v1'), ('arxiv:2611.12872?x=1', 'v1'),
            ('https://arxiv.org/html/2611.12872', 'v1'), ('arxiv:2611.12872v1', 'v2'),
            (None, 'v1'), ('arxiv:2611.12872', None),
        ]:
            with self.subTest(key=key, label=label):
                self.assertIsNone(pdf_evidence.pdf_source(key, label))

    def test_parse_rejects_invalid_arxiv_locations_before_opening_pdf(self):
        for url in [
            'http://arxiv.org/html/2611.12872v1',
            'https://user@arxiv.org/html/2611.12872v1',
            'https://arxiv.org:443/html/2611.12872v1',
            'https://arxiv.org.evil.test/html/2611.12872v1',
            'https://arxiv.org/html/2611.12872v1?x=1',
            'https://arxiv.org/html/2611.12872v1#top',
            'https://arxiv.org/html/2611.12872',
            PDF,
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    pdf_evidence.parse(_pdf(PAGE_1), url)

    def test_reports_pdf_dependency_unavailable(self):
        with patch.object(pdf_evidence.shutil, 'which', return_value=None):
            with self.assertRaises(pdf_evidence.PDFEvidenceError) as raised:
                pdf_evidence.parse(_pdf(PAGE_1), HTML)
        self.assertEqual(raised.exception.code, 'dependency_unavailable')

    def test_rejects_incorrect_magic_and_oversized_payload(self):
        for data in [b'<html>', b'%PDF-1.4 ' + b'x' * (20 * 1024 * 1024)]:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                pdf_evidence.render_page(data, 1)


@unittest.skipUnless(TOOLS_AVAILABLE, 'Poppler and util-linux prlimit required')
class PDFFallbackTests(unittest.TestCase):
    def test_extracted_text_uses_exact_physical_pages_and_content_hash(self):
        entry, manifest = pdf_evidence.parse(_pdf(PAGE_1, PAGE_2), HTML)
        self.assertEqual(
            [entry[k] for k in ('key', 'label', 'scope', 'title', 'source_url')],
            ['arxiv:2611.12872', 'v1', 'partial', 'A Two-Page Study', PDF],
        )
        self.assertEqual(entry['fulltext_status'], 'partial')
        self.assertEqual(
            [(block['anchor'], block['source_url']) for block in entry['blocks']],
            [('page-1', PDF + '#page=1'), ('page-2', PDF + '#page=2')],
        )
        self.assertIn('independent test split.', entry['blocks'][1]['text'])
        self.assertEqual(
            entry['content_hash'],
            compute_content_hash(entry['title'], 'partial', 'v1', entry['blocks']),
        )
        self.assertEqual([manifest['source'], manifest['pdf_url'], manifest['page_count']], [HTML, PDF, 2])
        self.assertEqual(manifest['missing_text_pages'], [])

    def test_captions_are_distinct_original_page_panels_without_cell_values(self):
        _, manifest = pdf_evidence.parse(_pdf(PAGE_1, PAGE_2), HTML)
        panels = manifest['panels']
        self.assertEqual(
            [(p['kind'], p['anchor'], p['caption']) for p in panels],
            [
                ('figure', 'page-2-figure-1',
                 'Figure 1: Distribution of measured values in the independent test split.'),
                ('table', 'page-2-table-2', 'Table 2: Error rates before and after calibration.'),
            ],
        )
        for panel in panels:
            self.assertEqual(panel['page_number'], 2)
            self.assertEqual(panel['page_url'], PDF + '#page=2')
            self.assertEqual(panel['representation'], 'pdf_page')
            self.assertEqual(panel['rows'], [])
            self.assertEqual(panel['images'], [])
            self.assertTrue(panel['steps'])

    @unittest.skipUnless(shutil.which('pdftohtml'), 'Poppler pdftohtml required')
    def test_bold_labels_without_punctuation_distinguish_caption_from_prose(self):
        right = (
            _text(230, 480, 'Figure 7 shows the examples in prose.')
            + _text(230, 330, 'Figure 7', bold=True)
            + _text(276, 330, 'An unpunctuated caption')
            + _text(230, 318, 'and a second caption line.')
            + _text(23, 318, 'An unrelated line in the other column.')
        )
        _, manifest = pdf_evidence.parse(_pdf(PAGE_1, right), HTML)
        self.assertEqual(
            [(p['anchor'], p['caption']) for p in manifest['panels']],
            [('page-2-figure-7', 'Figure 7 An unpunctuated caption and a second caption line.')],
        )

    @unittest.skipUnless(shutil.which('pdftohtml'), 'Poppler pdftohtml required')
    def test_adjacent_caption_columns_are_not_interleaved(self):
        adjacent = (
            _text(45, 500, 'Table 1: Accuracy by design.')
            + _text(45, 488, 'Each row represents one task.')
            + _text(191, 500, 'Table 2: Error by sample.')
            + _text(191, 488, 'Each row is a new dataset.')
        )
        _, manifest = pdf_evidence.parse(_pdf(adjacent), HTML)
        self.assertEqual(
            [(p['anchor'], p['caption']) for p in manifest['panels']],
            [
                ('page-1-table-1', 'Table 1: Accuracy by design. Each row represents one task.'),
                ('page-1-table-2', 'Table 2: Error by sample. Each row is a new dataset.'),
            ],
        )

    def test_ambiguous_duplicate_labels_never_claim_a_page_match(self):
        repeated = _text(45, 510, 'Figure 1: A second unrelated label.')
        _, manifest = pdf_evidence.parse(_pdf(PAGE_1, PAGE_2, repeated), HTML)
        self.assertEqual([p['kind'] for p in manifest['panels']], ['table'])
        self.assertGreater(manifest['unresolved_captions'], 0)

    def test_missing_text_pages_do_not_shift_citations(self):
        _, manifest = pdf_evidence.parse(_pdf(PAGE_1, '45 400 100 80 re S\n', PAGE_2), HTML)
        self.assertEqual(manifest['missing_text_pages'], [2])
        self.assertEqual(manifest['panels'][0]['page_number'], 3)

    def test_image_only_document_reports_no_text_but_can_render_original_page(self):
        raw = _pdf('45 400 100 80 re S\n')
        with self.assertRaises(pdf_evidence.PDFEvidenceError) as raised:
            pdf_evidence.parse(raw, HTML)
        self.assertEqual(raised.exception.code, 'no_text')
        self.assertTrue(pdf_evidence.render_page(raw, 1).startswith(b'\x89PNG\r\n\x1a\n'))

    def test_missing_captions_are_explicit(self):
        _, manifest = pdf_evidence.parse(_pdf(PAGE_1), HTML)
        self.assertEqual(manifest['state'], 'unavailable')
        self.assertEqual(manifest['reason'], 'no_reliable_captions')
        self.assertEqual(manifest['panels'], [])

    def test_rendering_returns_the_requested_original_page_as_bounded_png(self):
        raw = _pdf(PAGE_1, PAGE_2)
        png = pdf_evidence.render_page(raw, 2)
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(png[12:16], b'IHDR')
        width, height = struct.unpack('>II', png[16:24])
        self.assertGreater(width, 720)
        self.assertLessEqual(max(width, height), 1600)
        self.assertNotEqual(png, pdf_evidence.render_page(raw, 1))
        for invalid in [0, 3, True, '2']:
            with self.subTest(page=invalid), self.assertRaises(ValueError):
                pdf_evidence.render_page(raw, invalid)

    def test_rejects_documents_over_the_page_budget(self):
        with self.assertRaises(pdf_evidence.PDFEvidenceError) as raised:
            pdf_evidence.parse(_pdf(*([PAGE_1] * 121)), HTML)
        self.assertEqual(raised.exception.code, 'page_limit')

    def test_bounded_runner_reports_timeout(self):
        with patch.object(
            pdf_evidence.subprocess, 'run',
            side_effect=subprocess.TimeoutExpired('pdfinfo', 1),
        ):
            with self.assertRaises(pdf_evidence.PDFEvidenceError) as raised:
                pdf_evidence.parse(_pdf(PAGE_1), HTML)
        self.assertEqual(raised.exception.code, 'timeout')
