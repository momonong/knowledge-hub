"""Reading room keeps fixed-edition content and older actions in predictable regions."""
from html.parser import HTMLParser
import unittest
from unittest.mock import Mock

from knowledge_hub import reader_views


class Regions(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'wbr'}

    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs, tuple(self.stack)))
        if tag not in self.VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def by_id(self, value):
        matches = [row for row in self.elements if row[1].get('id') == value]
        if len(matches) != 1:
            raise AssertionError(f'Expected one element for {value}, got {len(matches)}')
        return matches[0]


def render_fixture():
    url = 'https://arxiv.org/html/2611.00001v2'
    item = {'id': 4, 'key': 'arxiv:2611.00001', 'title': 'Test paper',
            'notes': [], 'questions': [], 'connections': []}
    version = {'id': '7', 'label': 'v2', 'scope': 'full', 'blocks': [
                   {'anchor': f'S2.p{n}', 'text': 'The measured source text. ' + str(n) + (' Results detail.' * 22)}
                    for n in range(1, 16)],
               'published_at': '', 'fetched_at': '', 'source_url': url}
    service = Mock(provider=None)
    service.records.return_value = []
    service.progress.return_value = []
    service.unresolved.return_value = False
    service.availability.return_value = {'available': False, 'reason': ''}
    service.context.return_value = {'sources': []}
    reading = Mock()
    reading.get.return_value = {'tab': 'original', 'anchor': 'S2.p1', 'revision': 3, 'reflection': '',
                                 'open_questions': '', 'question_draft': 'Return to the draft.'}
    reading.suggest_summary.return_value = {'understanding': ''}
    return reader_views.detail(item, version, {'focal': {}}, service, reading, 'csrf', str)


class ReadingRoomTests(unittest.TestCase):
    def test_same_room_preserves_version_anchors_and_hides_only_supplementary_content(self):
        raw = render_fixture()
        page = Regions(raw)

        def ancestors(value):
            return [attrs.get('id') for _, attrs in page.by_id(value)[2]]

        reader = page.by_id('reader-start')[1]
        self.assertIn('data-reading-room', reader)
        self.assertEqual((reader['data-tab'], reader['data-anchor']), ('original', 'S2.p1'))
        guide = page.by_id('layer-guide')
        self.assertIn('panel-lesson', ancestors('layer-guide'))
        self.assertFalse(any('hidden' in attrs for _, attrs in guide[2]))
        self.assertIn('panel-original', ancestors('figures'))
        self.assertIn('panel-original', ancestors('S2.p1'))
        self.assertTrue(any(attrs.get('href') == '/items/4/pdf?version=7'
                            and any(el.get('id') == 'panel-original' for _, el in parents)
                            for tag, attrs, parents in page.elements if tag == 'a'))
        self.assertFalse(any(tag == 'iframe' for tag, _, _ in page.elements))
        self.assertIn('conversation', ancestors('followup'))
        self.assertIn('hidden', page.by_id('conversation')[1])
        self.assertTrue(any(tag == 'details' and 'open' not in attrs
                            for tag, attrs in page.by_id('panel-notes')[2]))
        self.assertTrue(any(tag == 'details' and 'open' not in attrs
                            for tag, attrs in page.by_id('study')[2]))
        self.assertIn('Return to the draft.', raw)
