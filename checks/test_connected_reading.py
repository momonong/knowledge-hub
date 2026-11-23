"""Host acceptance for the approved second phase; synthetic sources only."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

from knowledge_hub.library import KnowledgeLibrary
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from test_initial_library import seed
from test_reading_loop import Forms


class Provider:
    def __init__(self, mode='complete'):
        self.mode = mode
        self.sent = []
        self.polled = []

    def answer(self):
        sources = self.sent[-1]['sources']
        cites = [{'source_id': s['id'], 'anchor': s['excerpts'][0]['anchor'],
                  'quote': s['excerpts'][0]['quote'][:60]} for s in sources]
        if self.mode == 'invalid':
            cites[1]['quote'] = 'A fabricated quote that was never supplied.'
        return {'claims': [{'kind': 'comparison', 'text': '兩來源比較：星圖接續 <script>bad()</script>',
                            'citations': cites}], 'limitations': '僅根據提供片段，仍需核對完整論文。'}

    def submit(self, payload):
        self.sent.append(copy.deepcopy(payload))
        if self.mode == 'timeout':
            raise TimeoutError('PRIVATE-CREDENTIAL-MUST-NOT-LEAK')
        if self.mode == 'pending':
            return {'state': 'pending'}
        return {'state': 'completed', 'answer': self.answer()}

    def poll(self, operation_id):
        self.polled.append(operation_id)
        return {'state': 'completed', 'answer': self.answer()}


class ConnectedReadingAcceptance(unittest.TestCase):
    def setUp(self):
        from knowledge_hub.connections import ConnectionService, ConnectionError
        self.Service, self.Error = ConnectionService, ConnectionError
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'knowledge.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.lib = KnowledgeLibrary(self.store)
        c, g, s = seed()
        c['sources'].append(dict(c['sources'][0], id='metadata-only', title='Retrieval Survey',
                                primary_url='https://example.org/survey'))
        g['concepts'][0]['supporting_source_ids'].append('metadata-only')
        self.lib.import_seed(c, g, s)
        self.background = self.doc('sample-rag', 'Retrieval grounding uses evidence passages. '
                                   'The retrieval unit is a document passage.', 'rag-page-2')
        self.focal = self.doc('sample-agent', 'Agent memory retains earlier observations. '
                              'Retrieval grounding helps reconnect relevant observations.', 'agent-page-7')
        self.store.add_note(self.background['item_id'], self.background['version_id'],
                            'rag-page-2', 'PRIVATE-LOCAL-NOTE-STAR')
        self.provider = Provider()
        self.service = self.Service(self.store, self.provider)

    def doc(self, sid, text, anchor):
        return self.lib.add_document(sid, text.encode(), media_type='text/plain',
            source_url='https://example.org/' + sid, version_label='v1',
            blocks=[{'anchor': anchor, 'text': text, 'source_url': 'https://example.org/' + sid + '#' + anchor}],
            scope='partial', extraction_note='synthetic extraction', rights_note='synthetic')

    def context(self, **kw):
        return self.service.build_context(self.focal['item_id'], self.focal['version_id'], **kw)

    def generate(self, rid='connected-1', **kw):
        return self.service.generate(self.focal['item_id'], self.focal['version_id'], rid, **kw)

    def test_background_originals_notes_and_metadata_are_distinguished(self):
        ctx = self.context()
        self.assertEqual(ctx['focal']['version_id'], self.focal['version_id'])
        self.assertEqual(ctx['focal']['scope'], 'partial')
        self.assertEqual(ctx['related'][0]['source_id'], 'sample-rag')
        background = ctx['related'][0]
        self.assertEqual(background['relation_kind'], 'background')
        self.assertTrue(background['reason'])
        self.assertTrue(background['excerpts'])
        self.assertEqual(background['excerpts'][0]['anchor'], 'rag-page-2')
        metadata = next(r for r in ctx['related'] if r.get('source_id') == 'metadata-only')
        self.assertFalse(metadata['excerpts'])
        self.assertFalse(metadata.get('version_id'))
        self.assertIn('PRIVATE-LOCAL-NOTE-STAR', json.dumps(ctx['notes']))
        self.assertTrue(ctx['limitations'])
        self.assertFalse(any(r.get('item_id') == self.focal['item_id'] for r in ctx['related']))

    def test_new_daily_item_uses_lexical_retrieval_and_late_page_windows(self):
        late = ('Unrelated filler. ' * 450) + 'RareNeedle retrieval grounding reconnection evidence.'
        old = self.doc('sample-rag', late, 'late-page-99')
        entry = dict(key='daily:outside-library', source_name='manual', title='Retrieval grounding notes',
                     label='v1', scope='partial', source_url='https://example.org/daily',
                     content_hash=hashlib.sha256(b'new-daily').hexdigest(),
                     blocks=[{'anchor':'daily-1','text':'Retrieval grounding connects old knowledge.',
                              'source_url':'https://example.org/daily#daily-1'}])
        item, version, _ = self.store.upsert_parsed_entry(entry)
        ctx = self.service.build_context(item, version, focus='RareNeedle')
        related = next(r for r in ctx['related'] if r.get('source_id') == 'sample-rag')
        self.assertEqual(related['relation_kind'], 'lexical')
        self.assertEqual(related['version_id'], old['version_id'])
        self.assertIn('RareNeedle', json.dumps(related['excerpts']))
        for excerpt in related['excerpts']:
            self.assertLessEqual(len(excerpt['quote']), 700)
            self.assertIn(excerpt['quote'], late)

    def test_completed_synthesis_is_grounded_private_and_idempotent(self):
        row = self.generate()
        self.assertEqual(row['state'], 'completed')
        payload = self.provider.sent[0]
        self.assertEqual(len(payload['sources']), 2)
        self.assertEqual([s['id'] for s in payload['sources']], ['S1', 'S2'])
        self.assertNotIn('PRIVATE-LOCAL-NOTE-STAR', json.dumps(payload))
        self.assertEqual(payload['operation_id'], row['operation_id'])
        citations = row['answer']['claims'][0]['citations']
        self.assertEqual({c['version_id'] for c in citations},
                         {self.focal['version_id'], self.background['version_id']})
        for c in citations:
            self.assertIn('https://example.org/', c['source_url'])
            self.assertIn('version=', c['reading_url'])
        repeated = self.generate()
        self.assertEqual(row, repeated)
        self.assertEqual(len(self.provider.sent), 1)
        with self.assertRaises(self.Error): self.generate(focus='changed')
        newservice = self.Service(KnowledgeStore(self.db), self.provider)
        self.assertEqual(newservice.records(self.focal['item_id']), [row])

    def test_invalid_quotes_never_become_a_completed_explanation(self):
        self.provider.mode = 'invalid'
        try:
            row = self.generate()
        except self.Error:
            row = self.service.records(self.focal['item_id'])[0]
        self.assertEqual(row['state'], 'failed')
        self.assertFalse(row.get('answer'))

    def test_unknown_is_durable_before_dispatch_and_new_id_cannot_replay(self):
        def submit(payload):
            observed = self.Service(KnowledgeStore(self.db)).records(self.focal['item_id'])
            self.assertEqual(observed[0]['operation_id'], payload['operation_id'])
            self.assertEqual(observed[0]['state'], 'unknown')
            self.provider.sent.append(payload)
            raise TimeoutError('PRIVATE-CREDENTIAL-MUST-NOT-LEAK')
        self.provider.submit = submit
        try: self.generate()
        except self.Error: pass
        record = self.service.records(self.focal['item_id'])[0]
        self.assertEqual(record['state'], 'unknown')
        self.assertNotIn('PRIVATE-CREDENTIAL', json.dumps(record))
        for rid in ['connected-1', 'connected-2']:
            with self.assertRaises(self.Error): self.generate(rid)
        reopened = self.Service(KnowledgeStore(self.db), self.provider)
        with self.assertRaises(self.Error):
            reopened.generate(self.focal['item_id'], self.focal['version_id'], 'after-restart')
        self.assertEqual(len(self.provider.sent), 1)
        self.assertEqual(reopened.reconcile(record['id'])['state'], 'completed')
        self.assertEqual(self.provider.polled, [record['operation_id']])
        self.assertEqual(len(self.provider.sent), 1)

    def test_pending_polls_original_operation_and_version_snapshot_survives(self):
        self.provider.mode = 'pending'
        pending = self.generate()
        self.assertEqual(pending['state'], 'pending')
        with self.assertRaises(self.Error): self.generate('new-pending-id')
        self.doc('sample-rag', 'A newer changed original.', 'new-page')
        done = self.service.reconcile(pending['id'])
        self.assertEqual(done['state'], 'completed')
        versions = {c['version_id'] for c in done['answer']['claims'][0]['citations']}
        self.assertIn(self.background['version_id'], versions)

    def test_unconfigured_no_two_sources_and_invalid_version_never_dispatch(self):
        service = self.Service(self.store)
        with self.assertRaises(self.Error):
            service.generate(self.focal['item_id'], self.focal['version_id'], 'unconfigured')
        self.assertEqual(service.records(self.focal['item_id']), [])
        for version, focus in [('99999', ''), (self.focal['version_id'], 'x' * 501)]:
            with self.assertRaises((self.Error, ValueError)):
                self.service.generate(self.focal['item_id'], version, 'bad-input', focus=focus)
        self.assertEqual(self.provider.sent, [])

    def request(self, app, path, fields=None, origin='http://127.0.0.1:8765'):
        env = {}; setup_testing_defaults(env)
        p = urlsplit(path)
        env.update(PATH_INFO=p.path, QUERY_STRING=p.query, HTTP_HOST='127.0.0.1:8765',
                   SERVER_NAME='127.0.0.1', SERVER_PORT='8765')
        if fields is not None:
            body = urlencode(fields).encode()
            env.update(REQUEST_METHOD='POST', HTTP_ORIGIN=origin,
                       CONTENT_TYPE='application/x-www-form-urlencoded', CONTENT_LENGTH=str(len(body)))
            env['wsgi.input'] = io.BytesIO(body)
        status = []
        body = b''.join(app(env, lambda s, h: status.append(int(s.split()[0])))).decode()
        return status[0], body

    def test_reading_page_security_search_export_and_no_get_dispatch(self):
        app = create_app(self.db, connection_provider=self.provider)
        path = f'/items/{self.focal["item_id"]}'
        code, page = self.request(app, path)
        self.assertEqual(code, 200)
        self.assertIn('串連既有知識', page)
        self.assertIn('PRIVATE-LOCAL-NOTE-STAR', page)
        self.assertEqual(self.provider.sent, [])
        csrf = Forms(self.request(app, '/')[1]).csrf
        form = dict(csrf=csrf, version_id=self.focal['version_id'], request_id='page-generate', focus='')
        self.assertEqual(self.request(app, path+'/connections', dict(form, csrf='wrong'))[0], 403)
        self.assertEqual(self.request(app, path+'/connections', form, origin='https://evil.example')[0], 403)
        self.assertEqual(self.provider.sent, [])
        self.assertEqual(self.request(app, path+'/connections', form)[0], 303)
        page = self.request(app, path)[1]
        self.assertIn('兩來源比較', page)
        self.assertNotIn('<script>bad()</script>', page)
        self.assertIn('星圖接續', self.request(app, '/search?'+urlencode({'q':'星圖接續'}))[1])
        self.assertIn('星圖接續', json.dumps(json.loads(self.request(app, '/export.json')[1]), ensure_ascii=False))
        self.assertEqual(len(self.provider.sent), 1)


if __name__ == '__main__': unittest.main()
