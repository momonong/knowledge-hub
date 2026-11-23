"""Synthetic engineering coverage; not a teaching-quality evaluation."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_study import entry, FakeProvider
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.study import StudyService
from knowledge_hub.study_views import render


class DiagnosticsTests(unittest.TestCase):
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
        self.store.add_note(self.item, self.version, 'p1', 'PRIVATE_NOTE_SENTINEL')
        self.provider = FakeProvider()
        self.service = StudyService(self.store, self.provider)

    def generate(self, request='diagnostic'):
        return self.service.generate(self.item, self.version, request, 'lesson', '理解機制')

    def page(self):
        with patch.object(self.provider, 'binding', side_effect=AssertionError('binding on render')), \
             patch.object(self.provider, 'prepare', side_effect=AssertionError('prepare on render')), \
             patch.object(self.provider, 'submit', side_effect=AssertionError('submit on render')), \
             patch.object(self.provider, 'poll', side_effect=AssertionError('poll on render')):
            return render(self.service, self.item, self.version, 'csrf', lambda url: url)

    def test_shape_failure_has_safe_actionable_explanation_and_survives_restart(self):
        self.provider.reply = lambda payload: {'rejected': 'RAW_MODEL_SENTINEL'}
        row = self.generate()
        self.assertEqual(row['state'], 'failed')
        before = self.service.records(self.item)
        events = self.service.events(row['id'])
        diagnostic = self.service.diagnostics(self.item, self.version)[row['id']]
        self.assertEqual(diagnostic['code'], 'answer_shape')
        self.assertEqual(diagnostic['provider_state'], 'completed')
        self.assertIn('未回傳有效的答案結構', self.page())
        self.service = StudyService(KnowledgeStore(self.path), self.provider)
        self.assertEqual(self.service.diagnostics(self.item, self.version)[row['id']], diagnostic)
        self.assertEqual(self.service.records(self.item), before)
        self.assertEqual(self.service.events(row['id']), events)
        for sentinel in ('RAW_MODEL_SENTINEL', 'PRIVATE_NOTE_SENTINEL'):
            self.assertNotIn(sentinel, json.dumps(diagnostic))
            self.assertNotIn(sentinel, self.page())
        self.assertEqual(len(self.provider.calls), 1)

    def test_comparison_requires_focal_and_background(self):
        original = self.provider.reply
        def missing(payload):
            result = original(payload)
            self.assertGreater(len(payload['sources']), 1)
            result['sections'][3]['citations'] = result['sections'][3]['citations'][:1]
            return result
        self.provider.reply = missing
        row = self.generate()
        self.assertEqual(self.service.diagnostics(self.item, self.version)[row['id']]['code'], 'comparison_sources')
        self.assertIn('比較段未同時引用本篇與背景原文', self.page())
        self.assertIsNone(row['answer'])

    def test_unknown_and_provider_http_failure_are_distinct(self):
        self.provider.mode = 'timeout'
        row = self.generate()
        self.assertIn('傳輸結果未知', self.page())
        self.assertIn('不會重新提交', self.page())
        # Synthetic receipt transition only: no retry, polling or provider call.
        self.service._accept(row['id'], {'state': 'failed', 'reason_code': 'provider_failed',
            'trace': {'sdk': {'http_status': 503, 'error_code': 'provider_http_error'}}})
        diagnostic = self.service.diagnostics(self.item, self.version)[row['id']]
        self.assertEqual(diagnostic['code'], 'provider_http_failed')
        self.assertIn('提供者 HTTP 請求失敗', self.page())
        self.assertNotIn('傳輸結果未知', self.page())
        self.assertEqual(len(self.provider.calls), 1)

    def test_untrusted_metadata_and_other_records_are_not_projected(self):
        self.provider.mode = 'timeout'
        row = self.generate()
        malicious = {'state': 'unknown', 'reason_code': '<script>SECRET</script>',
            'validation_code': ['answer_shape'], 'provider_state': '<script>SECRET</script>',
            'sdk': {'http_status': True, 'message': 'EXCEPTION_SENTINEL',
                    'headers': {'Authorization': 'TOKEN_SENTINEL'}},
            'payload': 'PAYLOAD_SENTINEL', 'answer': 'RAW_SENTINEL'}
        with self.store._connect() as c:
            c.execute('UPDATE study_records SET reason_code=? WHERE id=?', ('<script>SECRET</script>', row['id']))
            c.execute('UPDATE study_events SET details_json=? WHERE record_id=? AND event=\'submit\'',
                      (json.dumps(malicious), row['id']))
        diagnostics = self.service.diagnostics(self.item, self.version)
        self.assertEqual(diagnostics[row['id']]['code'], 'request_unknown')
        self.assertEqual(self.service.diagnostics(self.other, self.other_version), {})
        text = json.dumps(diagnostics) + self.page()
        for sentinel in ('SECRET', 'EXCEPTION_SENTINEL', 'TOKEN_SENTINEL', 'PAYLOAD_SENTINEL',
                         'RAW_SENTINEL', 'PRIVATE_NOTE_SENTINEL'):
            self.assertNotIn(sentinel, text)

    def test_no_event_malformed_and_oversized_events_render_safely(self):
        self.provider.reply = lambda payload: {}
        row = self.generate()
        with self.store._connect() as c:
            c.execute('DELETE FROM study_events WHERE record_id=?', (row['id'],))
        for details in (None, '{invalid', '[]', json.dumps({'junk': 'x' * 20000})):
            with self.subTest(details=str(details)[:20]):
                if details is not None:
                    with self.store._connect() as c:
                        c.execute("INSERT INTO study_events(record_id,event,created_at,details_json) VALUES (?,'poll','now',?)",
                                  (row['id'], details))
                self.assertEqual(self.service.diagnostics(self.item, self.version)[row['id']]['code'], 'invalid_citations')
                self.assertIn('生成或引用檢查失敗', self.page())

    def test_latest_completion_does_not_show_previous_unknown_diagnostics(self):
        self.provider.mode = 'timeout'
        row = self.generate()
        self.service._accept(row['id'], {'state': 'completed',
            'answer': FakeProvider().reply(self.provider.calls[-1]), 'usage_state': 'known'})
        self.assertEqual(self.service.diagnostics(self.item, self.version), {})
        page = self.page()
        self.assertNotIn('傳輸結果未知', page)
        self.assertIn('核對本段證據', page)
        self.assertIn('/progress', page)
        self.assertIn('parent_id', page)
        saved = self.service.save_progress(self.item, self.version, 'save', row['id'],
                                          'PRIVATE_UNDERSTANDING', 'PRIVATE_QUESTION')
        self.assertEqual(self.service.progress(self.item, self.version)[0], saved)
        self.assertNotIn('PRIVATE_', json.dumps(self.service.diagnostics(self.item, self.version)))

    def test_latest_event_only_and_receipt_state_must_match(self):
        self.provider.mode = 'timeout'
        row = self.generate()
        for state in ('unknown', 'failed'):
            with self.subTest(state=state):
                with self.store._connect() as c:
                    details = {'state': state, 'reason_code': 'transport_unknown',
                               'provider_state': 'completed', 'validation_code': 'answer_shape'}
                    c.execute("INSERT INTO study_events(record_id,event,created_at,details_json) VALUES (?,'poll','now',?)",
                              (row['id'], json.dumps(details)))
                self.assertEqual(self.service.diagnostics(self.item, self.version)[row['id']]['code'], 'transport_unknown')
                self.assertNotIn('未回傳有效的答案結構', self.page())

    def test_long_history_uses_one_read_only_projection_query_and_is_scoped(self):
        self.provider.reply = lambda payload: {}
        row = self.generate()
        # Seed historical receipts directly: never exceed generation budgets
        # or submit additional synthetic or real provider requests.
        with self.store._connect() as c:
            for i in range(205):
                cur = c.execute('''INSERT INTO study_records
                    (item_id,version_id,request_id,operation_id,mode,prompt,payload_json,state,reason_code,created_at,updated_at)
                    SELECT item_id,version_id,?,? ,mode,prompt,payload_json,state,reason_code,created_at,updated_at
                    FROM study_records WHERE id=?''', ('history-' + str(i), 'operation-' + str(i), row['id']))
                c.execute("INSERT INTO study_events(record_id,event,created_at,details_json) VALUES (?,'submit','now',?)",
                          (cur.lastrowid, json.dumps({'state': 'failed', 'reason_code': 'invalid_citations',
                              'provider_state': 'completed', 'validation_code': 'answer_shape'})))
            # A different item's event must not appear in this item's map.
            cur = c.execute('''INSERT INTO study_records
                (item_id,version_id,request_id,operation_id,mode,prompt,payload_json,state,reason_code,created_at,updated_at)
                SELECT ?,?,'other','other-operation',mode,prompt,payload_json,state,reason_code,created_at,updated_at
                FROM study_records WHERE id=?''', (self.other, self.other_version, row['id']))
            other_id = cur.lastrowid
            c.execute("INSERT INTO study_events(record_id,event,created_at,details_json) VALUES (?,'submit','now',?)",
                      (other_id, json.dumps({'state': 'failed', 'reason_code': 'invalid_citations',
                          'provider_state': 'completed', 'validation_code': 'comparison_sources',
                          'message': 'OTHER_RECORD_SENTINEL'})))
        statements = []
        connect = self.store._connect
        @contextmanager
        def traced():
            with connect() as c:
                c.set_trace_callback(statements.append)
                yield c
        with patch.object(self.store, '_connect', traced):
            diagnostics = self.service.diagnostics(self.item, self.version)
        self.assertEqual(len(diagnostics), 206)
        self.assertNotIn(other_id, diagnostics)
        self.assertTrue(all(d['code'] == 'answer_shape' for d in diagnostics.values()))
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].lstrip().upper().startswith('SELECT'))
        self.assertNotIn('OTHER_RECORD_SENTINEL', self.page())
        self.assertEqual(len(self.provider.calls), 1)


if __name__ == '__main__':
    unittest.main()
