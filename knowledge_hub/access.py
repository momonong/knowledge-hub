"""Independent KH password and passkey access with Home-only administration.

Mirrors the deployed Home reading-access contract without sharing its database,
verifier or sessions. All state lives beside KH data in a private, separate DB.
"""
from contextlib import contextmanager
from hashlib import sha256, scrypt
from hmac import compare_digest
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
import json
import os
import re
import secrets
import sqlite3
import stat
import time

from . import passkeys

COOKIE = '__Secure-momonong-knowledge'
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
ASSETS = Path(__file__).with_name('static')
ANONYMOUS_SECONDS = 900
PASSWORD_IDLE_SECONDS = 1800
PASSWORD_SECONDS = 28800
PASSWORD_ENROLLMENT_SECONDS = 300
TRUST_SECONDS = 30 * 86400
PASSKEY_FIELDS = {
    '/access/passkey/register/begin': {'name'},
    '/access/passkey/register/finish': {'credential'},
    '/access/passkey/login/begin': set(),
    '/access/passkey/login/finish': {'credential'},
}
OWNER_FIELDS = {
    '/access/password': {'password'},
    '/access/devices/list': set(),
    '/access/devices/revoke': {'id'},
}
POST_FIELDS = {**OWNER_FIELDS, **PASSKEY_FIELDS, '/access/login': {'password'}, '/access/logout': set()}


class Rejected(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def password_valid(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128 or any(c in value for c in '\r\n\0'):
        return False
    try:
        value.encode('utf-8')
        return True
    except UnicodeError:
        return False


def password_hash(password, salt):
    return scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=1, dklen=64, maxmem=64*1024*1024)


def token_from(environ):
    raw = environ.get('HTTP_COOKIE', '')
    if len(raw) > 8192 or sum(part.strip().startswith(COOKIE+'=') for part in raw.split(';')) != 1:
        return ''
    try:
        cookie = SimpleCookie(raw)
        token = cookie[COOKIE].value if COOKIE in cookie else ''
        return token if TOKEN.fullmatch(token) else ''
    except CookieError:
        return ''


def token_digest(token):
    return sha256(token.encode()).hexdigest()


class AccessState:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.check_directory()
        try:
            fd = os.open(self.path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS password(id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL, digest BLOB NOT NULL, revision TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS passkey_devices(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, revision TEXT NOT NULL,
                credential_id BLOB NOT NULL UNIQUE, public_key BLOB NOT NULL,
                sign_count INTEGER NOT NULL, backup_eligible INTEGER NOT NULL,
                created_at REAL NOT NULL, last_used_at REAL NOT NULL, revoked_at REAL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY, csrf TEXT NOT NULL, revision TEXT,
                created REAL NOT NULL, seen REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS session_auth(
                session_token TEXT PRIMARY KEY REFERENCES sessions(token) ON DELETE CASCADE,
                password_at REAL, trust_revision TEXT,
                device_id TEXT REFERENCES passkey_devices(id))''')
            db.execute('CREATE INDEX IF NOT EXISTS session_auth_device ON session_auth(device_id) WHERE device_id IS NOT NULL')
            columns = {column['name'] for column in db.execute('PRAGMA table_info(sessions)')}
            if {'password_at', 'device_id'} <= columns:
                # Move records from the initial local prototype without changing token lifetimes.
                db.execute('''INSERT INTO session_auth(session_token,password_at,trust_revision,device_id)
                    SELECT token,password_at,CASE WHEN device_id IS NOT NULL THEN revision END,device_id
                    FROM sessions WHERE password_at IS NOT NULL OR device_id IS NOT NULL''')
                db.execute('UPDATE sessions SET revision=NULL WHERE device_id IS NOT NULL')
                db.execute('DROP INDEX IF EXISTS sessions_device')
                db.execute('ALTER TABLE sessions DROP COLUMN device_id')
                db.execute('ALTER TABLE sessions DROP COLUMN password_at')
            db.execute('''CREATE TABLE IF NOT EXISTS passkey_challenges(
                session_token TEXT NOT NULL REFERENCES sessions(token) ON DELETE CASCADE,
                purpose TEXT NOT NULL, revision TEXT NOT NULL, state TEXT NOT NULL,
                expires_at REAL NOT NULL, device_id TEXT, name TEXT,
                PRIMARY KEY(session_token, purpose))''')
            db.execute('CREATE TABLE IF NOT EXISTS rates(bucket TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL)')
            # A rollback can delete sessions without cascading, or change the KH password.
            db.execute('DELETE FROM session_auth WHERE NOT EXISTS (SELECT 1 FROM sessions WHERE token=session_auth.session_token)')
            db.execute('DELETE FROM passkey_challenges WHERE NOT EXISTS (SELECT 1 FROM sessions WHERE token=passkey_challenges.session_token)')
            db.execute('''UPDATE passkey_devices SET revoked_at=? WHERE revoked_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM password WHERE id=1 AND revision=passkey_devices.revision)''', (time.time(),))

    def check_directory(self):
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError('Private access directory required')
        # A user namespace may map filesystem-root ownership to an overflow UID.
        root_uid = Path('/').lstat().st_uid
        for parent in self.path.parent.parents:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (root_uid, os.geteuid())
                    or (info.st_mode & 0o022 and not (info.st_uid == root_uid and info.st_mode & stat.S_ISVTX))):
                raise OSError('Unsafe access parent')

    @contextmanager
    def connection(self):
        self.check_directory()
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError('Unsafe access state')
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def password(db):
        row = db.execute('SELECT * FROM password WHERE id=1').fetchone()
        if not row:
            return None
        if (not isinstance(row['salt'], bytes) or len(row['salt']) != 32
                or not isinstance(row['digest'], bytes) or len(row['digest']) != 64
                or not isinstance(row['revision'], str) or not TOKEN.fullmatch(row['revision'])):
            raise OSError('Invalid password state')
        return row

    @staticmethod
    def session(db, token):
        now = time.time()
        digest = token_digest(token)
        row = db.execute('''SELECT s.token,s.csrf,s.created,s.seen,a.password_at,a.device_id,
            coalesce(a.trust_revision,s.revision) AS revision FROM sessions AS s
            LEFT JOIN session_auth AS a ON a.session_token=s.token WHERE s.token=?''', (digest,)).fetchone()
        if not row:
            return None
        if row['device_id']:
            device = AccessState.active_device(db, row['device_id'], row['revision'])
            expired = device is None or now - row['created'] >= TRUST_SECONDS
        elif row['revision']:
            expired = now - row['seen'] >= PASSWORD_IDLE_SECONDS or now - row['created'] >= PASSWORD_SECONDS
        else:
            expired = now - row['created'] >= ANONYMOUS_SECONDS
        if expired:
            db.execute('DELETE FROM sessions WHERE token=?', (digest,))
            return None
        if not row['device_id']:
            db.execute('UPDATE sessions SET seen=? WHERE token=?', (now, digest))
        return row

    @staticmethod
    def active_device(db, device_id, revision):
        return db.execute('SELECT * FROM passkey_devices WHERE id=? AND revision=? AND revoked_at IS NULL',
                          (device_id, revision)).fetchone()

    @staticmethod
    def new_session(db, revision=None, revoke='', *, password_login=False, device_id=None, now=None):
        if now is None:
            now = time.time()
        db.execute('''DELETE FROM sessions WHERE token=? OR token IN (
            SELECT s.token FROM sessions AS s LEFT JOIN session_auth AS a ON a.session_token=s.token
            WHERE (a.device_id IS NOT NULL AND (a.device_id=? OR s.created<=?))
            OR (a.device_id IS NULL AND s.revision IS NULL AND s.created<=?)
            OR (s.revision IS NOT NULL AND (s.seen<=? OR s.created<=?)))''',
            (token_digest(revoke), device_id, now - TRUST_SECONDS, now - ANONYMOUS_SECONDS,
             now - PASSWORD_IDLE_SECONDS, now - PASSWORD_SECONDS))
        # Anonymous traffic cannot evict an authenticated reading session.
        db.execute('''DELETE FROM sessions WHERE token IN (
            SELECT s.token FROM sessions AS s LEFT JOIN session_auth AS a ON a.session_token=s.token
            WHERE s.revision IS NULL AND a.device_id IS NULL ORDER BY s.created DESC, s.token
            LIMIT -1 OFFSET 255)''')
        if password_login:
            db.execute('''DELETE FROM sessions WHERE token IN (
                SELECT token FROM sessions WHERE revision IS NOT NULL ORDER BY created DESC, token
                LIMIT -1 OFFSET 1023)''')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        digest = token_digest(token)
        # The old reader treats a positive revision as password authorization.
        # Store trusted revisions only in the side table so rollback sees an anonymous session.
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (digest, csrf, None if device_id else revision, now, now))
        if password_login or device_id:
            db.execute('INSERT INTO session_auth VALUES(?,?,?,?)',
                       (digest, now if password_login else None, revision if device_id else None, device_id))
        return token, csrf

    def allow(self, bucket, maximum):
        # Committed independently so a rejected login cannot roll back its count.
        with self.connection() as db:
            now = time.time()
            row = db.execute('SELECT * FROM rates WHERE bucket=?', (bucket,)).fetchone()
            if not row or now-row['started'] >= 300:
                db.execute('INSERT OR REPLACE INTO rates VALUES(?,?,1)', (bucket, now))
                return True
            if row['count'] >= maximum:
                return False
            db.execute('UPDATE rates SET count=count+1 WHERE bucket=?', (bucket,))
            return True


class ReadingAccess:
    def __init__(self, path, *, prefix, owner):
        self.path, self.prefix, self.owner = path, prefix, owner
        self._state = None

    @property
    def state(self):
        if self._state is None:
            self._state = AccessState(self.path)
        return self._state

    def authorize(self, environ):
        try:
            with self.state.connection() as db:
                password = self.state.password(db)
                session = self.state.session(db, token_from(environ))
                return self.authenticated(session, password)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def reply(self, start, status, body, mime='application/json; charset=utf-8', token=None,
              max_age=PASSWORD_SECONDS):
        payload = (json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body).encode()
        headers = [('Content-Type',mime), ('Content-Length',str(len(payload))), ('Cache-Control','no-store'),
            ('Vary','Cookie'), ('X-Content-Type-Options','nosniff'), ('Referrer-Policy','no-referrer'),
            ('X-Robots-Tag','noindex, nofollow, noarchive, nosnippet'),
            ('Content-Security-Policy',"default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")]
        if token is not None:
            headers.append(('Set-Cookie',f'{COOKIE}={token}; Path={self.prefix}/; Max-Age={max_age if token else 0}; Secure; HttpOnly; SameSite=Strict'))
        start(f'{status} {HTTPStatus(status).phrase}',headers)
        return [payload]

    def page(self, start, mode='login', status=200):
        document=(ASSETS/'access.html').read_text().replace('{{prefix}}',self.prefix).replace('{{mode}}',mode)
        return self.reply(start,status,document,'text/html; charset=utf-8')

    def challenge(self, start, unavailable=False):
        if unavailable:
            return self.reply(start,503,{'error':'access_unavailable'})
        return self.page(start,status=401)

    @staticmethod
    def body(environ, maximum=4096):
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value: raise ValueError('Duplicate key')
                value[key]=item
            return value
        try:
            size=int(environ.get('CONTENT_LENGTH','0'))
            if not 0 < size <= maximum or environ.get('CONTENT_TYPE') != 'application/json': raise ValueError()
            raw=environ['wsgi.input'].read(size)
            if len(raw)!=size: raise ValueError()
            value=json.loads(raw.decode('utf-8'),object_pairs_hook=unique)
            if not isinstance(value,dict): raise ValueError()
            return value
        except (ValueError,UnicodeError,RecursionError):
            raise Rejected('invalid_request') from None

    @staticmethod
    def csrf(session,environ):
        token=environ.get('HTTP_X_CSRF_TOKEN','')
        if not session or not TOKEN.fullmatch(token) or not compare_digest(session['csrf'],token):
            raise Rejected('csrf_rejected',403)

    @staticmethod
    def authenticated(session, password):
        return bool(password and session and session['revision'] == password['revision'])

    @classmethod
    def can_enroll(cls, session, password, now):
        return bool(cls.authenticated(session, password) and session['device_id'] is None
                    and session['password_at'] is not None
                    and 0 <= now - session['password_at'] < PASSWORD_ENROLLMENT_SECONDS)

    @staticmethod
    def device_metadata(device):
        return {'id': device['id'], 'name': device['name'], 'created_at': int(device['created_at']),
                'last_used_at': int(device['last_used_at']),
                'revoked_at': int(device['revoked_at']) if device['revoked_at'] is not None else None}

    def session_payload(self, db, session, password):
        authenticated = self.authenticated(session, password)
        available = passkeys.SERVER is not None
        device = self.state.active_device(db, session['device_id'], session['revision']) if authenticated and session['device_id'] else None
        return {'configured': bool(password), 'authenticated': authenticated, 'csrf_token': session['csrf'],
                'passkeys_available': available,
                'can_register_passkey': available and self.can_enroll(session, password, time.time()),
                'trusted_device': self.device_metadata(device) if device else None,
                'trusted_until': int(session['created'] + TRUST_SECONDS) if device else None}

    def owner_action(self, relative, value, environ):
        allowed = self.owner(environ)
        if allowed is False:
            raise Rejected('owner_required',403)
        if allowed is not True:
            raise Rejected('owner_unavailable',503)
        if relative == '/access/password':
            if not password_valid(value['password']):
                raise Rejected('invalid_password')
            if not self.state.allow('change',12):
                raise Rejected('rate_limited',429)
            salt = secrets.token_bytes(32)
            digest = password_hash(value['password'], salt)
            with self.state.connection() as db:
                db.execute('INSERT OR REPLACE INTO password VALUES(1,?,?,?)', (salt, digest, secrets.token_urlsafe(32)))
                db.execute('UPDATE passkey_devices SET revoked_at=? WHERE revoked_at IS NULL', (time.time(),))
                db.execute('DELETE FROM sessions')
            return {'changed': True}
        if relative == '/access/devices/list':
            with self.state.connection() as db:
                devices = db.execute('SELECT * FROM passkey_devices ORDER BY revoked_at IS NOT NULL, created_at DESC, id')
                return {'devices': [self.device_metadata(device) for device in devices]}
        device_id = value['id']
        if not isinstance(device_id, str) or not TOKEN.fullmatch(device_id):
            raise Rejected('invalid_request')
        with self.state.connection() as db:
            changed = db.execute('UPDATE passkey_devices SET revoked_at=coalesce(revoked_at,?) WHERE id=?', (time.time(), device_id))
            if not changed.rowcount:
                raise Rejected('device_not_found',404)
            db.execute('DELETE FROM sessions WHERE token IN (SELECT session_token FROM session_auth WHERE device_id=?)',
                       (device_id,))
        return {'revoked': True}

    @staticmethod
    def passkey_begin(db, purpose, session_token, revision, value, now):
        name = device_id = None
        if purpose == 'register':
            name = value['name'].strip() if isinstance(value['name'], str) else ''
            if not 1 <= len(name) <= 60 or not name.isprintable():
                raise Rejected('invalid_device_name')
            device_id = secrets.token_urlsafe(32)
            options, state = passkeys.registration_options(name, device_id)
        else:
            options, state = passkeys.authentication_options()
        db.execute('''INSERT OR REPLACE INTO passkey_challenges
                    (session_token,purpose,revision,state,expires_at,device_id,name) VALUES(?,?,?,?,?,?,?)''',
                   (session_token, purpose, revision, json.dumps(state), now + passkeys.CHALLENGE_SECONDS,
                    device_id, name))
        return options

    @staticmethod
    def passkey_finish(db, purpose, challenge, credential, now):
        state = json.loads(challenge['state'])
        revision = challenge['revision']
        if purpose == 'register':
            data = passkeys.verify_registration(state, credential)
            attested = data.credential_data
            if db.execute('SELECT 1 FROM passkey_devices WHERE credential_id=?', (attested.credential_id,)).fetchone():
                raise Rejected('credential_already_enrolled',409)
            db.execute('''INSERT INTO passkey_devices(id,name,revision,credential_id,public_key,
                       sign_count,backup_eligible,created_at,last_used_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                       (challenge['device_id'], challenge['name'], revision, attested.credential_id, bytes(attested),
                        data.counter, data.is_backup_eligible(), now, now))
            return challenge['device_id']

        assertion = passkeys.parse_assertion(credential)
        device = db.execute('''SELECT * FROM passkey_devices
                            WHERE credential_id=? AND revision=? AND revoked_at IS NULL''',
                            (assertion.raw_id, revision)).fetchone()
        if device is None:
            raise passkeys.InvalidPasskey()
        counter = passkeys.verify_assertion(state, assertion, device)
        db.execute('UPDATE passkey_devices SET last_used_at=?, sign_count=? WHERE id=?', (now, counter, device['id']))
        return device['id']

    def passkey_action(self, relative, value, environ):
        if passkeys.SERVER is None:
            raise Rejected('passkeys_unavailable',503)
        purpose, phase = relative.split('/')[-2:]
        token = token_from(environ)
        failure = None
        with self.state.connection() as db:
            now = time.time()
            session = self.state.session(db, token)
            self.csrf(session, environ)
            password = self.state.password(db)
            if not password:
                raise Rejected('not_configured',503)
            session_token = session['token']
            if phase == 'finish':
                # Consume on the first finish attempt, even when credential verification fails.
                challenge = db.execute('''DELETE FROM passkey_challenges WHERE session_token=? AND purpose=?
                                       RETURNING *''', (session_token, purpose)).fetchone()
            try:
                if purpose == 'register' and not self.can_enroll(session, password, now):
                    raise Rejected('recent_password_required',403)
                if purpose == 'login' and self.authenticated(session, password):
                    raise Rejected('already_authenticated',409)
                if phase == 'begin':
                    return self.passkey_begin(db, purpose, session_token, password['revision'], value, now), None
                if not challenge or challenge['expires_at'] <= now or challenge['revision'] != password['revision']:
                    raise Rejected('invalid_passkey_challenge')
                device_id = self.passkey_finish(db, purpose, challenge, value['credential'], now)
            except passkeys.InvalidPasskey:
                failure = Rejected('invalid_passkey',401)
            except Rejected as error:
                failure = error
            if failure is None:
                new_token, _ = self.state.new_session(db, password['revision'], token, device_id=device_id, now=now)
                session = self.state.session(db, new_token)
                return self.session_payload(db, session, password), new_token
        raise failure

    def handle(self,path,environ,start):
        method=environ.get('REQUEST_METHOD','GET')
        relative=path[len(self.prefix):]
        if relative=='/settings/' and method=='GET': return self.page(start,'settings')
        if not relative.startswith('/access/'): return None
        try:
            if method=='GET':
                if relative in ('/access/login','/access/logout'):
                    return self.page(start,relative.rsplit('/',1)[1])
                if relative in ('/access/access.js', '/access/access.css', '/access/palette.css', '/access/book.svg'):
                    name=relative.rsplit('/',1)[1]
                    mime = {'.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}
                    asset = ASSETS / name
                    return self.reply(start, 200, asset.read_text(), mime[asset.suffix] + '; charset=utf-8')
                if relative=='/access/session':
                    with self.state.connection() as db:
                        password=self.state.password(db)
                        session=self.state.session(db,token_from(environ))
                        token=None
                        if not session:
                            token,_=self.state.new_session(db)
                            session=self.state.session(db,token)
                        value=self.session_payload(db,session,password)
                    return self.reply(start,200,value,token=token,max_age=ANONYMOUS_SECONDS)
            if method!='POST' or relative not in POST_FIELDS:
                raise Rejected('not_found',404)
            value=self.body(environ, maximum=16384 if relative in PASSKEY_FIELDS and relative.endswith('/finish') else 4096)
            if set(value) != POST_FIELDS[relative]:
                raise Rejected('invalid_request')
            if relative in OWNER_FIELDS:
                return self.reply(start,200,self.owner_action(relative,value,environ))
            if relative in PASSKEY_FIELDS:
                payload,token=self.passkey_action(relative,value,environ)
                return self.reply(start,200,payload,token=token,max_age=TRUST_SECONDS)
            old_token=token_from(environ)
            if relative=='/access/logout':
                with self.state.connection() as db:
                    self.csrf(self.state.session(db,old_token),environ)
                    db.execute('DELETE FROM sessions WHERE token=?',(token_digest(old_token),))
                return self.reply(start,200,{'authenticated':False},token='')
            with self.state.connection() as db:
                self.csrf(self.state.session(db,old_token),environ)
            if not self.state.allow('login',10): raise Rejected('rate_limited',429)
            with self.state.connection() as db:
                # Recheck CSRF while locked: a simultaneous password reset revokes it.
                self.csrf(self.state.session(db,old_token),environ)
                password=self.state.password(db)
                if not password: raise Rejected('not_configured',503)
                if not password_valid(value['password']) or not compare_digest(password_hash(value['password'],password['salt']),password['digest']):
                    raise Rejected('invalid_login',401)
                token,_=self.state.new_session(db,password['revision'],old_token,password_login=True)
                value=self.session_payload(db,self.state.session(db,token),password)
            return self.reply(start,200,value,token=token)
        except Rejected as error:
            return self.reply(start,error.status,{'error':error.code})
        except (OSError,sqlite3.Error,ValueError):
            return self.reply(start,503,{'error':'access_unavailable'})
