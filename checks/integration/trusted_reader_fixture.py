"""Loopback HTTPS with the actual KH access gate, a disposable reader, and a test-only Home owner."""
from datetime import datetime, timezone
from hashlib import sha256
from http.cookies import SimpleCookie
from pathlib import Path
import json
import os
import secrets
import ssl
import sys
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

from knowledge_hub import passkeys
from knowledge_hub.access import ReadingAccess, password_hash
from knowledge_hub.hosting import MountedApplication
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app


TEST_HOST = 'reader.test'
TEST_ORIGIN = 'https://' + TEST_HOST


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


@patch.multiple(passkeys, RP_ID=TEST_HOST, ORIGINS=(TEST_ORIGIN,))
def main():
    work = Path(sys.argv[1])
    os.umask(0o077)
    reader, owner, owner_csrf = (secrets.token_urlsafe(32) for _ in range(3))
    def owner_session(environ):
        return SimpleCookie(environ.get('HTTP_COOKIE', '')).get('kh_fixture_owner')
    def owner_authorized(environ):
        cookie = owner_session(environ)
        return bool(cookie and cookie.value == owner and environ.get('HTTP_X_CSRF_TOKEN') == owner_csrf)

    access = ReadingAccess(work/'access.sqlite3', prefix='/knowledge', owner=owner_authorized)
    with access.state.connection() as db:
        salt = secrets.token_bytes(32)
        db.execute('INSERT INTO password VALUES (1,?,?,?)', (salt, password_hash(reader, salt), secrets.token_urlsafe(32)))
    db_path = work/'reader.sqlite3'
    store = KnowledgeStore(db_path)
    source = 'https://example.org/robotics'
    sentence = 'We evaluate a robotic manipulation policy trained with offline reinforcement learning.'
    item, version, _ = store.upsert_parsed_entry(dict(
        key='fixture:robot-policy', source_name='manual', title='Robot manipulation with offline reinforcement learning',
        label='v1', scope='abstract', source_url=source, published_at=datetime.now(timezone.utc).isoformat(),
        blocks=[dict(anchor='abstract', kind='paragraph', text=sentence, source_url=source)],
        content_hash=sha256(sentence.encode()).hexdigest(),
    ))
    def offline(_):
        raise OSError('Loopback fixture uses only its seeded article')
    site = create_app(db_path, fetch=offline)
    mounted = MountedApplication(site, prefix='/knowledge', origin=TEST_ORIGIN, authorize=access.authorize, access=access)
    def app(environ, start_response):
        def start(status, headers, exc_info=None):
            return start_response(status, [*headers, ('X-KH-Test-Fixture', 'trusted-reader-loopback')], exc_info)
        environ['HTTP_X_FORWARDED_PROTO'] = 'https'
        if environ.get('PATH_INFO') == '/home-api/session':
            cookie = owner_session(environ)
            signed_in = bool(cookie and cookie.value == owner)
            body = json.dumps({'authenticated': signed_in, 'csrf_token': owner_csrf if signed_in else ''}).encode()
            start('200 OK', [('Content-Type','application/json'), ('Cache-Control','no-store')])
            return [body]
        return mounted(environ, start)
    # Only this fixture process uses the reserved RP. The normal verifier still checks every signature.
    with patch.object(passkeys, 'SERVER', passkeys._server()), make_server('127.0.0.1', 0, app, handler_class=Quiet) as server:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(work/'tls.crt',work/'tls.key')
        server.socket = tls.wrap_socket(server.socket,server_side=True)
        (work/'fixture.json').write_text(json.dumps({'reader':reader, 'owner':owner,'port':server.server_port,
            'deepLink':f'/knowledge/items/{item}?version={version}#reader-start'}))
        print('ready', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
