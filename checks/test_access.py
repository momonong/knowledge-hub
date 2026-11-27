"""Independent KH login, management isolation and private state regression."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import io
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub.hosting import MountedApplication, HomeSessionAuthorizer
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from checks.test_study import entry

WEBAUTHN = unittest.skipUnless(importlib.util.find_spec('fido2'), 'requires optional fido2')


class SoftwareAuthenticator:
    """Build WebAuthn test responses and sign assertions with a test-only key."""

    def __init__(self, marker):
        from cryptography.hazmat.primitives.asymmetric import ec

        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = bytes([marker]) * 32
        self.user_id = None
        self.count = 0

    def create(self, options, *, origin='https://momonong.me'):
        from hashlib import sha256
        from fido2.cose import ES256
        from fido2.utils import websafe_encode
        from fido2.webauthn import (AttestationObject, AttestedCredentialData, AuthenticatorData,
                                   CollectedClientData)

        self.user_id = options['user']['id']
        credential_data = AttestedCredentialData.create(bytes(16), self.id,
                                                        ES256.from_cryptography_key(self.key.public_key()))
        flags = AuthenticatorData.FLAG.AT | AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.UV
        data = AuthenticatorData.create(sha256(b'momonong.me').digest(), flags, 0, credential_data)
        client = CollectedClientData.create('webauthn.create', options['challenge'], origin)
        return {'id': websafe_encode(self.id), 'rawId': websafe_encode(self.id), 'type': 'public-key',
                'response': {'clientDataJSON': client.b64,
                             'attestationObject': websafe_encode(AttestationObject.create('none', data, {}))}}

    def assert_credential(self, options, *, origin='https://momonong.me', rp=b'momonong.me', uv=True,
                          cross_origin=False, invalid_signature=False, user_handle=None):
        from hashlib import sha256
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from fido2.utils import websafe_encode
        from fido2.webauthn import AuthenticatorData, CollectedClientData

        self.count += 1
        flags = AuthenticatorData.FLAG.UP
        if uv:
            flags |= AuthenticatorData.FLAG.UV
        data = AuthenticatorData.create(sha256(rp).digest(), flags, self.count)
        client = CollectedClientData.create('webauthn.get', options['challenge'], origin, cross_origin)
        signature = b'invalid' if invalid_signature else self.key.sign(data + client.hash, ec.ECDSA(hashes.SHA256()))
        return {'id': websafe_encode(self.id), 'rawId': websafe_encode(self.id), 'type': 'public-key',
                'response': {'clientDataJSON': client.b64, 'authenticatorData': websafe_encode(data),
                             'signature': websafe_encode(signature),
                             'userHandle': self.user_id if user_handle is None else user_handle}}


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

    def passkey(self, route, body, csrf=None):
        return self.request('/knowledge/access/passkey/' + route, body,
                            csrf=self.session()['csrf_token'] if csrf is None else csrf)

    def enroll(self, authenticator, name, **options):
        begin = self.passkey('register/begin', {'name': name})
        self.assertEqual(begin['status'], 200, begin['body'])
        public = json.loads(begin['body'])['publicKey']
        self.assertEqual(public['rp']['id'], 'momonong.me')
        self.assertEqual(public['authenticatorSelection']['authenticatorAttachment'], 'platform')
        self.assertEqual(public['authenticatorSelection']['residentKey'], 'required')
        self.assertEqual(public['authenticatorSelection']['userVerification'], 'required')
        result = self.passkey('register/finish', {'credential': authenticator.create(public, **options)})
        self.assertEqual(result['status'], 200, result['body'])
        return result

    def assertion_options(self):
        begin = self.passkey('login/begin', {})
        self.assertEqual(begin['status'], 200, begin['body'])
        payload = json.loads(begin['body'])
        self.assertEqual(set(payload), {'publicKey'})
        public = payload['publicKey']
        self.assertEqual(public['rpId'], 'momonong.me')
        self.assertEqual(public['userVerification'], 'required')
        self.assertFalse(public.get('allowCredentials'))
        self.assertNotIn('user', public)
        return public

    @WEBAUTHN
    def test_trust_cookie_rotates_expires_and_passkey_renews_fixed_30_days(self):
        self.configure()
        self.assertFalse(self.session()['can_register_passkey'])
        self.assertEqual(self.login()['status'], 200)
        self.assertTrue(self.session()['passkeys_available'])
        self.assertTrue(self.session()['can_register_passkey'])
        password_cookie = self.cookies
        authenticator = SoftwareAuthenticator(1)
        now = time.time()
        with patch('knowledge_hub.access.time.time', return_value=now):
            result = self.enroll(authenticator, '折疊手機')
        self.assertNotEqual(self.cookies, password_cookie)
        self.assertEqual(self.request(cookie=password_cookie)['status'], 401)
        self.assertIn('Max-Age=2592000', result['headers']['Set-Cookie'])
        status = json.loads(result['body'])
        self.assertEqual(status['trusted_device']['name'], '折疊手機')
        self.assertEqual(status['trusted_until'], int(now + 2592000))
        self.assertFalse(status['can_register_passkey'])
        original_cookie = self.cookies
        # The old reader interprets a matching sessions.revision as a password login.
        # It must see this passkey session as anonymous when the app rolls back.
        token = original_cookie.split('=', 1)[1]
        with sqlite3.connect(self.root / 'access.sqlite3') as db:
            row = db.execute('SELECT revision FROM sessions WHERE token=?', (sha256(token.encode()).hexdigest(),)).fetchone()
        self.assertEqual(row, (None,))
        with patch('knowledge_hub.access.time.time', return_value=now + 2591999):
            self.assertEqual(self.request()['status'], 200)
            self.assertEqual(self.session()['trusted_until'], status['trusted_until'])
        with patch('knowledge_hub.access.time.time', return_value=now + 2592000):
            self.assertEqual(self.request()['status'], 401)
            options = self.assertion_options()
            anonymous_cookie = self.cookies
            result = self.passkey('login/finish', {'credential': authenticator.assert_credential(options)})
            self.assertEqual(result['status'], 200, result['body'])
            self.assertNotEqual(self.cookies, anonymous_cookie)
            self.assertIn('Max-Age=2592000', result['headers']['Set-Cookie'])
            self.assertEqual(json.loads(result['body'])['trusted_until'], int(now + 5184000))
            self.assertEqual(self.request()['status'], 200)
            self.assertEqual(self.request(cookie=original_cookie)['status'], 401)

    @WEBAUTHN
    def test_invalid_assertion_consumes_challenge_and_cross_session_attempt_does_not(self):
        self.configure()
        self.login()
        authenticator = SoftwareAuthenticator(2)
        self.enroll(authenticator, '手機')
        self.assertEqual(self.logout()['status'], 200)

        options = self.assertion_options()
        original = self.cookies
        self.cookies = ''
        assertion = authenticator.assert_credential(options)
        self.assertEqual(self.passkey('login/finish', {'credential': assertion})['status'], 400)
        self.cookies = original
        for invalid in ({'origin': 'https://example.momonong.me'},
                        {'origin': 'android:apk-key-hash:' + 'A' * 43},
                        {'rp': b'other.example'}, {'uv': False}, {'cross_origin': True},
                        {'invalid_signature': True}, {'user_handle': 'AA'}):
            with self.subTest(invalid=invalid):
                options = self.assertion_options()
                rejected = authenticator.assert_credential(options, **invalid)
                self.assertEqual(self.passkey('login/finish', {'credential': rejected})['status'], 401)
                good = authenticator.assert_credential(options)
                self.assertEqual(self.passkey('login/finish', {'credential': good})['status'], 400)
                self.assertEqual(self.request()['status'], 401)
        options = self.assertion_options()
        self.assertEqual(self.passkey('login/finish', {'credential': authenticator.assert_credential(options)})['status'], 200)

    @WEBAUTHN
    def test_device_revocation_clears_only_one_device_and_password_reset_clears_every_device(self):
        self.configure()
        first = SoftwareAuthenticator(3)
        self.login()
        native_origin = 'android:apk-key-hash:5Z0ZXgCQakigsnXNBuEimYYqXaS-zuJUefzHwkcKTv4'
        first_db = json.loads(self.enroll(first, '手機', origin=native_origin)['body'])['trusted_device']
        first_cookie = self.cookies
        self.cookies = ''
        self.login()
        second = SoftwareAuthenticator(4)
        second_db = json.loads(self.enroll(second, '平板')['body'])['trusted_device']
        second_cookie = self.cookies
        self.cookies = ''
        pending = self.assertion_options()
        self.assertIsNone(self.session()['trusted_device'])
        revoke_path = '/knowledge/access/devices/revoke'
        list_path = '/knowledge/access/devices/list'
        self.assertEqual(self.request(revoke_path, {'id': first_db['id']}, csrf='owner-csrf')['status'], 403)
        self.assertEqual(self.request(list_path, {}, csrf='owner-csrf')['status'], 403)
        self.owner.return_value = True
        devices = json.loads(self.request(list_path, {}, csrf='owner-csrf')['body'])['devices']
        self.assertEqual({device['name'] for device in devices}, {'手機', '平板'})
        self.assertTrue(all(device['revoked_at'] is None for device in devices))
        self.assertEqual(self.request(revoke_path, {'id': first_db['id']}, csrf='owner-csrf')['status'], 200)
        devices = json.loads(self.request(list_path, {}, csrf='owner-csrf')['body'])['devices']
        self.assertIsNotNone(next(device['revoked_at'] for device in devices if device['id'] == first_db['id']))
        self.owner.return_value = False
        self.assertEqual(self.request(cookie=first_cookie)['status'], 401)
        self.assertEqual(self.request(cookie=second_cookie)['status'], 200)
        revoked = self.passkey('login/finish', {'credential': first.assert_credential(pending)})
        self.assertEqual(revoked['status'], 401)
        options = self.assertion_options()
        unknown = self.passkey('login/finish', {'credential': SoftwareAuthenticator(99).assert_credential(options)})
        self.assertEqual(unknown['status'], revoked['status'])
        self.assertEqual(json.loads(unknown['body']), {'error': 'invalid_passkey'})
        self.assertEqual(unknown['body'], revoked['body'])
        self.login()
        begin = self.passkey('register/begin', {'name': '復活'})
        duplicate = first.create(json.loads(begin['body'])['publicKey'])
        self.assertEqual(self.passkey('register/finish', {'credential': duplicate})['status'], 409)
        self.configure('An updated private password')
        self.assertEqual(self.request(cookie=second_cookie)['status'], 401)
        options = self.assertion_options()
        self.assertEqual(self.passkey('login/finish', {'credential': second.assert_credential(options)})['status'], 401)
        self.assertEqual(self.login('An updated private password')['status'], 200)
        self.assertNotEqual(first_db['id'], second_db['id'])

    @WEBAUTHN
    def test_parallel_assertion_submissions_issue_only_one_trust_session(self):
        self.configure()
        self.login()
        authenticator = SoftwareAuthenticator(6)
        self.enroll(authenticator, '手機')
        trusted_cookie = self.cookies
        self.cookies = ''
        options = self.assertion_options()
        assertion = authenticator.assert_credential(options)
        csrf = self.session()['csrf_token']
        anonymous_cookie = self.cookies
        barrier = Barrier(2)

        def finish():
            barrier.wait(timeout=5)
            return self.request('/knowledge/access/passkey/login/finish', {'credential': assertion},
                                csrf=csrf, cookie=anonymous_cookie)

        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda _: finish(), range(2)))
        self.assertEqual(sorted(result['status'] for result in results), [200, 403])
        self.assertEqual(self.request()['status'], 200)
        self.assertEqual(self.request(cookie=trusted_cookie)['status'], 401)
        self.assertEqual(self.request(cookie=anonymous_cookie)['status'], 401)

    def logout(self):
        return self.request('/knowledge/access/logout', {}, csrf=self.session()['csrf_token'])

    @WEBAUTHN
    def test_registration_requires_recent_password_and_current_challenge(self):
        self.configure()
        self.assertEqual(self.passkey('register/begin', {'name': '手機'})['status'], 403)
        self.login()
        begin = self.passkey('register/begin', {'name': '手機'})
        authenticator = SoftwareAuthenticator(5)
        credential = authenticator.create(json.loads(begin['body'])['publicKey'])
        with patch('knowledge_hub.access.time.time', return_value=time.time() + 181):
            self.assertEqual(self.passkey('register/finish', {'credential': credential})['status'], 400)
        with patch('knowledge_hub.access.time.time', return_value=time.time() + 301):
            self.assertEqual(self.passkey('register/begin', {'name': '手機'})['status'], 403)
        self.assertEqual(self.request()['status'], 200)

    def test_legacy_database_password_session_survives_passkey_schema_migration(self):
        password = 'A separate KH password'
        from knowledge_hub.access import password_hash

        path = self.root / 'access.sqlite3'
        path.touch(mode=0o600)
        revision = 'c' * 43
        salt = os.urandom(32)
        token = 'a' * 43
        csrf = 'b' * 43
        now = time.time()
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE password(id INTEGER PRIMARY KEY, salt BLOB, digest BLOB, revision TEXT)')
            db.execute('CREATE TABLE sessions(token TEXT PRIMARY KEY, csrf TEXT, revision TEXT, created REAL, seen REAL)')
            db.execute('INSERT INTO password VALUES(1,?,?,?)', (salt, password_hash(password, salt), revision))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sha256(token.encode()).hexdigest(), csrf, revision, now, now))
        self.cookies = '__Secure-momonong-knowledge=' + token
        self.assertEqual(self.request()['status'], 200)
        self.assertFalse(self.session()['can_register_passkey'])
        password_session = self.login(password)
        self.assertEqual(password_session['status'], 200)
        upgraded_cookie = self.cookies

        # These are the positional statements the old release still uses after rollback.
        anonymous_token, reader_token = ('d' * 43, 'e' * 43)
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sha256(anonymous_token.encode()).hexdigest(), csrf, None, now, now))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sha256(reader_token.encode()).hexdigest(), csrf, revision, now, now))
            upgraded_token = upgraded_cookie.split('=', 1)[1]
            row = db.execute('SELECT revision FROM sessions WHERE token=?', (sha256(upgraded_token.encode()).hexdigest(),)).fetchone()
        self.assertEqual(row, (revision,))
        self.access._state = None
        self.assertEqual(self.request(cookie='__Secure-momonong-knowledge=' + anonymous_token)['status'], 401)
        self.assertEqual(self.request(cookie='__Secure-momonong-knowledge=' + reader_token)['status'], 200)
        self.assertEqual(self.request(cookie=upgraded_cookie)['status'], 200)

    @WEBAUTHN
    def test_reopen_after_legacy_reset_removes_orphans_and_marks_credentials_revoked(self):
        from knowledge_hub.access import password_hash

        self.configure()
        self.login()
        authenticator = SoftwareAuthenticator(7)
        trusted = self.enroll(authenticator, '手機')
        trusted_cookie = self.cookies
        device_id = json.loads(trusted['body'])['trusted_device']['id']
        self.cookies = ''
        self.assertion_options()
        before_reset = self.cookies

        # 1.0.6 changes the revision and deletes sessions without foreign keys enabled.
        password = 'A legacy reset password'
        salt = os.urandom(32)
        path = self.root / 'access.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('INSERT OR REPLACE INTO password VALUES(1,?,?,?)', (salt, password_hash(password, salt), 'f' * 43))
            db.execute('DELETE FROM sessions')
            self.assertEqual(db.execute('SELECT count(*) FROM passkey_challenges').fetchone(), (1,))

        self.access._state = None
        self.assertEqual(self.request(cookie=trusted_cookie)['status'], 401)
        self.assertEqual(self.request(cookie=before_reset)['status'], 401)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('SELECT count(*) FROM passkey_challenges').fetchone(), (0,))
        self.owner.return_value = True
        devices = json.loads(self.request('/knowledge/access/devices/list', {}, csrf='owner-csrf')['body'])['devices']
        self.assertIsNotNone(next(device['revoked_at'] for device in devices if device['id'] == device_id))
        self.owner.return_value = False
        self.assertEqual(self.login(password)['status'], 200)

    @unittest.skipIf(importlib.util.find_spec('fido2'), 'requires running without optional fido2')
    def test_missing_dependency_fails_closed_for_passkeys_only(self):
        self.configure()
        self.assertFalse(self.session()['passkeys_available'])
        self.assertEqual(self.passkey('login/begin', {})['status'], 503)
        self.assertEqual(self.login()['status'], 200)
        self.assertEqual(self.request()['status'], 200)

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

    def test_login_and_reader_share_brand_assets_with_separate_access_paths(self):
        login_page = self.request()['body']
        public = {}
        for name, mime in [('palette.css', 'text/css'), ('book.svg', 'image/svg+xml')]:
            with self.subTest(asset=name):
                path = '/knowledge/access/' + name
                self.assertIn(path, login_page)
                result = self.request(path)
                self.assertEqual(result['status'], 200)
                self.assertEqual(result['headers']['Content-Type'].split(';')[0], mime)
                self.assertEqual(self.request('/knowledge/static/' + name)['status'], 401)
                public[name] = result['body']
        self.assertIn('--ground: #f6f3e9', public['palette.css'])
        self.assertIn('--ink: #252d2e', public['palette.css'])
        self.assertIn('--accent: #284f65', public['palette.css'])
        self.assertIn('--highlight: #f3dd8b', public['palette.css'])
        self.assertIn('M25,43 Q25,40 28,41', public['book.svg'])
        self.assertEqual(self.request('/knowledge/access/reader.css')['status'], 404)
        self.configure()
        self.login()
        for name, body in public.items():
            self.assertEqual(self.request('/knowledge/static/' + name)['body'], body)

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
