"""Build a synthetic briefing through the actual v1.1 renderer and hosted prefix."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from wsgiref.util import setup_testing_defaults

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from knowledge_hub.access import ReadingAccess
from knowledge_hub.hosting import MountedApplication
from knowledge_hub.reader_views import home


class EmptyCatalog:
    def get_sources(self):
        return {}

    def continuations(self, **kwargs):
        return {'rows': []}

    def list_sources(self, **kwargs):
        return []


def card(index, day):
    return {
        'item': {'id': index, 'title': f'Paper {index}', 'source_name': 'arxiv', 'key': f'arxiv:2611.{index:05}'},
        'version': {'id': index + 100, 'published_at': day, 'scope': 'full', 'label': 'v1',
                    'source_url': f'https://arxiv.org/abs/2611.{index:05}v1'},
        'category': '推論系統',
        'brief': {'l1': {'change': 'Change', 'relevance': 'Relevance'},
                  'l2': {'mechanism': 'Method', 'metrics': 'Result', 'comparison': 'Baseline', 'limits': 'Limit'},
                  'metrics_source': {'version_id': str(index + 100), 'anchor': 'S4',
                                     'href': f'/items/{index}?version={index+100}#S4'}},
    }


def render(day):
    cards = [card(i, day) for i in range(1, 6)]
    views = EmptyCatalog()
    payload = home(views, views, views, 'PRIVATE_CSRF', picks=cards, selected_day=day, today=day)
    document = payload.encode()
    def app(environ, start):
        start('200 OK', [('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(document)))])
        return [document]
    mounted = MountedApplication(app, origin='https://momonong.me', prefix='/knowledge', authorize=lambda _: True)
    environ = {}
    setup_testing_defaults(environ)
    environ.update(PATH_INFO='/knowledge/', HTTP_HOST='momonong.me', REMOTE_ADDR='127.0.0.1', HTTP_X_FORWARDED_PROTO='https')
    return b''.join(mounted(environ, lambda *_: None)).decode()


if __name__ == '__main__':
    day = '2026-11-26'
    with TemporaryDirectory() as temp:
        access = ReadingAccess(Path(temp) / 'access.sqlite3', prefix='/knowledge', owner=lambda _: True)
        challenge = b''.join(access.challenge(lambda *_: None)).decode()
    static = ROOT / 'knowledge_hub' / 'static'
    print(json.dumps({'complete': render(day),
                      'login': challenge, 'accessScript': (static / 'access.js').read_text(),
                      'accessCss': (static / 'access.css').read_text(), 'palette': (static / 'palette.css').read_text(),
                      'book': (static / 'book.svg').read_text(),
                      'readerCss': (static / 'reader.css').read_text(), 'briefingCss': (static / 'briefing.css').read_text()},
                     ensure_ascii=False))
