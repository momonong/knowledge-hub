"""Independent KH login, management isolation and private state regression."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub.hosting import MountedApplication, HomeSessionAuthorizer
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from checks.test_study import entry


class Access(unittest.TestCase):
    def setUp(self):
        from knowledge_hub.access import ReadingAccess
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = KnowledgeStore(self.root/'knowledge.sqlite3')
        self.item, self.version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2601.00001', 'Private reading title', 'Public article evidence.'))
        self.owner = Mock(return_value=False)
        self.access = ReadingAccess(self.root/'access.sqlite3', prefix='/knowledge', owner=self.owner)
        self.app = MountedApplication(create_app(self.root/'knowledge.sqlite3'),
            origin='https://momonong.me', prefix='/knowledge', authorize=self.access.authorize, access=self.access)
        self.cookies = ''

    def request(self, path='/knowledge/', value=None, csrf='', cookie=None, origin='https://momonong.me', raw=None, content_type='application/json'):
        env = {}; setup_testing_defaults(env)
        parsed=urlsplit(path)
        env.update(PATH_INFO=parsed.path, QUERY_STRING=parsed.query, HTTP_HOST='momonong.me',
                   REMOTE_ADDR='127.0.0.1', HTTP_X_FORWARDED_PROTO='https',
                   HTTP_COOKIE=self.cookies if cookie is None else cookie)
        if value is not None or raw is not None:
            data=raw if raw is not None else json.dumps(value).encode()
            env.update(REQUEST_METHOD='POST', HTTP_ORIGIN=origin, HTTP_X_CSRF_TOKEN=csrf,
                       CONTENT_TYPE=content_type, CONTENT_LENGTH=str(len(data)))
            env['wsgi.input']=io.BytesIO(data)
        result={}
        def start(status, headers):
            result.update(status=int(status.split()[0]),headers=dict(headers))
        result['body']=b''.join(self.app(env,start)).decode()
        if 'Set-Cookie' in result['headers']: self.cookies=result['headers']['Set-Cookie'].split(';')[0]
        return result

    def session(self): return json.loads(self.request('/knowledge/access/session')['body'])

    def configure(self, password='A separate KH password'):
        self.owner.return_value=True
        result=self.request('/knowledge/access/password', {'password':password}, csrf='owner-csrf')
        self.owner.return_value=False
        self.assertEqual(result['status'],200)

    def login(self, password='A separate KH password'):
        session=self.session()
        return self.request('/knowledge/access/login',{'password':password},csrf=session['csrf_token'])

    def test_unconfigured_is_locked_even_with_home_or_another_reader_cookie(self):
        self.owner.return_value=True
        for cookie in ('__Host-momonong-home='+'a'*43, '__Secure-momonong-lately='+'b'*43):
            for path in ('/knowledge/','/knowledge/export.json',f'/knowledge/items/{self.item}'):
                result=self.request(path,cookie=cookie)
                self.assertEqual(result['status'],401)
                self.assertNotIn('Private reading title',result['body'])
        self.owner.assert_not_called()
        self.assertFalse(self.session()['configured'])

    def test_owner_can_set_without_gaining_reader_access(self):
        self.configure()
        self.assertTrue(self.session()['configured'])
        self.assertEqual(self.request(cookie='__Host-momonong-home='+'a'*43)['status'],401)
        self.assertEqual(self.login()['status'],200)
        self.assertIn('Private reading title',self.request(f'/knowledge/items/{self.item}')['body'])

    def test_cookie_rotation_attributes_logout_and_persistent_login(self):
        self.configure(); self.session(); old=self.cookies
        result=self.login(); self.assertNotEqual(old,self.cookies)
        for value in ('Secure','HttpOnly','SameSite=Strict','Path=/knowledge/'): self.assertIn(value,result['headers']['Set-Cookie'])
        self.assertEqual(self.request(cookie=old)['status'],401)
        from knowledge_hub.access import ReadingAccess
        restarted=ReadingAccess(self.root/'access.sqlite3',prefix='/knowledge',owner=self.owner)
        self.assertTrue(restarted.authorize({'HTTP_COOKIE':self.cookies}))
        token=self.session()['csrf_token']
        self.assertEqual(self.request('/knowledge/access/logout',{},csrf=token)['status'],200)
        self.assertEqual(self.request()['status'],401)

    def test_rotation_revokes_sessions_preserves_private_data_and_other_cookies(self):
        from knowledge_hub.reading import ReadingState
        reading=ReadingState(self.store)
        reading.save(self.item,self.version,0,{'reflection':'Private understanding must survive','question_draft':'Unsent private question'})
        before=self.store.export_data()
        self.configure(); self.login(); old=self.cookies
        self.configure('Another independent phrase')
        self.assertEqual(self.request(cookie=old)['status'],401)
        self.assertEqual(self.login()['status'],401)
        self.assertEqual(self.login('Another independent phrase')['status'],200)
        after=self.store.export_data()
        before.pop('exported_at',None);after.pop('exported_at',None)
        self.assertEqual(after,before)
        self.assertEqual(reading.get(self.item,self.version)['reflection'],'Private understanding must survive')

    def test_setting_requires_owner_and_csrf_and_exact_origin(self):
        self.assertEqual(self.request('/knowledge/access/password',{'password':'A separate KH password'})['status'],403)
        self.configure(); self.login()
        self.assertEqual(self.request('/knowledge/access/password',{'password':'Another password phrase'},csrf=self.session()['csrf_token'])['status'],403)
        self.owner.return_value=True
        self.assertEqual(self.request('/knowledge/access/password',{'password':'Another password phrase'},origin='https://evil.test')['status'],403)
        self.owner.return_value=None
        self.assertEqual(self.request('/knowledge/access/password',{'password':'Another password phrase'})['status'],503)

    def test_login_csrf_schema_limits_and_rate_survive_restart(self):
        self.configure(); csrf=self.session()['csrf_token']
        self.assertEqual(self.request('/knowledge/access/login',{'password':'A separate KH password'})['status'],403)
        for raw in (b'{"password":"x","password":"y"}', b'{"password":"x","extra":1}', b'x'*4097):
            self.assertEqual(self.request('/knowledge/access/login',raw=raw,csrf=csrf)['status'],400)
        for _ in range(10): self.assertEqual(self.request('/knowledge/access/login',{'password':'wrong'},csrf=csrf)['status'],401)
        self.access._state=None
        self.assertEqual(self.request('/knowledge/access/login',{'password':'A separate KH password'},csrf=csrf)['status'],429)

    def test_sessions_expire_and_bad_state_fails_closed(self):
        self.configure(); self.login()
        import time
        with patch('knowledge_hub.access.time.time',return_value=time.time()+1801):
            self.assertEqual(self.request()['status'],401)
        os.chmod(self.root/'access.sqlite3',0o644)
        self.assertEqual(self.request()['status'],503)
        self.assertEqual(self.request('/knowledge/healthz')['status'],200)

    def test_reading_save_reopen_requires_independent_login(self):
        from knowledge_hub.reading import ReadingState
        self.configure(); self.login()
        self.assertEqual(self.request(f'/knowledge/items/{self.item}')['status'],200)
        raw=urlencode({'csrf':self.store.get_csrf_token(),'version_id':self.version,'revision':'0',
                       'reflection':'My saved understanding','question_draft':'Continue this question'}).encode()
        response=self.request(f'/knowledge/items/{self.item}/reading',raw=raw,content_type='application/x-www-form-urlencoded')
        self.assertEqual(response['status'],200)
        self.assertEqual(json.loads(response['body'])['reflection'],'My saved understanding')
        token=self.session()['csrf_token']
        self.request('/knowledge/access/logout',{},csrf=token)
        self.assertEqual(self.request(f'/knowledge/items/{self.item}')['status'],401)
        self.assertEqual(self.login()['status'],200)
        page=self.request(f'/knowledge/items/{self.item}?version={self.version}')
        self.assertIn('My saved understanding',page['body'])
        self.assertEqual(ReadingState(self.store).get(self.item,self.version)['question_draft'],'Continue this question')

    def test_public_auth_assets_and_settings_do_not_include_private_content(self):
        for path in ('/knowledge/settings/','/knowledge/access/login','/knowledge/access/access.js','/knowledge/access/access.css'):
            r=self.request(path)
            self.assertEqual(r['status'],200)
            self.assertNotIn('Private reading title',r['body'])
            self.assertEqual(r['headers']['Cache-Control'],'no-store')
        self.assertEqual(self.request('/knowledge/access/../access.sqlite3')['status'],404)


class OwnerCsrf(unittest.TestCase):
    def test_owner_requires_matching_live_csrf_and_only_forwards_home_cookie(self):
        auth=HomeSessionAuthorizer(18090,'https://momonong.me')
        env={'HTTP_COOKIE':'__Host-momonong-home='+'a'*43,'HTTP_X_CSRF_TOKEN':'b'*43}
        with patch.object(auth,'session',return_value={'authenticated':True,'csrf_token':'b'*43}):
            self.assertIs(auth.authorize_change(env),True)
            self.assertIs(auth.authorize_change({**env,'HTTP_X_CSRF_TOKEN':'c'*43}),False)
        with patch.object(auth,'session',return_value=None): self.assertIsNone(auth.authorize_change(env))


if __name__=='__main__': unittest.main()
