"""The reading room presents private recall on its initial, authenticated GET."""
from html.parser import HTMLParser
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from wsgiref.util import setup_testing_defaults

from checks.test_study import entry
from knowledge_hub.hosting import MountedApplication
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app


class Page(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.tags = []
        self.scripts = []
        self.in_script = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == 'script':
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)


class RoomConnections(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'room.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.item_id, self.version, _ = self.store.upsert_parsed_entry(
            entry('arxiv:2601.00001', 'A reader and a cache', 'A public article about cache admission.')
        )
        self.note = self.store.add_note(self.item_id, self.version, 'p1', 'Previously saved note.')
        recall = ModuleType('knowledge_hub.knowledge_recall')
        recall.recommend = Mock()
        self.recommend = recall.recommend
        patcher = patch.dict(sys.modules, {recall.__name__: recall})
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, *, allow=True):
        site = MountedApplication(create_app(self.db), origin='https://fixture.example', prefix='/knowledge',
                                  authorize=lambda _: allow)
        env = {}
        setup_testing_defaults(env)
        env.update(HTTP_HOST='fixture.example', HTTP_X_FORWARDED_PROTO='https', REMOTE_ADDR='127.0.0.1',
                   PATH_INFO=f'/knowledge/items/{self.item_id}', QUERY_STRING=f'version={self.version}')
        statuses = []
        raw = b''.join(site(env, lambda status, _: statuses.append(status)))
        return int(statuses[0].split()[0]), raw.decode()

    def test_related_knowledge_and_native_disclosure_are_rendered_on_the_first_mounted_get(self):
        href = f'/items/{self.item_id}?version={self.version}#note-{self.note["id"]}'
        self.recommend.return_value = [
            {'kind': 'note', 'title': '先前的快取筆記', 'reason': '這篇的入場條件呼應你對快取容量的疑問。',
             'excerpt': 'PRIVATE_EXACT_NOTE', 'href': href},
            {'kind':'starter', 'title':'Seed article', 'reason':'這篇可以和先前學過的替換策略對照。',
             'excerpt':'A public source passage.', 'href':'/library/seed-article'},
        ]
        status, raw = self.request()
        self.assertEqual(status, 200)
        self.recommend.assert_called_once()
        self.assertEqual(str(self.recommend.call_args.args[2]['id']), self.version)
        self.assertLess(raw.index('這篇連到你庫裡的舊知'), raw.index('data-layer-guide'))
        self.assertIn('這篇的入場條件呼應你對快取容量的疑問。', raw)
        doc = Page(raw)
        disclosure = [attrs for tag, attrs in doc.tags if tag == 'details' and 'data-connections-disclosure' in attrs]
        self.assertEqual(len(disclosure), 1)
        self.assertNotIn('open', disclosure[0])
        _, _, content = raw.partition('<details data-connections-disclosure>')
        summary, _, cards = content.partition('</summary>')
        self.assertIn('這篇連到你庫裡的舊知', summary)
        self.assertIn('2 筆', summary)
        self.assertNotIn('data-connection-reason', summary)
        self.assertIn('data-connection-reason', cards)
        card = [attrs for tag, attrs in doc.tags if tag == 'details' and 'data-knowledge-kind' in attrs]
        self.assertEqual(len(card), 2)
        self.assertTrue(all('open' not in attrs for attrs in card))
        self.assertTrue(any('data-knowledge-connections' in attrs and 'data-private' in attrs
                            for _, attrs in doc.tags))
        destinations = {attrs.get('href') for tag, attrs in doc.tags
                        if tag == 'a' and 'data-connection-destination' in attrs}
        self.assertIn('/knowledge' + href, destinations)
        self.assertIn('/knowledge/library/seed-article', destinations)
        self.assertTrue(any(attrs.get('id') == f'note-{self.note["id"]}' for _, attrs in doc.tags))
        self.assertIn('PRIVATE_EXACT_NOTE', raw)
        self.assertNotIn('PRIVATE_EXACT_NOTE', ''.join(doc.scripts))

    def test_untrusted_values_are_escaped_and_noncanonical_destinations_are_not_rendered(self):
        dangerous = '<img src=x onerror=alert(1)>'
        safe = {'kind':'read', 'title':dangerous, 'reason':dangerous, 'excerpt':dangerous,
                'href':f'/items/{self.item_id}?version={self.version}#p1'}
        self.recommend.return_value = [
            safe,
            {**safe, 'title':'Offsite', 'href':'//evil.example/private'},
            {**safe, 'title':'Backslash', 'href':'/\\evil.example/private'},
            {**safe, 'title':'Traversal', 'href':'/library/..'},
            {**safe, 'title':'Extra data', 'href':f'/items/{self.item_id}?version={self.version}&secret=leak'},
            {**safe, 'title':'Versionless', 'href':f'/items/{self.item_id}#p1'},
        ]
        status, raw = self.request()
        self.assertEqual(status, 200)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', raw)
        self.assertNotIn(dangerous, raw)
        self.assertIn('1 筆', raw)
        self.assertTrue(all(title not in raw for title in ('Offsite', 'Backslash', 'Traversal', 'Extra data', 'Versionless')))

    def test_empty_result_has_an_honest_empty_state_and_unauthenticated_get_never_ranks(self):
        self.recommend.return_value = []
        status, raw = self.request(allow=False)
        self.assertEqual(status, 401)
        self.recommend.assert_not_called()
        self.assertNotIn('這篇連到你庫裡的舊知', raw)
        status, raw = self.request()
        self.assertEqual(status, 200)
        self.assertIn('目前還沒有找到可直接對照的庫內紀錄', raw)
        self.assertFalse(any('data-connections-disclosure' in attrs for _, attrs in Page(raw).tags))
        self.assertFalse(any('data-knowledge-kind' in attrs for _, attrs in Page(raw).tags))
