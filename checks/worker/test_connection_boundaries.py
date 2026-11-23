"""Synthetic edge and offline HTTP contract checks; executed by the host only."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge_hub.broker import BrokerProvider, encoded
from knowledge_hub.connections import ConnectionError, ConnectionService, INSTRUCTIONS, validate_answer
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.sources import compute_content_hash


def payload():
    return {'operation_id': 'stable-operation-1', 'instructions': INSTRUCTIONS,
            'focus': '檢索比較', 'context_truncated': True, 'limitations': '有限片段',
            'sources': [{'id': sid, 'item_id': n, 'version_id': str(n),
                'title': '公開原文', 'scope': 'partial', 'excerpts': [
                    {'anchor': 'p1', 'quote': 'Evidence retrieval. ' + '證據段落' * 700,
                     'reading_url': f'/items/{n}?version={n}#p1',
                     'source_url': f'https://example.org/{n}#p1'}]}
                for sid, n in [('S1', 1), ('S2', 2)]]}


def answer(sources):
    return {'claims': [{'kind': 'comparison', 'text': '兩份公開證據的比較。', 'citations': [
        {'source_id': s['id'], 'anchor': s['excerpts'][0]['anchor'],
         'quote': s['excerpts'][0]['quote'][:20]} for s in sources]}], 'limitations': '有限片段，解讀仍需核對。'}


class BrokerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token = Path(self.temp.name) / 'token'
        self.token.write_text('SYNTHETIC-PRIVATE-TOKEN', encoding='ascii')
        os.chmod(self.token, 0o600)
        self.provider = BrokerProvider('http://127.0.0.1:8001', self.token, 'configured-provider', 'configured-model')
        self.calls = []
        self.responses = []

    def fake_connection(self, host, port, timeout):
        self.assertEqual((host, port, timeout), ('127.0.0.1', 8001, 15))
        test = self

        class Connection:
            def request(self, method, path, body=None, headers=None):
                test.calls.append((method, path, body, headers))

            def getresponse(self):
                status, value = test.responses.pop(0)
                if isinstance(value, Exception):
                    raise value
                stream = io.BytesIO(value if isinstance(value, bytes) else encoded(value).encode())
                stream.status = status
                return stream

            def close(self):
                pass

        return Connection()

    def test_http_body_auth_bounds_and_read_only_usage_unknown_completion(self):
        original = payload()
        prepared = self.provider.prepare(original)
        self.assertEqual(original, payload())
        task = self.provider.task(prepared)
        self.assertLessEqual(len(encoded({'input': task['input'], 'options': {}}).encode()), 4096)
        for s, old in zip(prepared['sources'], original['sources']):
            self.assertIn(s['excerpts'][0]['quote'], old['excerpts'][0]['quote'])
        result = answer(prepared['sources'])
        self.responses = [(202, {'state': 'pending'}), (200, {'state': 'completed_usage_unknown'}),
                          (200, {'state': 'completed_usage_unknown', 'result': {'text': encoded(result)},
                                 'finish_reason': 'stop', 'response_truncated': False})]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            self.assertEqual(self.provider.submit(prepared), {'state': 'pending'})
            done = self.provider.poll(prepared['operation_id'])
        self.assertEqual(done['state'], 'completed')
        self.assertEqual(done['usage_state'], 'unknown')
        validate_answer(done['answer'], prepared['sources'])
        self.assertEqual([(c[0], c[1]) for c in self.calls], [
            ('POST', '/quota/api/v1/jobs'), ('GET', '/quota/api/v1/jobs/stable-operation-1'),
            ('GET', '/quota/api/v1/jobs/stable-operation-1/result')])
        body = json.loads(self.calls[0][2])
        self.assertEqual(body['request_key'], prepared['operation_id'])
        self.assertEqual(body['capability'], 'text_generation')
        self.assertEqual(body['max_attempts'], 1)
        self.assertEqual(set(body), {'request_key', 'capability', 'provider', 'model', 'input',
                                     'max_output_tokens', 'max_attempts'})
        self.assertEqual(body['max_output_tokens'], 512)
        self.assertNotIn('options', body)
        self.assertEqual(body, {
            'request_key': 'stable-operation-1', 'capability': 'text_generation',
            'provider': 'configured-provider', 'model': 'configured-model',
            'input': {'messages': [{'role': 'user', 'content': self.provider.prompt(prepared)}]},
            'max_output_tokens': 512, 'max_attempts': 1})
        self.assertLessEqual(len(encoded({'input': body['input'], 'options': {}}).encode('utf-8')), 4096)
        self.assertEqual(body['provider'], 'configured-provider')
        self.assertEqual(body['model'], 'configured-model')
        self.assertEqual(body['input']['messages'][0]['content'], self.provider.prompt(prepared))
        self.assertEqual(self.calls[0][3]['Authorization'], 'Bearer SYNTHETIC-PRIVATE-TOKEN')
        self.assertNotIn('SYNTHETIC-PRIVATE-TOKEN', encoded(prepared))

    def test_redirect_missing_job_and_transport_do_not_retry_or_resubmit(self):
        prepared = self.provider.prepare(payload())
        self.responses = [(307, {}), (404, {})]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            self.assertEqual(self.provider.submit(prepared)['state'], 'unknown')
            self.assertEqual(self.provider.poll(prepared['operation_id'])['state'], 'unknown')
        self.assertEqual(len(self.calls), 2)
        self.responses = [(200, TimeoutError('SYNTHETIC-PRIVATE-TOKEN'))]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            result = self.provider.poll(prepared['operation_id'])
        self.assertEqual(result, {'state': 'unknown', 'reason_code': 'transport_unknown'})
        self.assertNotIn('SYNTHETIC-PRIVATE-TOKEN', encoded(result))
        self.assertEqual(len(self.calls), 3)

    def test_invalid_truncated_and_oversized_result_never_complete(self):
        prepared = self.provider.prepare(payload())
        for result in ({'text': '{"claims":'}, {'text': '{}', 'truncated': True},
                       {'text': '{}', 'finish_reason': 'length'}, {'answer': {}}):
            self.responses = [(200, {'state': 'completed'}), (200, {'result': result})]
            with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
                self.assertEqual(self.provider.poll(prepared['operation_id'])['state'], 'failed')
        self.responses = [(200, b'x' * 131073)]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            self.assertEqual(self.provider.poll(prepared['operation_id']),
                             {'state': 'unknown', 'reason_code': 'transport_unknown'})

    def test_explicit_unknown_is_provider_diagnostic_without_result_or_resubmit(self):
        prepared = self.provider.prepare(payload())
        original = copy.deepcopy(prepared)
        self.responses = [(202, {'state': 'queued'}), (200, {'state': 'running'}),
                          (200, {'state': 'unknown'})]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            self.assertEqual(self.provider.submit(prepared), {'state': 'pending'})
            self.assertEqual(self.provider.poll(prepared['operation_id']), {'state': 'pending'})
            unknown = self.provider.poll(prepared['operation_id'])
        self.assertEqual(unknown, {'state': 'unknown', 'reason_code': 'provider_unknown', 'http_status': 200})
        self.assertNotEqual(unknown['reason_code'], 'protocol_unknown')
        self.assertNotIn('answer', unknown)
        self.assertNotIn('usage_state', unknown)
        self.assertEqual(prepared, original)
        self.assertEqual([(c[0], c[1]) for c in self.calls], [
            ('POST', '/quota/api/v1/jobs'),
            ('GET', '/quota/api/v1/jobs/stable-operation-1'),
            ('GET', '/quota/api/v1/jobs/stable-operation-1')])
        self.responses = [(200, {'state': 'novel-unrecognized-state'})]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            novel = self.provider.poll(prepared['operation_id'])
        self.assertEqual(novel, {'state': 'unknown', 'reason_code': 'protocol_unknown', 'http_status': 200})
        self.assertEqual(sum(c[0] == 'POST' for c in self.calls), 1)
        self.assertFalse(any(c[1].endswith('/result') for c in self.calls))

    def test_actual_outcome_truncation_usage_and_waiting_queue(self):
        prepared = self.provider.prepare(payload())
        complete_text = encoded(answer(prepared['sources']))
        for outcome in (
            {'state': 'completed', 'result': {'text': complete_text}, 'response_truncated': True},
            {'state': 'completed', 'result': {'text': complete_text}, 'finish_reason': 'length'},
            {'state': 'completed', 'result': {'text': complete_text, 'response_truncated': True}},
            {'state': 'completed', 'result': {'text': complete_text, 'finish_reason': 'max_tokens'}},
        ):
            self.responses = [(200, {'state': 'completed'}), (200, outcome)]
            with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
                result = self.provider.poll(prepared['operation_id'])
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['reason_code'], 'output_truncated')
        self.responses = [(200, {'state': 'completed'}), (200, {
            'state': 'completed_usage_unknown', 'result': {'text': complete_text},
            'finish_reason': 'stop', 'response_truncated': False})]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            result = self.provider.poll(prepared['operation_id'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['usage_state'], 'unknown')
        self.responses = [(200, {'state': 'completed'}), (200, {'result': {'text': complete_text}})]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            self.assertEqual(self.provider.poll(prepared['operation_id'])['usage_state'], 'unknown')
        previous_calls = len(self.calls)
        self.responses = [(200, {'state': 'waiting'})]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            self.assertEqual(self.provider.poll(prepared['operation_id']), {'state': 'pending'})
        self.assertEqual(len(self.calls), previous_calls + 1)
        self.assertTrue(all(call[0] == 'GET' for call in self.calls))

    def test_rejection_json_and_transport_use_safe_reason_codes(self):
        prepared = self.provider.prepare(payload())
        for status, value, state, reason in (
            (403, {'message': 'SYNTHETIC-PRIVATE-TOKEN'}, 'failed', 'http_rejected'),
            (200, b'not json SYNTHETIC-PRIVATE-TOKEN', 'unknown', 'invalid_json'),
            (503, {'error': 'SYNTHETIC-PRIVATE-TOKEN'}, 'unknown', 'http_unknown'),
        ):
            self.responses = [(status, value)]
            with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
                result = self.provider.submit(prepared)
            self.assertEqual(result, {'state': state, 'reason_code': reason, 'http_status': status})
            self.assertNotIn('SYNTHETIC-PRIVATE-TOKEN', encoded(result))
        self.responses = [(200, {'state': 'completed'}), (200, {'result': {'text': '{invalid'}})]
        with patch('knowledge_hub.broker.http.client.HTTPConnection', self.fake_connection):
            result = self.provider.poll(prepared['operation_id'])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['reason_code'], 'invalid_json')

    def test_changed_adapter_binding_rejects_submit_before_http(self):
        prepared = self.provider.prepare(payload())
        self.assertEqual(prepared['provider_binding'], {
            'base_url': 'http://127.0.0.1:8001', 'provider': 'configured-provider',
            'model': 'configured-model', 'max_output_tokens': 512})
        self.provider.port = 8002
        self.provider.base_url = 'http://127.0.0.1:8002'
        with patch('knowledge_hub.broker.http.client.HTTPConnection', side_effect=AssertionError('Must not send')):
            with self.assertRaises(ValueError):
                self.provider.submit(prepared)
        with self.assertRaises(ValueError):
            BrokerProvider('http://127.0.0.1:8001', self.token, 'p', 'm', max_output_tokens=513)

    def test_preflight_rejects_before_http_and_private_file_and_url_validation(self):
        too_large = payload()
        too_large['focus'] = '字' * 5000
        with self.assertRaises(ValueError):
            self.provider.prepare(too_large)
        with patch('knowledge_hub.broker.http.client.HTTPConnection', side_effect=AssertionError('Must not send')):
            with self.assertRaises(ValueError):
                self.provider.submit(payload())
        for url in ('https://127.0.0.1:8001', 'http://localhost:8001', 'http://127.0.0.1:8001/',
                    'http://127.0.0.1:8001?x=1', 'http://a@127.0.0.1:8001', 'http://127.0.0.1:65536'):
            with self.assertRaises(ValueError):
                BrokerProvider(url, self.token, 'p', 'm')
        os.chmod(self.token, 0o644)
        with self.assertRaises(ValueError):
            BrokerProvider('http://127.0.0.1:8001', self.token, 'p', 'm')
        os.chmod(self.token, 0o600)
        link = self.token.with_name('link')
        link.symlink_to(self.token)
        with self.assertRaises(ValueError):
            BrokerProvider('http://127.0.0.1:8001', link, 'p', 'm')


class ConnectionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'hub.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.ids = []
        for n in (1, 2):
            entry = {'key': f'daily:{n}', 'source_name': 'manual', 'title': 'Retrieval evidence',
                     'label': 'v1', 'scope': 'partial', 'source_url': f'https://example.org/{n}',
                     'blocks': [{'anchor': 'p1', 'text': f'Retrieval evidence source {n}.',
                                 'source_url': f'https://example.org/{n}#p1'}]}
            entry['content_hash'] = compute_content_hash(entry['title'], entry['scope'], entry['label'], entry['blocks'])
            item, version, _ = self.store.upsert_parsed_entry(entry)
            self.ids.append((item, version))
        self.store.add_note(*self.ids[1], 'p1', 'PRIVATE-NOTE-EXCLUDED')

    def test_restart_binding_changes_block_before_any_poll_or_submit(self):
        original = {'base_url': 'http://127.0.0.1:8001', 'provider': 'p', 'model': 'm',
                    'max_output_tokens': 512}

        class BoundProvider:
            def __init__(self, binding, state):
                self.config = copy.deepcopy(binding)
                self.state = state
                self.submitted, self.polled = [], []

            def binding(self):
                return copy.deepcopy(self.config)

            def submit(self, value):
                self.submitted.append(copy.deepcopy(value))
                return {'state': self.state}

            def poll(self, operation_id):
                self.polled.append(operation_id)
                return {'state': self.state}

        for identity, state in zip(self.ids, ('unknown', 'pending')):
            provider = BoundProvider(original, state)
            row = ConnectionService(self.store, provider).generate(*identity, 'bound-' + state)
            self.assertEqual(row['payload']['provider_binding'], original)
            self.assertEqual(set(row['payload']['provider_binding']),
                             {'base_url', 'provider', 'model', 'max_output_tokens'})
            before = ConnectionService(self.store).records(identity[0])
            for field, value in (('base_url', 'http://127.0.0.1:8002'), ('provider', 'other'),
                                 ('model', 'new-model'), ('max_output_tokens', 256)):
                config = dict(original, **{field: value})
                changed = BoundProvider(config, state)
                reopened = ConnectionService(KnowledgeStore(self.db), changed)
                with self.assertRaises(ConnectionError) as error:
                    reopened.reconcile(row['id'])
                self.assertEqual(error.exception.code, 409)
                with self.assertRaises(ConnectionError):
                    reopened.generate(*identity, 'new-' + field + '-' + state)
                self.assertEqual(changed.polled, [])
                self.assertEqual(changed.submitted, [])
                self.assertEqual(reopened.records(identity[0]), before)
            unbound = BoundProvider(None, state)
            reopened = ConnectionService(KnowledgeStore(self.db), unbound)
            with self.assertRaises(ConnectionError):
                reopened.reconcile(row['id'])
            self.assertEqual(unbound.polled, [])
            self.assertEqual(unbound.submitted, [])
            self.assertEqual(reopened.records(identity[0]), before)
            restored = BoundProvider(original, state)
            ConnectionService(KnowledgeStore(self.db), restored).reconcile(row['id'])
            self.assertEqual(restored.polled, [row['operation_id']])
            self.assertEqual(restored.submitted, [])
            self.assertEqual(len(provider.submitted), 1)

    def test_changed_binding_during_preparation_prevents_reservation_and_submit(self):
        class ChangingProvider:
            def __init__(self):
                self.port = 8001
                self.calls = 0

            def binding(self):
                return {'base_url': f'http://127.0.0.1:{self.port}', 'provider': 'p',
                        'model': 'm', 'max_output_tokens': 512}

            def prepare(self, value):
                self.port = 8002
                return value

            def submit(self, value):
                self.calls += 1
                raise AssertionError('Binding mismatch must prevent dispatch')

        provider = ChangingProvider()
        service = ConnectionService(self.store, provider)
        with self.assertRaises(ConnectionError):
            service.generate(*self.ids[0], 'changed-before-submit')
        self.assertEqual(provider.calls, 0)
        self.assertEqual(service.records(self.ids[0][0]), [])

    def test_diagnostics_are_whitelisted_and_invalid_citations_are_specific(self):
        class Provider:
            response = {'state': 'failed', 'reason_code': 'SYNTHETIC-SECRET',
                        'http_status': 'SYNTHETIC-SECRET', 'message': 'SYNTHETIC-SECRET'}

            def submit(self, value):
                return self.response

        provider = Provider()
        service = ConnectionService(self.store, provider)
        row = service.generate(*self.ids[0], 'unsafe-diagnostic')
        self.assertEqual(row['reason_code'], 'provider_failed')
        self.assertIsNone(row['http_status'])
        self.assertNotIn('SYNTHETIC-SECRET', encoded(row))
        provider.response = {'state': 'failed', 'reason_code': 'http_rejected', 'http_status': 403,
                             'error': 'SYNTHETIC-SECRET'}
        row = service.generate(*self.ids[0], 'known-rejection')
        self.assertEqual(row['reason_code'], 'http_rejected')
        self.assertEqual(row['http_status'], 403)
        self.assertNotIn('SYNTHETIC-SECRET', encoded(row))

        class InvalidCitationProvider:
            def submit(self, value):
                result = answer(value['sources'])
                result['claims'][0]['citations'][0]['quote'] = 'SYNTHETIC-SECRET-FABRICATION'
                return {'state': 'completed', 'answer': result}

        row = ConnectionService(self.store, InvalidCitationProvider()).generate(*self.ids[0], 'bad-citation')
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['reason_code'], 'invalid_citations')
        self.assertIsNone(row['answer'])
        self.assertNotIn('SYNTHETIC-SECRET', encoded(row))
        with self.store._connect() as conn:
            raw = conn.execute('SELECT * FROM connection_records WHERE id=?', (row['id'],)).fetchone()
            self.assertNotIn('SYNTHETIC-SECRET', encoded(dict(raw)))

    def test_legacy_record_migration_preserves_unresolved_snapshot(self):
        class Provider:
            def submit(self, value):
                return {'state': 'pending'}

        row = ConnectionService(self.store, Provider()).generate(*self.ids[0], 'legacy-record')
        with self.store._connect() as conn:
            conn.execute('ALTER TABLE connection_records DROP COLUMN reason_code')
            conn.execute('ALTER TABLE connection_records DROP COLUMN http_status')
        reopened = ConnectionService(KnowledgeStore(self.db), Provider())
        migrated = reopened.records(self.ids[0][0])[0]
        self.assertEqual(migrated['payload'], row['payload'])
        self.assertEqual(migrated['operation_id'], row['operation_id'])
        self.assertEqual(migrated['state'], 'pending')
        self.assertIsNone(migrated['reason_code'])
        self.assertIsNone(migrated['http_status'])
        with self.assertRaises(ConnectionError):
            reopened.generate(*self.ids[0], 'cannot-bypass-legacy')

    def test_numeric_evidence_versions_keep_notes_replay_and_restart_links(self):
        test = self

        class Provider:
            def submit(self, value):
                for source in value['sources']:
                    test.assertIs(type(source['version_id']), int)
                return {'state': 'completed', 'answer': answer(value['sources'])}

        service = ConnectionService(self.store, Provider())
        context = service.build_context(*self.ids[0])
        self.assertIs(type(context['focal']['version_id']), int)
        self.assertEqual(context['focal']['version_id'], int(self.ids[0][1]))
        self.assertEqual(context['related'][0]['version_id'], int(self.ids[1][1]))
        self.assertIs(type(context['related'][0]['version_id']), int)
        self.assertEqual(context['notes'][0]['text'], 'PRIVATE-NOTE-EXCLUDED')
        row = service.generate(*self.ids[0], 'numeric-versions')
        self.assertEqual(row['state'], 'completed')
        expected = {int(version) for _, version in self.ids}
        citations = row['answer']['claims'][0]['citations']
        self.assertEqual({c['version_id'] for c in citations}, expected)
        for cite in citations:
            self.assertIs(type(cite['version_id']), int)
            self.assertIn(f"version={cite['version_id']}#p1", cite['reading_url'])
        reopened = ConnectionService(KnowledgeStore(self.db))
        self.assertEqual(reopened.records(self.ids[0][0]), [row])
        self.assertEqual(reopened.generate(self.ids[0][0], int(self.ids[0][1]),
                                          'numeric-versions'), row)

    def test_durable_snapshot_mutation_and_interruption(self):
        test = self

        class Provider:
            def submit(self, value):
                reopened = ConnectionService(KnowledgeStore(test.db), self)
                rows = reopened.records(test.ids[0][0])
                test.assertEqual(rows[0]['state'], 'unknown')
                test.assertEqual(rows[0]['payload'], value)
                test.assertNotIn('PRIVATE-NOTE-EXCLUDED', encoded(value))
                with test.assertRaises(ConnectionError):
                    reopened.generate(*test.ids[0], 'another-request')
                value['sources'][0]['excerpts'][0]['quote'] = 'Invented source quote.'
                return {'state': 'completed', 'answer': answer(value['sources'])}

        service = ConnectionService(self.store, Provider())
        row = service.generate(*self.ids[0], 'request-1')
        self.assertEqual(row['state'], 'failed')
        self.assertIsNone(row['answer'])
        self.assertNotIn('Invented source quote.', encoded(row))

    def test_strict_citations_require_both_sources_and_no_model_urls(self):
        sources = payload()['sources']
        reply = answer(sources)
        reply['claims'][0]['citations'][0]['source_url'] = 'https://model.example'
        with self.assertRaises(ValueError):
            validate_answer(reply, sources)
        reply = answer(sources)
        reply['claims'][0]['citations'].pop()
        with self.assertRaises(ValueError):
            validate_answer(reply, sources)
        reply = answer(sources)
        reply['claims'][0]['kind'] = 'background'
        with self.assertRaises(ValueError):
            validate_answer(reply, sources)

    def test_stopwords_alone_do_not_create_connections(self):
        with self.store._connect() as c:
            c.execute("UPDATE items SET title='the and with'")
            c.execute("UPDATE version_titles SET title='the and with'")
            c.execute('UPDATE item_versions SET blocks_json=?',
                      (json.dumps([{'anchor': 'p1', 'text': 'the and with are from'}]),))
        service = ConnectionService(self.store)
        self.assertEqual(service.build_context(*self.ids[0])['related'], [])


if __name__ == '__main__':
    unittest.main()
