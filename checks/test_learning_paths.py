"""Learning paths must be evidence-bound, useful without a provider, and read-only."""
import copy
import html
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

from knowledge_hub import teaching, learning
from knowledge_hub.connection_views import render_journey
from knowledge_hub.library import KnowledgeLibrary
import test_teaching as teaching_fixtures
from worker import test_connection_views as view_fixtures


def guide_version(sid, vid):
    guide = teaching.GUIDES[sid]
    grouped = {}
    for evidence in guide['evidence']:
        grouped.setdefault(evidence['anchor'], []).append(evidence['quote'])
    return {'id': str(vid), 'scope': 'partial', 'source_url': guide['source_url'],
            'content_hash': guide['content_hash'], 'published_at': '2026-11-09T00:00:00Z',
            'blocks': [{'anchor': a, 'text': '\n'.join(q)} for a, q in grouped.items()]}


class LearningPathTests(unittest.TestCase):
    def setUp(self):
        self.items, self.mappings = {}, []
        for i, sid in enumerate(teaching.GUIDES, 100):
            self.items[i] = {'id': i, 'versions': [guide_version(sid, i + 200)]}
            self.mappings.append({'source_id': sid, 'item_id': i, 'version_id': i + 200})

    def journey(self, title, text, *, sid=None, focus='', scope='abstract'):
        version = {'id': '99', 'scope': scope, 'source_url': 'https://example.org/new',
                   'content_hash': 'unreviewed', 'blocks': [{'anchor': 'abstract', 'text': text}]}
        if sid:
            version = guide_version(sid, 99)
        focal = {'item_id': 9, 'title': title,
                 'teaching_guide': teaching.build_teaching_guide(sid, version, 9)}
        return learning.build_journey(focal, version, self.mappings, self.items, focus)

    def test_curated_daily_path_contains_inline_background_and_two_sided_evidence(self):
        path = self.journey('HiVe-Mem', '', sid='daily-hive-mem')
        self.assertEqual(path['mode'], 'curated')
        self.assertEqual([s['source_id'] for s in path['steps']],
                         ['rag-original-rag', 'rag-graphrag', 'rag-longmemeval', 'mm-clip'])
        self.assertEqual(len(path['comparisons']), 4)
        for comparison in path['comparisons']:
            self.assertEqual(len(comparison['evidence']), 2)
            self.assertEqual(comparison['evidence'][0]['item_id'], 9)
            self.assertNotEqual(comparison['evidence'][1]['item_id'], 9)
        page = render_journey(path, html.escape)
        for step in path['steps']:
            self.assertIn(html.escape(step['guide']['mechanism']), page)
            self.assertIn(html.escape(step['guide']['answer']), page)
        self.assertIn('同頁學習路徑', page)

    def test_new_unseen_article_gets_explainable_suggestions_without_curated_claims(self):
        path = self.journey('New multimodal retrieval study',
                            'We align images and text, then retrieve relevant evidence.')
        ids = {s['source_id'] for s in path['steps']}
        self.assertIn('mm-clip', ids)
        self.assertIn('rag-original-rag', ids)
        self.assertEqual(path['mode'], 'suggested')
        self.assertFalse(path['comparisons'])
        self.assertTrue(all(s['reason'] for s in path['steps']))
        preset = teaching.lesson_preset(None, path)
        self.assertIsNotNone(preset)
        self.assertIn('本篇尚無策展解說', preset['blueprint_md'])
        self.assertIn('https://example.org/new', preset['blueprint_md'])
        self.assertIn('CLIP', preset['blueprint_md'])
        self.assertNotIn('## 跨篇比較', preset['blueprint_md'])

    def test_hardware_memory_does_not_imply_conversational_memory_and_references_are_ignored(self):
        path = self.journey('Memory bandwidth of GPU kernels',
                            'Operational intensity bounds throughput.')
        self.assertEqual([s['source_id'] for s in path['steps']], ['sys-roofline'])
        unrelated = self.journey('Plant morphology', 'A study of leaves and stems.')
        self.assertFalse(unrelated['steps'])
        self.assertEqual(unrelated['mode'], 'unavailable')

    def test_missing_or_changed_background_never_supports_curated_comparison(self):
        for mapping in self.mappings:
            if mapping['source_id'] == 'rag-graphrag':
                self.items[mapping['item_id']]['versions'][0]['content_hash'] = 'changed'
        path = self.journey('HiVe-Mem', '', sid='daily-hive-mem')
        self.assertNotIn('rag-graphrag', [s['source_id'] for s in path['steps']])
        self.assertNotIn('rag-graphrag', [s['source_id'] for s in path['comparisons']])
        self.assertTrue(path['missing'])

    def test_focus_is_labelled_and_unknown_revision_cannot_reuse_curated_daily_teaching(self):
        path = self.journey('Unreviewed paper', 'No detailed source yet.', focus='語音轉錄')
        self.assertEqual([s['source_id'] for s in path['steps']], ['mm-whisper'])
        self.assertIn('指定焦點', path['steps'][0]['reason'])
        v = guide_version('daily-hive-mem', 99)
        self.assertIsNotNone(teaching.build_teaching_guide(None, v, 9))
        v['content_hash'] = 'changed'
        self.assertIsNone(teaching.build_teaching_guide(None, v, 9))

    def test_journey_and_preset_do_not_mutate_guides_and_escape_text(self):
        before = copy.deepcopy(teaching.GUIDES)
        path = self.journey('HiVe-Mem', '', sid='daily-hive-mem')
        guide = teaching.build_teaching_guide('daily-hive-mem', guide_version('daily-hive-mem', 99), 9)
        preset = teaching.lesson_preset(guide, path)
        self.assertIn('同頁學習路徑', preset['blueprint_md'])
        self.assertIn('跨篇比較', preset['blueprint_md'])
        self.assertLess(len(preset['blueprint_md']), 16000)
        path['steps'][0]['reason'] = '<img src=x onerror=bad()>'
        self.assertNotIn('<img', render_journey(path, html.escape))
        self.assertEqual(before, teaching.GUIDES)

    def test_later_unreviewed_snapshot_keeps_only_explicit_older_background_revision(self):
        mapping = next(m for m in self.mappings if m['source_id'] == 'rag-graphrag')
        newer = copy.deepcopy(self.items[mapping['item_id']]['versions'][0])
        newer.update(id='999', content_hash='unreviewed')
        self.items[mapping['item_id']]['versions'].append(newer)
        self.mappings.append({**mapping, 'version_id': 999})
        path = self.journey('HiVe-Mem', '', sid='daily-hive-mem')
        step = next(s for s in path['steps'] if s['source_id'] == 'rag-graphrag')
        self.assertEqual(step['guide']['version_id'], str(mapping['version_id']))

    def test_bibliography_and_late_mentions_do_not_seed_background_topics(self):
        version = {'blocks': [{'anchor': 'abstract', 'text': 'Plant morphology. ' * 500},
                              {'anchor': 'S8', 'text': 'retrieval augmented generation'},
                              {'anchor': 'bib.bib1', 'text': 'CLIP multimodal agents'}]}
        self.assertEqual(learning.topic_matches('Plant morphology', version), [])
        self.assertEqual(learning.available_guides([], {}), {})


class LearningWebTests(unittest.TestCase):
    request = teaching_fixtures.TeachingWebTests.request
    snapshot = teaching_fixtures.TeachingWebTests.snapshot

    def setUp(self):
        teaching_fixtures.TeachingWebTests.setUp(self)
        raw = b'Long-term memory needs temporal reasoning and knowledge updates.'
        d = KnowledgeLibrary(self.store).add_document('sample-agent', raw, media_type='text/plain',
            source_url='https://example.org/memory', version_label='fixture-memory',
            blocks=[{'anchor': 'p1', 'text': raw.decode(), 'source_url': 'https://example.org/memory#p1'}],
            scope='partial', extraction_note='synthetic', rights_note='synthetic')
        v = self.store.get_item(d['item_id'])['versions'][0]
        guide = copy.deepcopy(teaching.GUIDES['rag-longmemeval'])
        guide.update(source_url=v['source_url'], content_hash=v['content_hash'],
                     evidence=[{'anchor': 'p1', 'quote': raw.decode()}])
        patch.dict(teaching.GUIDES, {'sample-agent': guide}).start()
        patch.dict(learning.PREREQUISITES, {'sample-rag': ['sample-agent']}).start()
        self.background = guide

    def test_path_is_inline_unique_and_get_preserves_database(self):
        before = self.snapshot()
        status, _, raw = self.request(f'/items/{self.item}?version={self.version}')
        page = raw.decode()
        self.assertEqual(status, 200)
        self.assertIn(html.escape(self.background['mechanism']), page)
        self.assertIn(html.escape(self.background['answer']), page)
        self.assertIn('## 同頁學習路徑', page)
        self.assertEqual(page.count('id="reading-guide"'), 1)
        self.assertEqual(before, self.snapshot())
        status, _, raw = self.request('/manage')
        self.assertEqual(status, 200)
        self.assertIn('核心論文教學（1 篇可讀）', raw.decode())

    def test_manual_save_keeps_background_in_blueprint_and_does_not_mark_mastery(self):
        class Blueprint(HTMLParser):
            active = False
            value = ''
            def handle_starttag(self, tag, attrs):
                if tag == 'textarea': self.active = dict(attrs).get('name') == 'blueprint_md'
            def handle_endtag(self, tag):
                if tag == 'textarea': self.active = False
            def handle_data(self, data):
                if self.active: self.value += data
        _, _, raw = self.request(f'/items/{self.item}?version={self.version}')
        parser = Blueprint(); parser.feed(raw.decode())
        before = self.snapshot()
        md = parser.value + '\n我的理解：要核對知識更新。'
        status, _, _ = self.request(f'/items/{self.item}/lessons', {
            'csrf': self.store.get_csrf_token(), 'version_id': self.version,
            'title': '串連學習測試', 'learning_goal': '比較背景與新知',
            'blueprint_md': md, 'origin': 'agent_assisted'})
        self.assertEqual(status, 303)
        saved = self.store.get_lessons()[0]
        self.assertEqual(saved['blueprint_md'], md)
        self.assertIn(self.background['mechanism'], md)
        after = self.snapshot()
        for table in before:
            if table not in ('lessons', 'sqlite_sequence'):
                self.assertEqual(before[table], after[table], table)


class LearningPreviewTests(unittest.TestCase):
    def test_verified_method_quote_is_previewed_instead_of_unreviewed_extraction(self):
        from knowledge_hub.connection_views import render
        context = view_fixtures.ReadingViewTests().context()
        guide = teaching.build_teaching_guide('mm-clip', guide_version('mm-clip', 23), 22)
        context['related'][0]['teaching_guide'] = guide
        class Service:
            provider = None
            def records(self, item_id): return []
        page = render(Service(), 1, '1', 'test-csrf', '', html.escape, context=context)
        visible = ''.join(view_fixtures.Visible(page).text)
        self.assertIn(guide['evidence'][0]['quote'], visible)
        self.assertNotIn(context['related'][0]['excerpts'][0]['quote'][:160], visible)
        self.assertIn(html.escape(context['related'][0]['excerpts'][0]['quote']), page)
