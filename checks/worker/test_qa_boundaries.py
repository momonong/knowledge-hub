"""Concrete regressions beyond the frozen HTTP acceptance; host executes these."""

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from knowledge_hub import qa
from knowledge_hub.sources import compute_content_hash
from knowledge_hub.store import KnowledgeStore


class GroundedBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'qa.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.entry = {
            'key': 'python:synthetic', 'source_name': 'python',
            'title': '合成邊界案例', 'label': '3.14', 'scope': 'full',
            'source_url': 'https://docs.python.org/3.14/whatsnew/3.14.html',
            'blocks': [{'anchor': 'p1', 'text': 'Synthetic evidence.',
                        'source_url': 'https://docs.python.org/3.14/whatsnew/3.14.html'}],
        }
        self.item_id, self.version_id = self.insert(self.entry)

    def insert(self, entry):
        entry['content_hash'] = compute_content_hash(entry['title'], entry['scope'], entry['label'], entry['blocks'])
        item_id, version_id, _ = self.store.upsert_parsed_entry(entry)
        return item_id, version_id

    def answer(self, anchor='p1', quote='Synthetic evidence.'):
        return {'claims': [{'kind': 'evidence', 'text': '合成主張',
                            'citations': [{'anchor': anchor, 'quote': quote}]}],
                'limitations': '僅供合成驗證'}

    def ask(self, provider, request_id='synthetic-request'):
        return qa.ask(self.store, provider, self.item_id, self.version_id,
                      request_id, '合成問題')

    def test_reservation_visible_during_provider_and_parallel_new_id_blocked(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        calls = []

        def provider(payload):
            calls.append(payload)
            entered.set()
            if not release.wait(10):
                raise TimeoutError('Synthetic coordination failed')
            return self.answer()

        def run():
            try:
                self.ask(provider)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            reopened = KnowledgeStore(self.db)
            record = reopened.get_item(self.item_id)['questions'][0]
            self.assertEqual(record['status'], 'unknown')
            self.assertEqual(record['operation_id'], calls[0]['operation_id'])
            with self.assertRaises(qa.QAError) as blocked:
                qa.ask(reopened, provider, self.item_id, self.version_id,
                       'parallel-request', '另一問題')
            self.assertEqual(blocked.exception.code, 409)
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get_item(self.item_id)['questions'][0]['status'], 'completed')

    def test_interruption_and_unexpected_exception_remain_unknown_after_restart(self):
        def interrupted(payload):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.ask(interrupted)
        self.store = KnowledgeStore(self.db)
        self.assertEqual(self.store.get_item(self.item_id)['questions'][0]['status'], 'unknown')
        with self.assertRaises(qa.QAError) as blocked:
            self.ask(lambda payload: self.fail('Must not dispatch'), 'new-request')
        self.assertEqual(blocked.exception.code, 409)

    def test_truncation_rejects_omitted_and_cut_off_quotes(self):
        entry = copy.deepcopy(self.entry)
        entry['blocks'] = [{'anchor': f'p{i}', 'text': 'x' * 2000 + 'OMITTED'} for i in range(41)]
        self.item_id, self.version_id = self.insert(entry)
        version = next(v for v in self.store.get_item(self.item_id)['versions'] if v['id'] == self.version_id)
        source, truncated = qa.bounded_source(version, entry['title'])
        self.assertTrue(truncated)
        self.assertEqual(len(source['blocks']), 12)
        self.assertEqual(sum(len(b['text']) for b in source['blocks']), 24000)
        for anchor, quote in [('p0', 'OMITTED'), ('p12', 'x')]:
            with self.assertRaises(ValueError):
                qa.validate_answer(self.answer(anchor, quote), source, {anchor: entry['source_url']})
        short = dict(version, blocks=[{'anchor': f'p{i}', 'text': 'x'} for i in range(41)])
        limited, clipped = qa.bounded_source(short, entry['title'])
        self.assertTrue(clipped)
        self.assertEqual(len(limited['blocks']), 40)

    def test_truncation_signal_reaches_provider_and_matches_durable_receipt(self):
        entry = copy.deepcopy(self.entry)
        entry['scope'] = 'partial'
        entry['blocks'] = [{'anchor': f'p{i}', 'text': 'Synthetic evidence.'}
                           for i in range(41)]
        self.item_id, self.version_id = self.insert(entry)
        calls = []

        def provider(payload):
            calls.append(copy.deepcopy(payload))
            # Inspect the already committed reservation, not an in-memory flag.
            with self.store._connect() as conn:
                row = conn.execute('SELECT * FROM questions WHERE operation_id=?',
                                   (payload['operation_id'],)).fetchone()
                self.assertEqual(row['status'], 'unknown')
                self.assertTrue(row['context_truncated'])
                self.assertEqual(json.loads(row['payload_json']), payload)
                self.assertEqual(json.loads(row['source_json']), payload['source'])
            return self.answer('p0')

        self.ask(provider)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]['context_truncated'], True)
        self.assertEqual(calls[0]['source']['scope'], 'partial')
        self.assertEqual(len(calls[0]['source']['blocks']), 40)
        self.assertIn('所有解說只涵蓋實際送出的段落', calls[0]['instructions'])
        self.assertIn('context_truncated', calls[0]['instructions'])
        reopened = KnowledgeStore(self.db)
        record = reopened.get_item(self.item_id)['questions'][0]
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['context_truncated'], calls[0]['context_truncated'])
        self.assertEqual(record['source'], calls[0]['source'])
        self.assertEqual(record['reading_scope'], 'partial')

    def test_unexpected_provider_error_and_oversized_version_id(self):
        def broken(payload):
            raise RuntimeError('Synthetic unexpected provider failure')

        with self.assertRaises(qa.QAError) as invalid:
            qa.ask(self.store, broken, self.item_id, '9999999999999999999',
                   'bad-version', '問題')
        self.assertEqual(invalid.exception.code, 400)
        self.assertEqual(self.store.get_item(self.item_id)['questions'], [])
        with self.assertRaises(qa.QAError) as unknown:
            self.ask(broken)
        self.assertEqual(unknown.exception.code, 503)
        self.store = KnowledgeStore(self.db)
        self.assertEqual(self.store.get_item(self.item_id)['questions'][0]['status'], 'unknown')

    def test_mutating_provider_cannot_expand_the_validated_context(self):
        def mutate(payload):
            payload['source']['blocks'][0]['text'] = 'Invented text.'
            return self.answer(quote='Invented text.')

        with self.assertRaises(qa.QAError) as invalid:
            self.ask(mutate)
        self.assertEqual(invalid.exception.code, 502)
        record = self.store.get_item(self.item_id)['questions'][0]
        self.assertEqual(record['status'], 'failed')
        self.assertIsNone(record['answer'])
        self.assertEqual(record['source']['blocks'][0]['text'], 'Synthetic evidence.')
        with self.assertRaises(qa.QAError) as replay:
            self.ask(lambda payload: self.fail('Failed request must not dispatch'))
        self.assertEqual(replay.exception.code, 409)

    def test_local_anchor_url_and_title_remain_bound_to_original_snapshot(self):
        entry = copy.deepcopy(self.entry)
        entry['title'] = 'New title'
        entry['blocks'][0]['text'] = 'New content.'
        self.insert(entry)
        self.ask(lambda payload: self.answer())
        record = self.store.get_item(self.item_id)['questions'][0]
        self.assertEqual(record['source']['title'], '合成邊界案例')
        citation = record['answer']['claims'][0]['citations'][0]
        self.assertEqual(citation['source_url'], self.entry['source_url'])
        self.assertNotIn('#p1', citation['source_url'])

    def test_empty_unavailable_and_oversized_answers(self):
        version = {'id': '1', 'scope': 'full', 'source_url': self.entry['source_url'],
                   'blocks': [{'anchor': 'p1', 'text': '   '}]}
        with self.assertRaises(ValueError):
            qa.bounded_source(version, 'Empty')
        version['scope'] = 'unavailable'
        version['blocks'][0]['text'] = 'Unavailable placeholder'
        with self.assertRaises(ValueError):
            qa.bounded_source(version, 'Unavailable')
        version['scope'] = 'full'
        version['blocks'][0]['text'] = 'Synthetic evidence.'
        source, _ = qa.bounded_source(version, 'Synthetic')
        bad_answers = []
        for change in ({'tools': []}, {'limitations': 'x' * 4001}, {'claims': []}):
            answer = self.answer()
            answer.update(change)
            bad_answers.append(answer)
        oversized = self.answer()
        oversized['claims'] *= 8
        oversized['claims'] = copy.deepcopy(oversized['claims'])
        for claim in oversized['claims']:
            claim['text'] = '字' * 4000
        bad_answers.append(oversized)
        for answer in bad_answers:
            with self.subTest(answer_keys=list(answer)):
                with self.assertRaises(ValueError):
                    qa.validate_answer(answer, source, {'p1': self.entry['source_url']})
