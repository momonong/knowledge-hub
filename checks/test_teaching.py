"""Version binding, safe display and explicit persistence of curated lessons."""
import copy
import html
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge_hub import teaching
from knowledge_hub.connections import ConnectionService
from knowledge_hub.connection_views import render_teaching
from knowledge_hub.library import KnowledgeLibrary
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from test_initial_library import seed
import test_lesson_workflow as lesson_fixtures


class TeachingBindingTests(unittest.TestCase):
    def fixture(self, sid):
        guide = teaching.GUIDES[sid]
        grouped = {}
        for cite in guide['evidence']:
            grouped.setdefault(cite['anchor'], []).append(cite['quote'])
        return {'id': '91', 'scope': 'partial', 'source_url': guide['source_url'],
                'content_hash': guide['content_hash'],
                'blocks': [{'anchor': anchor, 'text': '\n'.join(quotes)}
                           for anchor, quotes in grouped.items()]}

    def test_all_guides_bind_to_the_supplied_item_version_and_evidence(self):
        for sid in teaching.GUIDES:
            with self.subTest(sid=sid):
                version = self.fixture(sid)
                guide = teaching.build_teaching_guide(sid, version, 73)
                self.assertIsNotNone(guide)
                for cite in guide['evidence']:
                    self.assertIn('/items/73?version=91#', cite['reading_url'])
                    block = next(b for b in version['blocks'] if b['anchor'] == cite['anchor'])
                    self.assertIn(cite['quote'], block['text'])
                preset = teaching.lesson_preset(guide)
                self.assertIn(guide['question'], preset['blueprint_md'])
                self.assertIn(guide['answer'], preset['blueprint_md'])
                self.assertIn(guide['source_url'], preset['blueprint_md'])
                self.assertLess(len(preset['blueprint_md']), 16000)
                self.assertEqual(preset['origin'], 'agent_assisted')

    def test_mismatched_revision_source_and_missing_evidence_cannot_enable_guide(self):
        sid = 'rag-dpr'
        original = self.fixture(sid)
        for change in ({'content_hash': 'new-revision'}, {'source_url': 'https://example.org/copy'},
                       {'scope': 'unavailable'}, {'blocks': []},
                       {'blocks': [{'anchor': 'wrong-page', 'text': original['blocks'][0]['text']}]},
                       {'blocks': [{'anchor': 'page-3', 'text': teaching.GUIDES[sid]['evidence'][0]['quote']}]}):
            with self.subTest(change=change):
                self.assertIsNone(teaching.build_teaching_guide(sid, {**original, **change}, 73))
        self.assertIsNone(teaching.build_teaching_guide('unknown', original, 73))
        self.assertIsNone(teaching.lesson_preset(None))

    def test_guide_and_card_text_are_escaped_and_answer_starts_collapsed(self):
        guide = teaching.build_teaching_guide('rag-dpr', self.fixture('rag-dpr'), 73)
        guide['mechanism'] = '<script>not-a-script()</script>'
        brief = {'summary_zh': '<img src=x onerror=bad()>', 'prerequisite_concepts': ['<unsafe>']}
        page = render_teaching({'teaching_guide': guide, 'reading_brief': brief}, html.escape)
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img src=', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('<details><summary>展開參考答案</summary>', page)
        self.assertIn('href="#lesson-draft"', page)


class TeachingWebTests(unittest.TestCase):
    def request(self, path, *args, **kwargs):
        # Blueprint editing is preserved in the secondary management view.
        if not args and not kwargs and path.startswith('/items/'):
            path += ('&' if '?' in path else '?') + 'view=manage'
        return lesson_fixtures.LessonWorkflowAcceptance.request(self, path, *args, **kwargs)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'hub.sqlite3'
        self.store = KnowledgeStore(self.db)
        lib = KnowledgeLibrary(self.store)
        lib.import_seed(*seed())
        raw = b'The retriever selects relevant passages before generating the answer.'
        self.doc = lib.add_document('sample-rag', raw, media_type='text/plain',
            source_url='https://example.org/rag', version_label='fixture-v1',
            blocks=[{'anchor': 'p1', 'text': raw.decode(), 'source_url': 'https://example.org/rag#p1'}], scope='partial',
            extraction_note='synthetic fixture', rights_note='synthetic')
        self.item, self.version = self.doc['item_id'], self.doc['version_id']
        self.v = self.store.get_item(self.item)['versions'][0]
        guide = copy.deepcopy(teaching.GUIDES['rag-dpr'])
        guide.update(source_url=self.v['source_url'], content_hash=self.v['content_hash'],
                     evidence=[{'anchor': 'p1', 'quote': 'The retriever selects relevant passages'}])
        self.addCleanup(patch.stopall)
        patch.dict(teaching.GUIDES, {'sample-rag': guide}).start()
        self.app = create_app(self.db)
        self.store.get_csrf_token()

    def snapshot(self):
        with self.store._connect() as c:
            names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {name: [tuple(r) for r in c.execute('SELECT * FROM "' + name + '"')]
                    for name in names}

    def test_get_prefills_without_saving_or_changing_any_database_row(self):
        before = self.snapshot()
        code, _, raw = self.request(f'/items/{self.item}?version={self.version}')
        page = raw.decode()
        self.assertEqual(code, 200)
        self.assertIn('編輯這份學習稿', page)
        self.assertIn('# DPR：', page)
        self.assertIn('value="agent_assisted" selected', page)
        self.assertNotIn('name="parent_id"', page)
        self.assertEqual(before, self.snapshot())

    def test_prefill_requires_post_and_preserves_user_edits(self):
        class Textarea(HTMLParser):
            def __init__(self, page):
                super().__init__(); self.active = False; self.value = ''; self.feed(page)
            def handle_starttag(self, tag, attrs):
                if tag == 'textarea': self.active = dict(attrs).get('name') == 'blueprint_md'
            def handle_endtag(self, tag):
                if tag == 'textarea': self.active = False
            def handle_data(self, data):
                if self.active: self.value += data
        _, _, raw = self.request(f'/items/{self.item}?version={self.version}')
        md = Textarea(raw.decode()).value + '\n\n我的教學調整：補一個反例。'
        status, headers, _ = self.request(f'/items/{self.item}/lessons', {
            'csrf': self.store.get_csrf_token(), 'version_id': self.version, 'title': '手動修訂教學',
            'learning_goal': '核對檢索與生成的分工', 'blueprint_md': md, 'origin': 'agent_assisted'})
        self.assertEqual(status, 303)
        lesson = self.store.get_lessons()[0]
        self.assertEqual(lesson['blueprint_md'], md)
        self.assertEqual(str(lesson['version_id']), str(self.version))
        self.assertIsNone(lesson['parent_id'])
        self.assertIn('/lessons/', headers['Location'])

    def test_uncovered_revision_keeps_curated_card_but_uses_blank_template(self):
        teaching.GUIDES['sample-rag']['content_hash'] = 'different-revision'
        context = ConnectionService(self.store).build_context(self.item, self.version)
        self.assertEqual(context['focal']['reading_brief']['summary_zh'], '跨語言檢索與證據定位')
        self.assertIsNone(context['focal']['teaching_guide'])
        code, _, raw = self.request(f'/items/{self.item}?version={self.version}')
        self.assertEqual(code, 200)
        self.assertIn('策展導讀', raw.decode())
        self.assertNotIn('編輯這份學習稿', raw.decode())
        self.assertIn('value="manual" selected', raw.decode())
