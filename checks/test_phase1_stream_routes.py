import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub.hosting import MountedApplication
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from test_study import entry


class StreamRoutes(unittest.TestCase):
    def test_hosted_mount_passes_event_stream_before_completion_and_closes(self):
        yielded = []
        closed = []
        def upstream(env, start):
            start('200 OK', [('Content-Type','text/event-stream; charset=utf-8')])
            def stream():
                try:
                    for part in [b'first', b'second']:
                        yielded.append(part)
                        yield part
                finally:
                    closed.append(True)
            return stream()
        app = MountedApplication(upstream, origin='https://example.test', prefix='/knowledge',
                                 authorize=lambda env: True)
        env = {'HTTP_HOST':'example.test','REMOTE_ADDR':'127.0.0.1','HTTP_X_FORWARDED_PROTO':'https',
               'PATH_INFO':'/knowledge/test','REQUEST_METHOD':'GET'}
        result = app(env, lambda status, headers: None)
        self.assertEqual(yielded, [])
        iterator = iter(result)
        self.assertEqual(next(iterator), b'first')
        self.assertEqual(yielded, [b'first'])
        iterator.close()
        self.assertEqual(closed, [True])

    def test_real_routes_require_fixed_version_csrf_and_stream_verified_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'hub.sqlite3'
            store = KnowledgeStore(path)
            item, version, _ = store.upsert_parsed_entry(entry('arxiv:2601.00041', 'Agent memory', 'The system retrieves stored episodes and compares memory representations.'))
            app = create_app(path, fetch=lambda url: (_ for _ in ()).throw(OSError('offline')))
            def request(method, suffix, *, fields=None, query=''):
                env = {}
                setup_testing_defaults(env)
                env.update(HTTP_HOST='localhost', HTTP_ORIGIN='http://localhost', REQUEST_METHOD=method,
                           PATH_INFO=f'/items/{item}/{suffix}', QUERY_STRING=query)
                if fields is not None:
                    body = urlencode(fields).encode()
                    env.update(CONTENT_LENGTH=str(len(body)), CONTENT_TYPE='application/x-www-form-urlencoded')
                    env['wsgi.input'] = io.BytesIO(body)
                meta = {}
                output = app(env, lambda status, headers: meta.update(status=status, headers=dict(headers)))
                return meta, b''.join(output).decode()
            params = dict(question='這份原文有沒有列出量子電腦的型號？', version_id=version,
                          request_id='fixture-stream-route-001', route='primary')
            self.assertEqual(request('POST','followup',fields=params)[0]['status'][:3],'403')
            params['csrf'] = store.get_csrf_token()
            self.assertEqual(request('POST','followup',fields={**params,'version_id':'9999'})[0]['status'][:3],'404')
            meta, stream = request('POST','followup',fields=params)
            self.assertTrue(meta['headers']['Content-Type'].startswith('text/event-stream'))
            self.assertEqual([s.splitlines()[0] for s in stream.strip().split('\n\n')],
                             ['event: meta','event: section','event: section','event: section','event: done'])
            self.assertIn('原文未交代', stream)
            meta, guide = request('GET','guide', query='version='+version)
            self.assertEqual(meta['status'][:3], '200')
            self.assertTrue(json.loads(guide)['sections'])
