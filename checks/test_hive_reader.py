"""Private video and figure links must stay attached to the exact published version."""
import json
import io
import secrets
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub.hosting import MountedApplication
from knowledge_hub.sources import parse_arxiv_html
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app

PAPER = 'https://arxiv.org/html/2611.12872v1'
HTML = b'''<article><h1>HiVe-Mem</h1><p id="S3.p1">Memory evidence selection.</p>
<figure id="S3.F1" class="ltx_figure"><img src="2611.12872v1/hivemem.png">
<figcaption>Figure 1: Evidence selection.</figcaption></figure></article>'''


class PrivateReaderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = store = KnowledgeStore(root/'fixture.sqlite3')
        self.item, self.version, _ = store.upsert_parsed_entry(parse_arxiv_html(HTML, PAPER))
        alternate = HTML.replace(b'HiVe-Mem', b'New version')
        _, self.later, _ = store.upsert_parsed_entry(parse_arxiv_html(alternate, PAPER[:-1]+'2'))
        self.media = root/'media'
        (self.media/'figures').mkdir(parents=True)
        for name in ('guided.mp4', 'original.mp4'):
            (self.media/name).write_bytes(b'abcdefghij' * 10000)
        (self.media/'figures/paper.html').write_bytes(HTML)
        (self.media/'figures/hivemem.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        self.allowed = True
        app = create_app(store.db_path, article_media_root=self.media, fetch=lambda url: self.fail('unexpected fetch '+url))
        self.host = MountedApplication(app, origin='https://momonong.me', prefix='/knowledge', authorize=lambda env: self.allowed)

    def get(self, suffix='', *, version=None, method='GET', extra=None):
        env = {}
        setup_testing_defaults(env)
        env.update(PATH_INFO=f'/knowledge/items/{self.item}'+suffix, QUERY_STRING='version='+str(version or self.version),
                   HTTP_HOST='momonong.me', HTTP_X_FORWARDED_PROTO='https', REMOTE_ADDR='127.0.0.1', REQUEST_METHOD=method)
        env.update(extra or {})
        response = {}
        def start(status, headers):
            response.update(status=int(status[:3]), headers=dict(headers))
        body = self.host(env, start)
        return response, body

    def test_version_bound_players_and_private_ranges(self):
        response, page = self.get()
        markup = b''.join(page)
        self.assertIn(b'media-src \'self\'', response['headers']['Content-Security-Policy'].encode())
        self.assertEqual(markup.count(b'<video '), 2)
        self.assertIn(f'/knowledge/items/{self.item}/media/guided.mp4?version={self.version}'.encode(), markup)
        self.assertIn(b'data-guide-search', markup)
        self.assertNotIn(b'<video ', b''.join(self.get(version=self.later)[1]))
        self.assertEqual(self.get('/media/guided.mp4', version=self.later)[0]['status'], 404)
        response, stream = self.get('/media/original.mp4')
        self.assertEqual(response['status'], 200)
        try:
            self.assertLess(len(next(iter(stream))), 100000)
        finally:
            stream.close()
        response, stream = self.get('/media/guided.mp4', extra={'HTTP_RANGE':'bytes=-4'})
        self.assertEqual((response['status'],b''.join(stream)), (206,b'ghij'))
        self.assertEqual(response['headers']['Vary'], 'Cookie')
        response, stream = self.get('/media/guided.mp4', method='HEAD')
        self.assertEqual((response['headers']['Content-Length'], b''.join(stream)), ('100000', b''))
        self.allowed = False
        for suffix in ('', '/media/original.mp4','/visuals','/visuals/0/0'):
            self.assertEqual(self.get(suffix)[0]['status'], 401)

    def test_fixed_publisher_evidence_and_guide_available_offline(self):
        panel = json.loads(b''.join(self.get('/visuals')[1]))['panels'][0]
        self.assertEqual(panel['anchor'], 'S3.F1')
        self.assertIn('m8', panel['guide']['question'])
        self.assertEqual(len(panel['steps']),3)
        self.assertEqual(b''.join(self.get('/visuals/0/0')[1]), b'\x89PNG\r\n\x1a\n')

    def test_saved_time_and_figure_return_to_the_correct_version(self):
        from knowledge_hub.reading import ReadingState
        from knowledge_hub.study import StudyError
        state = ReadingState(self.store)
        initial = state.save(self.item, self.version, 0, {'anchor':'hive-original-t270'})
        self.assertEqual(state.get(self.item,self.version)['anchor'], 'hive-original-t270')
        saved = state.save(self.item, self.version, initial['revision'], {'anchor':'kh-visual-S4.T1'})
        for bad in ('hive-guided-t800', 'kh-visual-madeup'):
            with self.assertRaises(StudyError):
                state.save(self.item, self.version, saved['revision'], {'anchor':bad})
        with self.assertRaises(StudyError):
            state.save(self.item,self.later,0,{'anchor':'hive-guided-t280'})

    def test_save_returns_mounted_confirmation_fragment_to_scripted_reader(self):
        data = urlencode({'csrf':self.store.get_csrf_token(), 'version_id':self.version,
                          'request_id':secrets.token_urlsafe(24), 'understanding':'表 1 對照相同模型',
                          'questions':'', 'record_id':''}).encode()
        response, body = self.get('/progress', method='POST', extra={'HTTP_ORIGIN':'https://momonong.me',
            'CONTENT_TYPE':'application/x-www-form-urlencoded', 'CONTENT_LENGTH':str(len(data)),
            'wsgi.input':io.BytesIO(data), 'HTTP_ACCEPT':'application/json'})
        self.assertEqual(response['status'],200)
        location = json.loads(b''.join(body))['location']
        self.assertEqual(location, f'/knowledge/items/{self.item}?version={self.version}#progress-1')
        self.assertIn(b'id="progress-1"', b''.join(self.get()[1]))


if __name__ == '__main__':
    unittest.main()
