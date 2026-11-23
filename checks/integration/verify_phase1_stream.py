"""Exercise default deadlines against a loopback OpenAI-compatible stream and a copy of a real edition."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub.followup import Followups
from knowledge_hub.model_stream import ModelConfig
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app


def controlled_provider(stop):
    first_seen = Event()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            evidence = json.loads(payload['messages'][-1]['content'])['evidence'][0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def write(event):
                self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                self.wfile.flush()

            scenario = payload['model']
            if scenario == 'first_then_invalid':
                scenario = 'invalid' if first_seen.is_set() else 'first'
                first_seen.set()
            if scenario == 'first':
                write({'choices': [{'delta': {'role': 'assistant'}}]})
                stop.wait(50)
                return
            for kind in ('evidence', 'limits', 'inference'):
                text = '引文中的作者描述為「' + evidence['quote'] + '」。' if kind == 'evidence' else '原文未交代'
                ids = [evidence['id']] if kind == 'evidence' else []
                if scenario == 'invalid':
                    ids = ['foreign-edition-id']
                line = json.dumps(dict(kind=kind, text=text, ids=ids), ensure_ascii=False) + '\n'
                write({'choices': [{'delta': {'content': line}}]})
                if scenario in ('total', 'invalid'):
                    stop.wait(50)
                    return
            write({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()

    return Provider


def verify(db, item_id, version_id, cases=('local', 'success', 'invalid', 'first', 'total')):
    stop = Event()
    with TemporaryDirectory() as tmp, ThreadingHTTPServer(('127.0.0.1', 0), controlled_provider(stop)) as server:
        scratch = Path(tmp) / 'acceptance.sqlite3'
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(scratch)) as dst:
                src.backup(dst)
        store = KnowledgeStore(scratch)
        token = store.get_csrf_token()
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/v1'
        apps = {}
        for case in cases:
            routes = {} if case == 'local' else {'primary': ModelConfig(url, case), 'fallback': ModelConfig(url, 'success')}
            apps[case] = create_app(scratch, model_routes=routes)

        def exercise(case):
            data = urlencode(dict(csrf=token, question='RecordFolder 如何組合記錄？', route='primary',
                                  request_id=secrets.token_urlsafe(24), version_id=version_id)).encode()
            env = {}
            setup_testing_defaults(env)
            env.update(HTTP_HOST='localhost', HTTP_ORIGIN='http://localhost', REQUEST_METHOD='POST',
                       CONTENT_TYPE='application/x-www-form-urlencoded', CONTENT_LENGTH=str(len(data)),
                       PATH_INFO=f'/items/{item_id}/followup')
            env['wsgi.input'] = io.BytesIO(data)
            started = monotonic()
            events, sections, first = [], [], None
            for chunk in apps[case](env, lambda status, headers: None):
                event_line, data_line = chunk.decode().strip().splitlines()
                event = event_line.removeprefix('event: ')
                value = json.loads(data_line.removeprefix('data: '))
                events.append(event)
                if event == 'section':
                    if first is None:
                        first = round(monotonic() - started, 3)
                    sections.append(value)
            elapsed = round(monotonic() - started, 3)
            row = Followups(store).get(value['turn_id'])
            expected = {'first': 'first_token_timeout', 'total': 'total_timeout', 'invalid': 'invalid_citation'}
            if case in expected:
                assert events[-1] == 'error' and row['state'] == 'failed' and row['reason'] == expected[case], (events, row['reason'])
            else:
                assert events == ['meta', 'section', 'section', 'section', 'done'] and row['state'] == 'completed', (case, events, row['reason'])
                assert sections[0]['citations'] and first < 5, (case, len(sections[0]['citations']), first)
            if case == 'first':
                assert first is None and 14.8 <= elapsed <= 16
            if case == 'total':
                assert first < 5 and 44.8 <= elapsed <= 46
            if case == 'invalid':
                assert not sections
            return dict(case=case, mode=row['mode'], events=events, seconds=elapsed, first_section_seconds=first,
                        state=row['state'], reason=row['reason'], citations=[
                            citation['id'] for part in sections for citation in part['citations']])
        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(exercise, apps))
        finally:
            stop.set()
            server.shutdown()
            thread.join()
    return dict(item_id=item_id, version_id=version_id, cases=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--item', type=int, required=True)
    parser.add_argument('--version', type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.db, args.item, args.version), ensure_ascii=False, indent=2))
