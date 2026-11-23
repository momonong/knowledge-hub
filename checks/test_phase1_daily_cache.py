import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from knowledge_hub.daily import DailyRefresh, TAIPEI, run_hosted_refresh
from knowledge_hub.sources import parse_huggingface_papers
from knowledge_hub.store import KnowledgeStore
from test_discovery import feed, RELATED


class DailyCache(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = KnowledgeStore(Path(directory.name) / 'test.db')
        entry, = parse_huggingface_papers(feed([RELATED], '2026-11-20T12:00:00Z'))
        item, version, _ = self.store.upsert_parsed_entry(entry)
        self.pick = dict(item=dict(id=item, key=entry['key'], title=entry['title'], source_name='huggingface'),
                         version=dict(id=version, label=entry['label'], published_at=entry['published_at'],
                                      source_url=entry['source_url'], scope='abstract'),
                         category='Agent 與檢索', spectrum_reason='時序記憶檢索',
                         reasons=[], evidence='作者摘要', evidence_strength=2,
                         brief={'l1': {'change':'改動'}, 'l2': {'mechanism':'機制'}, 'state':'ready'})
        self.daily = DailyRefresh(self.store)

    def test_selection_and_cache_survive_restart_and_failed_refresh(self):
        day = '2026-11-20'
        self.daily.run(lambda: [self.pick], day=day)
        restarted = DailyRefresh(KnowledgeStore(self.store.db_path))
        self.assertEqual(restarted.selection(day)[0]['brief'], self.pick['brief'])
        restarted.run(lambda: [], day=day, force=True)
        self.assertEqual(restarted.selection(day)[0]['version']['id'], self.pick['version']['id'])
        self.assertIn(self.pick['item']['key'], restarted.recent_keys('2026-11-21'))
        self.assertEqual(restarted.selection('2026-11-21'), [])

    def test_crashed_run_can_recover_without_double_sending_active_run(self):
        day = self.daily.day()
        with self.store._connect() as conn:
            conn.execute("INSERT INTO daily_refresh(day,state,started_at) VALUES(?,'running','2000-01-01T00:00:00Z')", (day,))
        def sync():
            self.assertEqual(self.daily.run(lambda: self.fail('duplicate collection'))['state'], 'running')
            return [self.pick]
        self.assertEqual(self.daily.run(sync)['state'], 'partial')
        self.assertEqual(len(self.daily.selection(day)), 1)

    def test_scheduler_wakes_at_seven_without_a_browser(self):
        clock = iter([datetime(2026, 11, 21, 6, 59, 57, tzinfo=TAIPEI),
                      datetime(2026, 11, 21, 7, 0, 0, tzinfo=TAIPEI)])
        waits, runs = [], []

        class Stop:
            def wait(self, delay):
                waits.append(delay)
                return len(waits) == 3

        run_hosted_refresh(lambda: runs.append(True), Stop(), now=lambda: next(clock))
        self.assertEqual(waits[:2], [1, 3])
        self.assertEqual(runs, [True])

    def test_source_failure_history_survives_later_success(self):
        self.store.update_source_state('arxiv', 'failed', 'Feed unavailable')
        self.store.update_source_state('arxiv', 'updated')
        failures = self.daily.source_failures()
        self.assertEqual([(r['name'], r['error']) for r in failures], [('arxiv', 'Feed unavailable')])
