"""Synthetic checks for complete resumption and visible public background evidence."""
from html import escape
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from wsgiref.util import setup_testing_defaults

from checks.test_study import entry, FakeProvider
from knowledge_hub.connections import ConnectionService, reading_url
from knowledge_hub.hosting import MountedApplication
from knowledge_hub.reading import ReadingState
from knowledge_hub.reader_views import resumable
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.study import StudyService
from knowledge_hub.web import create_app


class ReadingContinuation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'synthetic.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.reading = ReadingState(self.store)
        self.provider = FakeProvider()

    def request(self, path, *, hosted=False):
        app = create_app(self.db, study_provider=self.provider)
        env = {}; setup_testing_defaults(env)
        parsed = urlsplit(path)
        env.update(PATH_INFO=parsed.path, QUERY_STRING=parsed.query, HTTP_HOST='127.0.0.1:8765')
        if hosted:
            app = MountedApplication(app, origin='https://momonong.me', prefix='/knowledge',
                                     authorize=lambda _: True)
            env.update(HTTP_HOST='momonong.me', HTTP_X_FORWARDED_PROTO='https', REMOTE_ADDR='127.0.0.1')
        result = {}
        def start(status, headers):
            result.update(status=int(status.split()[0]), headers=dict(headers))
        result['body'] = b''.join(app(env, start)).decode()
        return result

    def drafts(self, count):
        pairs = []
        for i in range(count):
            item, version, _ = self.store.upsert_parsed_entry(entry(
                f'arxiv:2601.{i:05}', f'Synthetic draft {i}', f'Unique reading evidence number {i}.'))
            self.reading.save(item, version, 0, {'anchor':'p1', 'tab':'original',
                                                'reflection':f'Draft {i} <script>private()</script>'})
            pairs.append((item, str(version)))
        return pairs

    @staticmethod
    def history_pairs(body):
        return {(int(i), v) for i, v in re.findall(r'href="/(?:knowledge/)?items/(\d+)\?version=(\d+)#', body)}

    def backgrounds(self):
        focal, version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2601.00100', 'Episodic memory retrieval with consolidation',
            'Episodic memory retrieval consolidates stored observations before answering.'))
        title = 'Memory retrieval <script>public_title()</script>'
        quote = 'Episodic memory retrieval uses <script>public_quote()</script> similarity over stored observations.'
        other, other_version, _ = self.store.upsert_parsed_entry(entry('arxiv:2401.00100', title, quote))
        self.store.add_note(other, other_version, 'p1', 'PRIVATE_BACKGROUND_NOTE')
        progress = StudyService(self.store).save_progress(other, other_version, 'saved-background', None,
                                                        'PRIVATE_SAVED_UNDERSTANDING', 'PRIVATE_QUESTION')
        return focal, version, other, other_version, title, quote, progress

    def test_sixth_draft_is_reachable_and_keeps_old_version_anchor_and_escaping(self):
        pairs = self.drafts(6)
        oldest, old_version = pairs[0]
        self.store.upsert_parsed_entry(entry('arxiv:2601.00000', 'Changed newest title', 'A newer original.'))
        page = self.request('/reading')
        self.assertEqual(page['status'], 200)
        self.assertEqual(self.history_pairs(page['body']), set(pairs))
        self.assertIn(reading_url(oldest, old_version, 'p1'), page['body'])
        self.assertIn('Synthetic draft 0', page['body'])
        self.assertNotIn('Changed newest title', page['body'])
        self.assertIn('&lt;script&gt;private()&lt;/script&gt;', page['body'])
        self.assertNotIn('<script>private()', page['body'])
        self.assertEqual(len(resumable(self.store, self.reading)), 5)
        home = self.request('/')['body']
        aside = home.split('<aside class="briefing-rail"', 1)[1].split('</aside>', 1)[0]
        self.assertEqual(self.history_pairs(aside), set(pairs[-5:]))
        self.assertIn('href="/reading"', aside)

    def test_bounded_pages_include_every_draft_and_keep_mounted_links(self):
        pairs = self.drafts(23)
        first = self.request('/knowledge/reading', hosted=True)
        self.assertEqual(first['status'], 200)
        self.assertEqual(len(self.history_pairs(first['body'])), 20)
        self.assertIn('href="/knowledge/reading?page=2"', first['body'])
        second = self.request('/knowledge/reading?page=2', hosted=True)
        self.assertEqual(len(self.history_pairs(second['body'])), 3)
        self.assertIn('href="/knowledge/reading?page=1"', second['body'])
        self.assertNotIn('rel="next"', second['body'])
        self.assertFalse(self.history_pairs(first['body']) & self.history_pairs(second['body']))
        self.assertEqual(self.history_pairs(first['body']) | self.history_pairs(second['body']), set(pairs))
        self.assertNotIn('href="/items/', second['body'])
        self.assertNotIn('href="/reading', second['body'])

    def test_saved_understanding_without_draft_is_included_and_versions_are_not_duplicated(self):
        pairs = self.drafts(6)
        service = StudyService(self.store)
        item, version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2401.00001', 'Previously saved understanding', 'A preserved original.'))
        service.save_progress(item, version, 'older-progress', None, 'Older saved text', '')
        saved = service.save_progress(item, version, 'newer-progress', None, 'Latest saved text', '')
        service.save_progress(*pairs[-1], 'also-has-draft', None, 'Confirmed understanding', '')
        page = self.request('/reading')['body']
        self.assertEqual(self.history_pairs(page), set(pairs) | {(item, str(version))})
        self.assertEqual(page.count('<article>'), 7)
        self.assertIn(reading_url(item, version, f'progress-{saved["id"]}'), page)
        self.assertNotIn('Older saved text', page)
        self.assertIn('Latest saved text', page)

    def test_page_bounds_and_empty_state(self):
        self.assertIn('從一篇文章開始', self.request('/reading')['body'])
        for value in ('0', '-1', 'word', '1.5', '1' * 30):
            with self.subTest(page=value):
                self.assertEqual(self.request('/reading?page=' + value)['status'], 400)
        pairs = self.drafts(6)
        self.assertEqual(self.history_pairs(self.request('/reading?page=999999')['body']), set(pairs))

    def test_generic_background_renders_real_evidence_and_private_context_stays_separate(self):
        focal, version, other, other_version, title, quote, _ = self.backgrounds()
        context = StudyService(self.store).context(focal, version)
        background = next(s for s in context['sources'] if s['item_id'] == other)
        self.assertNotIn('PRIVATE_', json.dumps(context['sources']))
        page = self.request(f'/knowledge/items/{focal}?version={version}', hosted=True)['body']
        self.assertIn(escape(title), page)
        self.assertIn(escape(background['reason']), page)
        self.assertIn(escape(quote), page)
        self.assertIn('/knowledge' + reading_url(other, other_version, 'p1'), page)
        self.assertIn('閱讀建議，尚未生成教案', page)
        self.assertNotIn('目前沒有足夠原文能推薦先備教材', page)
        self.assertNotIn('<script>public_', page)
        section = page.split('class="background-item"')[1].split('</details>')[0]
        self.assertNotIn('PRIVATE_', section)
        self.assertIn('PRIVATE_SAVED_UNDERSTANDING', page)
        self.assertIn('data-private', page)
        self.assertEqual(self.provider.calls, [])

    def test_curated_background_is_not_repeated_as_generic_suggestion(self):
        focal, version, other, other_version, title, quote, _ = self.backgrounds()
        context = ConnectionService(self.store).build_context(focal, version)
        context['journey']['steps'] = [{'reason':'策展先備理由', 'guide':{
            'item_id':other, 'version_id':other_version, 'title':title,
            'mechanism':'策展機制', 'example':'策展例子', 'evidence':[]}}]
        with patch.object(ConnectionService, 'build_context', return_value=context):
            page = self.request(f'/items/{focal}?version={version}')['body']
        self.assertEqual(page.count('class="background-item"'), 1)
        self.assertIn('策展先備理由', page)
        self.assertEqual(self.provider.calls, [])

    def test_context_failure_is_reported_without_claiming_no_background_exists(self):
        focal, version, *_ = self.backgrounds()
        with patch('knowledge_hub.study.public_context', side_effect=ValueError('Synthetic retrieval failure')):
            response = self.request(f'/items/{focal}?version={version}')
        self.assertEqual(response['status'], 200)
        self.assertIn('Synthetic retrieval failure', response['body'])
        self.assertNotIn('目前沒有足夠原文能推薦先備教材', response['body'])
        self.assertEqual(self.provider.calls, [])

    def test_completed_lesson_and_qa_show_saved_system_history_limit_without_relying_on_answer(self):
        focal, version, *_ = self.backgrounds()
        limits = ('僅傳有限公開片段。 對話因輸入容量限制已省略部分較早內容；'
                  '完整歷史仍保存在知識庫。 <script>system_notice()</script>')
        for mode in ('lesson', 'qa'):
            with self.subTest(mode=mode):
                record = {'id':101, 'item_id':focal, 'version_id':str(version), 'state':'completed',
                          'mode':mode, 'prompt':'A public question', 'payload':{'limitations':limits},
                          'answer':{'sections':[{'role':'mechanism' if mode == 'lesson' else 'answer',
                              'kind':'author_claim', 'title':'Saved complete answer',
                              'text':'The complete stored answer remains unchanged.', 'citations':[]}],
                              'limitations':'Model limitation without history truncation notice.'}}
                original = copy.deepcopy(record)
                with patch.object(StudyService, 'records', return_value=[record]):
                    page = self.request(f'/items/{focal}?version={version}')['body']
                self.assertEqual(page.count('data-context-limitations'), 1)
                self.assertIn(escape(limits), page)
                self.assertNotIn('<script>system_notice()', page)
                self.assertIn(record['answer']['sections'][0]['text'], page)
                self.assertEqual(record, original)
        self.assertEqual(self.provider.calls, [])


if __name__ == '__main__':
    unittest.main()
