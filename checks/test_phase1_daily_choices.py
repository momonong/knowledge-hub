import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from knowledge_hub.daily import DailyRefresh
from knowledge_hub.store import KnowledgeStore
from test_study import entry


class DailyChoices(unittest.TestCase):
    def test_public_article_never_uses_a_private_attachment_for_daily_cards(self):
        import json
        from datetime import datetime, timedelta, timezone
        from knowledge_hub import briefs, discovery
        from knowledge_hub.library import KnowledgeLibrary
        from knowledge_hub.web import create_app
        from test_initial_library import seed
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'daily.sqlite3'
            store = KnowledgeStore(path)
            day = DailyRefresh(store).day()
            public = entry('arxiv:2611.60001','Agent memory retrieval','We propose an agent memory retrieval benchmark.')
            item, published, _ = store.upsert_parsed_entry({**public,'published_at':day})
            library = KnowledgeLibrary(store)
            catalog, graph, starter = seed()
            catalog['sources'][0].update(primary_url=public['source_url'],year=day)
            library.import_seed(catalog,graph,starter)
            secret = 'PRIVATE_ATTACHMENT'
            uploaded = library.add_document('sample-rag',secret.encode(),media_type='text/plain',
                source_url=public['source_url'][:-1]+'9',version_label='v9',
                blocks=[{'anchor':'p1','text':secret,'source_url':public['source_url'][:-1]+'9'}])
            stamp = datetime.now(timezone.utc)
            pool = discovery._pool(store,stamp-timedelta(days=2),stamp+timedelta(days=2))
            self.assertEqual([p['version']['id'] for p in pool],[published])
            # A persisted pre-fix card must also disappear before rendering or repair.
            private_pick = {'item':{'id':item},'version':{'id':uploaded['version_id']},'brief':{'state':'degraded'}}
            with store._connect() as conn:
                conn.execute('INSERT INTO daily_cards VALUES (?,?,?,?,?)',
                             (day,1,item,uploaded['version_id'],json.dumps(private_pick)))
            self.assertFalse(DailyRefresh(store).selection(day))
            sent = []
            def generate(pick):
                sent.append(pick['version']['blocks'][0]['text'])
                return briefs._local_brief(pick)
            app = create_app(path,brief_generator=generate)
            with patch('knowledge_hub.feeds.collect'):
                app.refresh_daily_sources(prefetch=False,force=True)
            self.assertTrue(sent)
            self.assertNotIn(secret,' '.join(sent))
            self.assertEqual([p['version']['id'] for p in DailyRefresh(store).selection(day)],[published])

    def test_rerunning_a_full_edition_preserves_its_versions_choices_and_cached_briefs(self):
        from knowledge_hub.briefs import FORMAT_VERSION
        from knowledge_hub.web import create_app
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'daily.sqlite3'
            store = KnowledgeStore(path)
            daily = DailyRefresh(store)
            cards = []
            for i in range(5):
                item, version, _ = store.upsert_parsed_entry(entry(f'arxiv:2601.0001{i}', f'Memory {i}', 'This method retrieves data.'))
                cards.append({'item':{'id':item}, 'version':{'id':version}, 'brief_format':FORMAT_VERSION,
                              'brief':{'state':'ready'}})
            daily.run(lambda: cards)
            daily.choose(daily.day(), item, version, 'skip')
            app = create_app(path, brief_generator=lambda p: self.fail('already cached'))
            with patch('knowledge_hub.feeds.collect'), patch('knowledge_hub.discovery.rank',side_effect=AssertionError('already published')):
                app.refresh_daily_sources(prefetch=False,force=True)
            current = daily.selection(daily.day())
            self.assertEqual([p['version']['id'] for p in current], [p['version']['id'] for p in cards])
            self.assertEqual(current[-1]['choice'], 'skip')

    def test_choices_are_dated_and_bound_to_a_published_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'daily.sqlite3'
            store = KnowledgeStore(path)
            item, version, _ = store.upsert_parsed_entry(entry('arxiv:2601.00011', 'Memory', 'The method compares two systems.'))
            daily = DailyRefresh(store)
            day = '2026-11-21'
            card = {'item':{'id':item}, 'version':{'id':version}, 'brief':{'state':'ready'}}
            daily.run(lambda: [card], day=day)
            for invalid in (('2026-11-20',version,'skip'), (day,'999','later'), (day,version,'read')):
                with self.assertRaises(ValueError):
                    daily.choose(invalid[0], item, invalid[1], invalid[2])
            daily.choose(day, item, version, 'skip')
            self.assertEqual(DailyRefresh(KnowledgeStore(path)).selection(day)[0]['choice'], 'skip')
            self.assertEqual(store.get_item(item)['status'], 'unread')
            for action in ('later','later','undo','undo'):
                daily.choose(day,item,version,action)
            self.assertEqual(store.get_item(item)['status'], 'unread')
            daily.choose(day, item, version, 'later')
            store.set_item_status(item, 'read')
            daily.choose(day, item, version, 'undo')
            self.assertEqual(store.get_item(item)['status'], 'read')
            daily.choose(day, item, version, 'later')
            store.set_item_status(item, 'read')
            store.set_item_status(item, 'later')
            daily.choose(day, item, version, 'undo')
            self.assertEqual(store.get_item(item)['status'], 'later')
