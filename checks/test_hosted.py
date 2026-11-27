"""Hosted behavior: private data must stay gated and writes stay under the mount."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from checks.test_study import entry


class Hosted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'hub.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.item, self.version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2601.00001', 'Protected paper title', 'Public article evidence.'))

    def request(self, path='/knowledge/', *, fields=None, allow=True, host='momonong.me',
                origin='https://momonong.me', forwarded='https', peer='127.0.0.1', cookie='', user_agent=''):
        from knowledge_hub import hosting
        app = hosting.MountedApplication(create_app(self.db), origin='https://momonong.me',
                                         prefix='/knowledge', authorize=lambda env: allow)
        env = {}; setup_testing_defaults(env)
        parsed = urlsplit(path)
        env.update(PATH_INFO=parsed.path, QUERY_STRING=parsed.query, HTTP_HOST=host,
                   HTTP_X_FORWARDED_PROTO=forwarded, REMOTE_ADDR=peer, HTTP_COOKIE=cookie, HTTP_USER_AGENT=user_agent)
        if fields is not None:
            raw = urlencode({'csrf': self.store.get_csrf_token(), **fields}).encode()
            env.update(REQUEST_METHOD='POST', HTTP_ORIGIN=origin,
                       CONTENT_TYPE='application/x-www-form-urlencoded', CONTENT_LENGTH=str(len(raw)))
            env['wsgi.input'] = io.BytesIO(raw)
        result = {}
        def start(status, headers):
            result.update(status=int(status.split()[0]), headers=dict(headers))
        result['body'] = b''.join(app(env, start)).decode()
        return result

    def test_anonymous_and_expired_session_cannot_read_export_or_save(self):
        for path, fields in [('/knowledge/',None),('/knowledge/export.json',None),
                             (f'/knowledge/items/{self.item}/reading',{'version_id':self.version,'revision':0,'reflection':'secret'})]:
            response=self.request(path,fields=fields,allow=False)
            self.assertEqual(response['status'],401)
            self.assertNotIn('Protected paper title',response['body'])
            self.assertNotIn(self.store.get_csrf_token(),response['body'])
        from knowledge_hub.reading import ReadingState
        self.assertEqual(ReadingState(self.store).get(self.item,self.version)['reflection'],'')

    def test_unavailable_home_fails_closed(self):
        response=self.request(allow=None)
        self.assertEqual(response['status'],503)
        self.assertNotIn('Protected paper title',response['body'])
        self.assertIn('href="data:image/svg+xml,', response['body'])
        self.assertNotIn('/sites/_icons/', response['body'])
        self.assertIn('img-src data:', response['headers']['Content-Security-Policy'])

    def test_native_navigation_markup_requires_the_exact_app_capability(self):
        native = 'Mozilla/5.0 (Linux; Android) Chrome/130 KnowledgeHubAndroid/2'
        path = f'/knowledge/items/{self.item}?version={self.version}'
        for agent in (native, 'Android wv', native + '0', native.replace('/2', '/1')):
            with self.subTest(agent=agent):
                response = self.request(path, user_agent=agent)
                self.assertEqual(response['status'], 200)
                self.assertEqual(' data-native-reader>' in response['body'], agent == native)
                self.assertIn('User-Agent', response['headers']['Vary'])
                self.assertIn('Cookie', response['headers']['Vary'])
                self.assertIn('href="/knowledge/reading"', response['body'])
        self.assertEqual(self.request(path, user_agent=native, allow=False)['status'], 401)

    def test_all_internal_urls_are_mounted_but_article_text_is_unchanged(self):
        response=self.request(f'/knowledge/items/{self.item}?version={self.version}')
        self.assertEqual(response['status'],200)
        for fragment in ('href="/knowledge/"','src="/knowledge/static/reader.js"',
                         'href="/knowledge/static/reader.css"','action="/knowledge/items/',
                         'name="kh-base-path" content="/knowledge"'):
            self.assertIn(fragment,response['body'])
        self.assertIn('Public article evidence.',response['body'])
        self.assertNotIn('action="/items/',response['body'])
        self.assertEqual(int(response['headers']['Content-Length']),len(response['body'].encode()))

    def test_correct_origin_and_csrf_save_then_redirect_stays_in_mount(self):
        response=self.request(f'/knowledge/items/{self.item}/reading',fields={
            'version_id':self.version,'revision':0,'reflection':'Hosted understanding'})
        self.assertEqual(response['status'],200)
        self.assertEqual(json.loads(response['body'])['reflection'],'Hosted understanding')
        response=self.request(f'/knowledge/items/{self.item}/status',fields={'status':'read'})
        self.assertEqual(response['status'],303)
        self.assertTrue(response['headers']['Location'].startswith('/knowledge/items/'))

    def test_host_scheme_proxy_peer_and_origin_are_enforced(self):
        for options in ({'host':'evil.example'},{'forwarded':'http'},{'peer':'192.0.2.4'},
                        {'host':'momonong.me.evil.example'},{'forwarded':'https,http'}):
            self.assertIn(self.request(**options)['status'],(400,403))
        for origin in ('https://evil.example','http://momonong.me','https://momonong.me/','null'):
            self.assertEqual(self.request(f'/knowledge/items/{self.item}/reading',origin=origin,
                fields={'version_id':self.version,'revision':0,'reflection':'bad'})['status'],403)

    def test_health_is_public_without_database_content_and_prefix_is_exact(self):
        response=self.request('/knowledge/healthz',allow=False)
        self.assertEqual(response['status'],200)
        self.assertEqual(json.loads(response['body']),{'status':'ok'})
        self.assertEqual(self.request('/knowledgeable/')['status'],404)
        self.assertEqual(self.request('/items/1')['status'],404)
        self.assertEqual(self.request('/knowledge')['headers']['Location'],'/knowledge/')

    def test_static_assets_are_served_inside_prefix(self):
        response=self.request('/knowledge/static/reader.js')
        self.assertEqual(response['status'],200)
        self.assertTrue(response['headers']['Content-Type'].startswith('text/javascript'))


class HomeTransport(unittest.TestCase):
    def test_only_home_cookie_is_forwarded_and_strict_boolean_is_required(self):
        from knowledge_hub.hosting import HomeSessionAuthorizer
        token='a'*43
        class Response:
            status=200
            def read(self, limit): return json.dumps({'authenticated':True,'csrf_token':'discarded'}).encode()
        sent=[]
        class Connection:
            def __init__(self, host, port, timeout):
                self.host=host; self.port=port
            def request(self, method, path, headers): sent.append((self.host,self.port,method,path,headers))
            def getresponse(self): return Response()
            def close(self): pass
        auth=HomeSessionAuthorizer(18090,'https://momonong.me')
        with patch('knowledge_hub.hosting.HTTPConnection',Connection):
            self.assertIs(auth({'HTTP_COOKIE':'other=private; __Host-momonong-home='+token}),True)
            self.assertEqual(sent[0][:4],('127.0.0.1',18090,'GET','/home-api/session'))
            self.assertEqual(sent[0][4]['Cookie'],'__Host-momonong-home='+token)
            self.assertIs(auth({'HTTP_COOKIE':''}),False)
            self.assertEqual(len(sent),1)
            with patch.object(Response,'read',return_value=b'{"authenticated":"true"}'):
                self.assertIs(auth({'HTTP_COOKIE':'__Host-momonong-home='+token}),None)
            with patch.object(Response,'read',side_effect=TimeoutError):
                self.assertIs(auth({'HTTP_COOKIE':'__Host-momonong-home='+token}),None)


if __name__=='__main__': unittest.main()
