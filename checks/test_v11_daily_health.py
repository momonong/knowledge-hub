"""A collection receipt reflects the visible briefs and live source states."""
from datetime import date, timedelta
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub import briefs
from knowledge_hub.daily import DailyRefresh
from knowledge_hub.sources import parse_arxiv_feed
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from test_v11_daily_upgrade import ABSTRACT, FULL, HTML, RESULT, tier


class DailyHealth(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'reading.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.daily = DailyRefresh(self.store)
        self.day = self.daily.day()
        for source in ('pytorch','arxiv','vllm'):
            self.store.update_source_state(source,'updated')

    def publish(self, *, count=5, degraded=0, day=None):
        picks = []
        day = day or self.day
        for i in range(count):
            url = ABSTRACT if i == 0 else f'https://arxiv.org/abs/2611.{7810+i}v1'
            title = f'Agent memory retrieval {i}'
            text = 'We propose a retrieval strategy to rerank candidates for long-term memory.'
            raw = (f'<feed><entry><id>{url}</id><title>{title}</title><summary>{text}</summary>'
                   f'<published>{day}T00:00:00Z</published></entry></feed>').encode()
            paper, = parse_arxiv_feed(raw)
            item_id,version_id,_ = self.store.upsert_parsed_entry(paper)
            picks.append({'item':{'id':item_id,'key':paper['key'],'title':title,'source_name':'arxiv'},
                          'version':{**paper,'id':version_id},'category':'AI Agents、記憶與檢索',
                          'spectrum_reason':'檢索候選如何重排。',
                          'brief':{**tier(),'state':'degraded' if i<degraded else 'ready',
                                   'mode':'abstract_fallback' if i<degraded else 'model'},
                          'brief_format':briefs.FORMAT_VERSION})
        self.daily.run(lambda: picks,day=day)
        return picks

    def fetch_html(self, url):
        if url == HTML:
            return FULL
        self.fail(f'Unexpected public fixture URL {url}')

    def test_normal_refresh_promotes_old_degraded_cards_after_all_model_overlays_are_ready(self):
        self.publish(degraded=5)
        before = self.daily.status()
        self.assertEqual(before['state'],'partial')
        app = create_app(self.db,fetch=self.fetch_html,brief_generator=lambda _: tier(RESULT))
        with patch('knowledge_hub.feeds.collect') as collect:
            final = app.refresh_daily_sources(prefetch=False)
        collect.assert_not_called()
        self.assertEqual(final['state'],'completed')
        self.assertEqual(self.daily.status(),final)
        self.assertEqual((final['started_at'],final['finished_at']),(before['started_at'],before['finished_at']))
        self.assertTrue(all(pick['brief']['state']=='degraded' for pick in self.daily.selection(self.day,resolved=False)))
        self.assertTrue(all(pick['brief']['state']=='ready' for pick in self.daily.selection(self.day)))
        collected = self.daily.run(lambda: self.daily.selection(self.day,resolved=False),force=True)
        self.assertEqual(collected['state'],'completed')

    def test_fetch_recovery_promotes_current_receipt_using_the_same_day_cards(self):
        first,*_ = self.publish(degraded=1)
        before = self.daily.status()
        app = create_app(self.db,fetch=self.fetch_html,brief_generator=lambda _: tier(RESULT))
        encoded = urlencode({'version_id':first['version']['id'],'csrf':self.store.get_csrf_token()}).encode()
        environ = {}
        setup_testing_defaults(environ)
        environ.update(REQUEST_METHOD='POST',HTTP_HOST='localhost',HTTP_ORIGIN='http://localhost',
                       PATH_INFO=f'/items/{first["item"]["id"]}/fetch',
                       CONTENT_TYPE='application/x-www-form-urlencoded',CONTENT_LENGTH=str(len(encoded)))
        environ['wsgi.input'] = io.BytesIO(encoded)
        response = []
        b''.join(app(environ,lambda state,headers: response.append(state)))
        self.assertEqual(response,['303 See Other'])
        updated = self.daily.status()
        self.assertEqual(updated['state'],'completed')
        self.assertEqual(updated['started_at'],before['started_at'])
        selected = self.daily.selection(self.day)[0]
        self.assertEqual(selected['version']['source_url'],HTML)
        self.assertEqual(selected['brief']['l2']['metrics'],RESULT)

    def test_reconciliation_leaves_running_failure_and_historical_receipts_untouched(self):
        self.publish()
        yesterday = (date.fromisoformat(self.day)-timedelta(days=1)).isoformat()
        self.publish(day=yesterday)
        for state,error,day in (('running','',self.day),('failed','暫時中止',self.day),
                                ('partial','仍有錯誤。',self.day),('partial','',yesterday)):
            with self.subTest(state=state,error=error,day=day):
                expected_started = '2026-11-23T07:00:00Z'
                with self.store._connect() as conn:
                    conn.execute('UPDATE daily_refresh SET state=?,error=?,started_at=? WHERE day=?',
                                 (state,error,expected_started,day))
                result = self.daily.reconcile_health(day)
                self.assertEqual((result['state'],result['error'],result['started_at']),(state,error,expected_started))

    def test_earlier_brief_update_does_not_finish_a_new_source_run(self):
        self.publish(degraded=5)
        competing_started = '2026-11-23T07:01:00Z'
        calls = []
        def generate(pick):
            if not calls:
                with self.store._connect() as conn:
                    conn.execute("""UPDATE daily_refresh SET state='running',started_at=?,finished_at=NULL
                                    WHERE day=?""",(competing_started,self.day))
            calls.append(pick['item']['id'])
            return tier(RESULT)
        app = create_app(self.db,fetch=self.fetch_html,brief_generator=generate)
        current = app.refresh_daily_sources(prefetch=False)
        self.assertEqual(len(calls),5)
        self.assertTrue(all(card['brief']['state']=='ready' for card in self.daily.selection(self.day)))
        self.assertEqual((current['state'],current['started_at'],current['finished_at']),
                         ('running',competing_started,None))
        self.assertEqual(self.daily.status(),current)

    def test_failed_or_unattempted_sources_and_incomplete_briefs_keep_partial(self):
        self.publish()
        for source_state in ('failed','unattempted'):
            with self.subTest(source_state=source_state):
                self.store.update_source_state('arxiv',source_state)
                self.assertEqual(self.daily.reconcile_health(self.day)['state'],'partial')
        self.store.update_source_state('arxiv','updated')
        self.assertTrue(self.daily.source_failures())  # A recovered failure is history.
        self.assertEqual(self.daily.reconcile_health(self.day)['state'],'completed')
        with self.store._connect() as conn:
            conn.execute('''UPDATE daily_cards SET card=json_set(card,'$.brief.state','degraded')
                            WHERE day=? AND position=1''',(self.day,))
        self.assertEqual(self.daily.reconcile_health(self.day)['state'],'partial')
        with self.store._connect() as conn:
            conn.execute('''UPDATE daily_cards SET card=json_set(card,'$.brief.state','ready')
                            WHERE day=? AND position=1''',(self.day,))
            conn.execute('DELETE FROM daily_cards WHERE day=? AND position=5',(self.day,))
        self.assertEqual(self.daily.reconcile_health(self.day)['state'],'partial')


if __name__ == '__main__':
    unittest.main()
