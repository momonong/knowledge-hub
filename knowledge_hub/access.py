"""Independent password-only KH access; Home is used only to manage the password.

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

COOKIE = '__Secure-momonong-knowledge'
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
ASSETS = Path(__file__).with_name('static')


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
            db.execute('CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, csrf TEXT NOT NULL, revision TEXT, created REAL NOT NULL, seen REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS rates(bucket TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL)')

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
        digest = sha256(token.encode()).hexdigest()
        row = db.execute('SELECT * FROM sessions WHERE token=?', (digest,)).fetchone()
        if not row:
            return None
        idle, absolute = (1800, 28800) if row['revision'] else (900, 900)
        if now-row['seen'] >= idle or now-row['created'] >= absolute:
            db.execute('DELETE FROM sessions WHERE token=?', (digest,))
            return None
        db.execute('UPDATE sessions SET seen=? WHERE token=?', (now, digest))
        return row

    @staticmethod
    def new_session(db, revision=None, revoke=''):
        now = time.time()
        db.execute('DELETE FROM sessions WHERE token=? OR seen < ? OR created < ?',
                   (sha256(revoke.encode()).hexdigest(), now-1800, now-28800))
        # Anonymous traffic cannot evict an authenticated reading session.
        db.execute('DELETE FROM sessions WHERE revision IS NULL AND token IN (SELECT token FROM sessions WHERE revision IS NULL ORDER BY created LIMIT MAX(0,(SELECT COUNT(*) FROM sessions WHERE revision IS NULL)-255))')
        if revision:
            db.execute('DELETE FROM sessions WHERE revision IS NOT NULL AND token IN (SELECT token FROM sessions WHERE revision IS NOT NULL ORDER BY created LIMIT MAX(0,(SELECT COUNT(*) FROM sessions WHERE revision IS NOT NULL)-1023))')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sha256(token.encode()).hexdigest(), csrf, revision, now, now))
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
                return bool(password and session and session['revision'] == password['revision'])
        except (OSError, sqlite3.Error, ValueError):
            return None

    def reply(self, start, status, body, mime='application/json; charset=utf-8', token=None):
        payload = (json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body).encode()
        headers = [('Content-Type',mime), ('Content-Length',str(len(payload))), ('Cache-Control','no-store'),
            ('Vary','Cookie'), ('X-Content-Type-Options','nosniff'), ('Referrer-Policy','no-referrer'),
            ('X-Robots-Tag','noindex, nofollow, noarchive, nosnippet'),
            ('Content-Security-Policy',"default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")]
        if token is not None:
            headers.append(('Set-Cookie',f'{COOKIE}={token}; Path={self.prefix}/; Max-Age={28800 if token else 0}; Secure; HttpOnly; SameSite=Strict'))
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
    def body(environ):
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value: raise ValueError('Duplicate key')
                value[key]=item
            return value
        try:
            size=int(environ.get('CONTENT_LENGTH','0'))
            if not 0 < size <= 4096 or environ.get('CONTENT_TYPE') != 'application/json': raise ValueError()
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

    def handle(self,path,environ,start):
        method=environ.get('REQUEST_METHOD','GET')
        relative=path[len(self.prefix):]
        if relative=='/settings/' and method=='GET': return self.page(start,'settings')
        if not relative.startswith('/access/'): return None
        try:
            if method=='GET':
                if relative in ('/access/login','/access/logout'):
                    return self.page(start,relative.rsplit('/',1)[1])
                if relative in ('/access/access.js','/access/access.css'):
                    name=relative.rsplit('/',1)[1]
                    return self.reply(start,200,(ASSETS/name).read_text(),
                        'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/css; charset=utf-8')
                if relative=='/access/session':
                    with self.state.connection() as db:
                        password=self.state.password(db)
                        session=self.state.session(db,token_from(environ))
                        token=None
                        if not session:
                            token,csrf=self.state.new_session(db)
                            session={'csrf':csrf,'revision':None}
                        value={'configured':bool(password),'authenticated':bool(password and session['revision']==password['revision']),
                               'csrf_token':session['csrf']}
                    return self.reply(start,200,value,token=token)
            if method!='POST' or relative not in ('/access/login','/access/logout','/access/password'):
                raise Rejected('not_found',404)
            value=self.body(environ)
            if set(value) != (set() if relative=='/access/logout' else {'password'}): raise Rejected('invalid_request')
            if relative=='/access/password':
                allowed=self.owner(environ)
                if allowed is not True: raise Rejected('owner_required' if allowed is False else 'owner_unavailable',403 if allowed is False else 503)
                if not password_valid(value['password']): raise Rejected('invalid_password')
                if not self.state.allow('change',12): raise Rejected('rate_limited',429)
                salt=secrets.token_bytes(32)
                digest=password_hash(value['password'],salt)
                with self.state.connection() as db:
                    db.execute('INSERT OR REPLACE INTO password VALUES(1,?,?,?)',(salt,digest,secrets.token_urlsafe(32)))
                    db.execute('DELETE FROM sessions')
                return self.reply(start,200,{'changed':True})
            with self.state.connection() as db:
                self.csrf(self.state.session(db,token_from(environ)),environ)
            if relative=='/access/logout':
                with self.state.connection() as db:
                    db.execute('DELETE FROM sessions WHERE token=?',(sha256(token_from(environ).encode()).hexdigest(),))
                return self.reply(start,200,{'authenticated':False},token='')
            if not self.state.allow('login',10): raise Rejected('rate_limited',429)
            with self.state.connection() as db:
                # Recheck CSRF while locked: a simultaneous password reset revokes it.
                self.csrf(self.state.session(db,token_from(environ)),environ)
                password=self.state.password(db)
                if not password: raise Rejected('not_configured',503)
                if not password_valid(value['password']) or not compare_digest(password_hash(value['password'],password['salt']),password['digest']):
                    raise Rejected('invalid_login',401)
                token,csrf=self.state.new_session(db,password['revision'],token_from(environ))
            return self.reply(start,200,{'authenticated':True,'csrf_token':csrf},token=token)
        except Rejected as error:
            return self.reply(start,error.status,{'error':error.code})
        except (OSError,sqlite3.Error,ValueError):
            return self.reply(start,503,{'error':'access_unavailable'})
