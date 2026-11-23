"""Private loopback host used by verify_phase1_browser.cjs. Run in a temporary directory."""
import argparse
from contextlib import closing, contextmanager, nullcontext
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import ssl
from threading import Event, Thread
from wsgiref.simple_server import make_server

from knowledge_hub.__main__ import PrivateRequestHandler, ReadingServer
from knowledge_hub.access import ReadingAccess, password_hash
from knowledge_hub.hosting import MountedApplication
from knowledge_hub.model_stream import ModelConfig
from knowledge_hub.web import create_app
from .verify_phase1_stream import controlled_provider


@contextmanager
def test_routes():
    stop = Event()
    with ThreadingHTTPServer(('127.0.0.1',0),controlled_provider(stop)) as provider:
        thread = Thread(target=provider.serve_forever,daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{provider.server_port}/v1'
        try:
            yield {'primary':ModelConfig(url,'first_then_invalid'),'fallback':ModelConfig(url,'success')}
        finally:
            stop.set()
            provider.shutdown()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('work', type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--test-provider',action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    db = args.work / 'knowledge.sqlite3'
    if not db.exists():
        with closing(sqlite3.connect(args.source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(db)) as dst:
                src.backup(dst)
        shutil.copytree(args.source.parent / 'article-editions', args.work / 'article-editions')
    access = ReadingAccess(args.work / 'access.sqlite3', prefix='/knowledge', owner=lambda env: False)
    with access.state.connection() as conn:
        if not access.state.password(conn):
            password = secrets.token_urlsafe(32)
            salt = secrets.token_bytes(32)
            conn.execute('INSERT INTO password VALUES(1,?,?,?)',
                         (salt, password_hash(password, salt), secrets.token_urlsafe(32)))
            (args.work / 'browser-credential').write_text(password)
    with test_routes() if args.test_provider else nullcontext({}) as routes:
        app = create_app(db,public_projects=True,model_routes=routes)
        mounted = MountedApplication(app, origin='https://kh-phase1.test', prefix='/knowledge',
                                     authorize=access.authorize, access=access)

        def proxy(environ, start):
            environ['HTTP_X_FORWARDED_PROTO'] = 'https'
            return mounted(environ, start)

        with make_server('127.0.0.1', args.port, proxy, server_class=ReadingServer, handler_class=PrivateRequestHandler) as server:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(args.work / 'local.crt', args.work / 'local.key')
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            print(server.server_port, flush=True)
            server.serve_forever()


if __name__ == '__main__':
    main()
