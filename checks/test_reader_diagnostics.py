"""Offline reader/WSGI engineering checks, not model-quality acceptance."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

from test_study import entry, FakeProvider
from knowledge_hub import reader_views
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.study import StudyService
from knowledge_hub.study_views import DIAGNOSTIC_LABELS, diagnostic_label
from knowledge_hub.web import create_app


class ReaderDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'hub.db'
        self.store = KnowledgeStore(self.path)
        self.item, self.version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2601.00001', 'Episodic memory retrieval',
            'Episodic memory retrieval stores observations before generation.'))
        self.other, self.other_version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2401.00001', 'Memory retrieval baseline',
            'Episodic memory retrieval uses similarity search over observations.'))
        self.provider = FakeProvider()
        self.service = StudyService(self.store, self.provider)
        self.app = create_app(self.path, study_provider=self.provider)

    def generate(self, request='reader-test', mode='lesson'):
        return self.service.generate(self.item, self.version, request, mode, '理解機制')

    def page(self):
        before = self.service.records(self.item)
        events = {r['id']: self.service.events(r['id']) for r in before}
        calls = len(self.provider.calls)
        env = {}
        setup_testing_defaults(env)
        env.update(PATH_INFO=f'/items/{self.item}', QUERY_STRING=f'version={self.version}',
                   HTTP_HOST='127.0.0.1:8765', SERVER_PORT='8765',
                   REQUEST_METHOD='GET', **{'wsgi.input': io.BytesIO()})
        status = []
        with patch.object(self.provider, 'submit', side_effect=AssertionError('render submitted')), \
             patch.object(self.provider, 'poll', side_effect=AssertionError('render polled')), \
             patch.object(self.provider, 'prepare', side_effect=AssertionError('render prepared')), \
             patch.object(reader_views, 'detail', wraps=reader_views.detail) as detail:
            body = b''.join(self.app(env, lambda code, headers: status.append(code))).decode()
        self.assertEqual(status, ['200 OK'])
        detail.assert_called_once()
        self.assertIn('data-reader ', body)
        self.assertEqual(self.service.records(self.item), before)
        self.assertEqual({r['id']: self.service.events(r['id']) for r in before}, events)
        self.assertEqual(len(self.provider.calls), calls)
        return body

    def test_lesson_shape(self):
        self.provider.reply = lambda payload: {'rejected': 'RAW_ANSWER_SENTINEL'}
        row = self.generate()
        self.assertEqual(row['state'], 'failed')
        page = self.page()
        self.assertIn(DIAGNOSTIC_LABELS['answer_shape'], page)
        self.assertNotIn('RAW_ANSWER_SENTINEL', page)
        self.assertNotIn(f'/study/{row["id"]}/reconcile', page)

    def test_comparison_missing_background_citation(self):
        original = self.provider.reply
        def missing(payload):
            result = original(payload)
            self.assertGreater(len(payload['sources']), 1)
            result['sections'][3]['citations'] = result['sections'][3]['citations'][:1]
            return result
        self.provider.reply = missing
        self.generate()
        self.assertIn(DIAGNOSTIC_LABELS['comparison_sources'], self.page())

    def test_qa_unknown_keeps_original_query_and_csrf(self):
        self.provider.mode = 'timeout'
        row = self.generate(mode='qa')
        page = self.page()
        self.assertIn(DIAGNOSTIC_LABELS['transport_unknown'], page)
        self.assertIn(f'action="/study/{row["id"]}/reconcile"', page)
        self.assertIn('查詢原工作', page)
        self.assertIn(f'name="csrf" value="{self.store.get_csrf_token()}"', page)
        self.assertIn('disabled aria-disabled="true"', page)

    def test_pending_keeps_query_form(self):
        self.provider.mode = 'pending'
        row = self.generate()
        page = self.page()
        self.assertIn('教學正在準備中，可查詢原工作進度。', page)
        self.assertIn(f'action="/study/{row["id"]}/reconcile"', page)
        self.assertNotIn('data-study-diagnostic', page)

    def test_legacy_without_event_falls_back(self):
        self.provider.reply = lambda payload: {}
        row = self.generate()
        with self.store._connect() as conn:
            conn.execute('DELETE FROM study_events WHERE record_id=?', (row['id'],))
        self.assertIn(diagnostic_label({}, 'failed'), self.page())

    def test_malicious_metadata_is_not_rendered(self):
        self.provider.mode = 'timeout'
        row = self.generate(mode='qa')
        malicious = {'state': 'unknown', 'reason_code': '<script>REASON_SECRET</script>',
                     'validation_code': ['answer_shape'], 'provider_state': 'STATE_SECRET',
                     'sdk': {'headers': {'Authorization': 'HEADER_SECRET'}, 'message': 'ERROR_SECRET'},
                     'answer': 'RAW_SECRET', 'payload': 'PRIVATE_SECRET'}
        with self.store._connect() as conn:
            conn.execute('UPDATE study_records SET reason_code=? WHERE id=?',
                         (malicious['reason_code'], row['id']))
            conn.execute("UPDATE study_events SET details_json=? WHERE record_id=? AND event='submit'",
                         (json.dumps(malicious), row['id']))
        page = self.page()
        self.assertIn(DIAGNOSTIC_LABELS['request_unknown'], page)
        for value in ('REASON_SECRET', 'STATE_SECRET', 'HEADER_SECRET', 'ERROR_SECRET',
                      'RAW_SECRET', 'PRIVATE_SECRET'):
            self.assertNotIn(value, page)

    def test_completed_receipt_does_not_keep_stale_failure(self):
        for mode in ('lesson', 'qa'):
            with self.subTest(mode=mode):
                self.provider.mode = 'timeout'
                row = self.generate('complete-' + mode, mode)
                self.service._accept(row['id'], {'state': 'completed',
                    'answer': FakeProvider().reply(self.provider.calls[-1]), 'usage_state': 'known'})
                page = self.page()
                self.assertIn('data-study-state="completed"', page)
                self.assertNotIn('data-study-diagnostic', page)
                self.assertNotIn(DIAGNOSTIC_LABELS['transport_unknown'], page)
                self.assertNotIn(f'/study/{row["id"]}/reconcile', page)

    def test_one_scoped_mapping_shared_by_lesson_and_qa(self):
        self.provider.reply = lambda payload: {}
        lesson = self.generate('failed-lesson')
        qa = self.generate('failed-qa', 'qa')
        original = StudyService.diagnostics
        calls = []
        def diagnostics(service, item, version):
            calls.append((item, version))
            return original(service, item, version)
        with patch.object(StudyService, 'diagnostics', diagnostics):
            page = self.page()
        self.assertEqual(calls, [(self.item, self.version)])
        self.assertEqual(page.count(DIAGNOSTIC_LABELS['answer_shape']), 2)
        for row in (lesson, qa):
            self.assertIn(f'id="operation-{row["id"]}"', page)


if __name__ == '__main__':
    unittest.main()
