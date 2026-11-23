"""The Taipei worker and its authenticated report share the same schedule."""
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

from checks.test_study import entry
from knowledge_hub.daily import DailyRefresh, TAIPEI, run_hosted_refresh
from knowledge_hub.hosting import MountedApplication
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app


class Schedule(unittest.TestCase):
    def simulate(self, stamps, states):
        clock = iter(datetime.fromisoformat(stamp) for stamp in stamps)
        states = iter(states)
        waits, runs = [], []

        class Stop:
            def wait(self, delay):
                waits.append(delay)
                return len(waits) > len(stamps)

        def refresh():
            runs.append(next(states))
            return {'state': runs[-1]}

        run_hosted_refresh(refresh, Stop(), now=lambda: next(clock))
        return waits, runs

    def test_utc_host_waits_exactly_to_taipei_seven_and_runs_once_per_day(self):
        waits, runs = self.simulate([
            '2026-11-22T22:59:59.750+00:00', '2026-11-22T23:00:00+00:00',
            '2026-11-22T23:10:00+00:00', '2026-11-23T23:00:00+00:00',
        ], ['completed', 'partial'])
        self.assertEqual(waits, [1, .25, 600, 600, 600])
        self.assertEqual(runs, ['completed', 'partial'])

    def test_after_seven_startup_can_recover_a_prior_lease_but_does_not_repeat_a_settled_run(self):
        _, runs = self.simulate([
            '2026-11-23T12:00:00+08:00', '2026-11-23T12:10:00+08:00',
            '2026-11-23T12:20:00+08:00', '2026-11-24T07:00:00+08:00',
        ], ['running', 'completed', 'failed'])
        self.assertEqual(runs, ['running', 'completed', 'failed'])


class DiscoveryStatus(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'report.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.daily = DailyRefresh(self.store)
        self.app = create_app(self.db)
        self.stamp = datetime(2026, 11, 23, 7, 42, tzinfo=TAIPEI)

    def request(self, *, accepts='text/html', allow=True):
        app = MountedApplication(self.app, origin='https://fixture.example', prefix='/knowledge',
                                 authorize=lambda _: allow)
        env = {}
        setup_testing_defaults(env)
        env.update(HTTP_HOST='fixture.example', HTTP_X_FORWARDED_PROTO='https', REMOTE_ADDR='127.0.0.1',
                   PATH_INFO='/knowledge/discovery/status', HTTP_ACCEPT=accepts)
        responses = []
        with patch('knowledge_hub.daily.taipei_now', return_value=self.stamp):
            body = b''.join(app(env, lambda status, headers: responses.append((status, headers))))
        status, headers = responses[0]
        return int(status.split()[0]), headers, body.decode()

    def publish(self, day):
        cards = []
        for i in range(5):
            title = f'Paper {i} <here>'
            item, version, _ = self.store.upsert_parsed_entry(entry(f'arxiv:2611.0000{i}', title, 'Public evidence.'))
            edition = self.store.select_version(self.store.get_item(item)['versions'], version)
            self.store.add_note(item, version, 'p1', 'UNRELATED_PRIVATE_NOTE')
            brief = {'state': 'ready' if i != 1 else 'degraded', 'l1': {'change': 'PRIVATE_LAYER_TEXT'}}
            if i < 2:
                brief.update(mode='model' if i == 0 else 'abstract_fallback', model='verified-route', ttft_ms=97)
            cards.append({'item': {'id':item, 'title': title}, 'version': edition, 'brief': brief,
                          'reasons': [{'title': 'PRIVATE_TOPIC'}]})
        self.daily.run(lambda: cards, day=day)
        with self.store._connect() as conn:
            conn.execute("UPDATE daily_refresh SET started_at='2026-11-22T23:00:03Z', "
                         "finished_at='2026-11-22T23:02:01Z' WHERE day=?", (day,))
        return cards

    def test_mounted_html_and_json_show_taipei_receipt_sources_and_five_visible_cards(self):
        cards = self.publish('2026-11-23')
        with patch('knowledge_hub.store.now_iso', return_value='2026-11-22T23:01:00Z'):
            self.store.update_source_state('pytorch', 'updated')
            self.store.update_source_state('arxiv', 'failed', 'HTTP 429')
        code, headers, body = self.request(accepts='application/json')
        self.assertEqual(code, 200)
        self.assertIn(('Cache-Control', 'no-store'), headers)
        self.assertIn(('Vary', 'Accept'), headers)
        report = json.loads(body)
        self.assertEqual(report['timezone'], 'Asia/Taipei')
        self.assertEqual(report['local_date'], '2026-11-23')
        self.assertEqual(report['next_scheduled_at'], '2026-11-24T07:00:00+08:00')
        self.assertEqual(report['today']['started_at_taipei'], '2026-11-23T07:00:03+08:00')
        self.assertEqual(report['today']['finished_at_taipei'], '2026-11-23T07:02:01+08:00')
        self.assertEqual(report['sources']['arxiv']['state'], 'failed')
        self.assertEqual(report['sources']['pytorch']['last_success_at_taipei'], '2026-11-23T07:01:00+08:00')
        self.assertEqual(len(report['cards']), 5)
        self.assertEqual([row['brief']['mode'] for row in report['cards'][:3]], ['model','abstract_fallback','local'])
        self.assertEqual(report['cards'][0]['brief']['ttft_ms'], 97)
        self.assertEqual(report['cards'][0]['version_id'], str(cards[0]['version']['id']))
        code, _, markup = self.request()
        self.assertEqual(code, 200)
        for text in ('07:00:03', '07:02:01', '下次例行排程', '讀取失敗', '已更新', '模型簡報', '摘要降級',
                     '本地簡報', 'Paper 0 &lt;here&gt;', 'Paper 4 &lt;here&gt;'):
            self.assertIn(text, markup)
        self.assertIn(f'/knowledge/items/{cards[0]["item"]["id"]}?version={cards[0]["version"]["id"]}', markup)
        for private in ('UNRELATED_PRIVATE_NOTE', 'PRIVATE_LAYER_TEXT', 'PRIVATE_TOPIC'):
            self.assertNotIn(private, body + markup)
        code, _, denied = self.request(accepts='application/json', allow=False)
        self.assertEqual(code, 401)
        self.assertNotIn('Paper 0', denied)

    def test_before_seven_reports_today_as_pending_with_prior_published_cards(self):
        self.publish('2026-11-22')
        self.stamp = datetime(2026, 11, 23, 6, 42, tzinfo=TAIPEI)
        _, _, payload = self.request(accepts='application/json')
        report = json.loads(payload)
        self.assertIsNone(report['today'])
        self.assertEqual(report['selection_day'], '2026-11-22')
        self.assertEqual(report['next_scheduled_at'], '2026-11-23T07:00:00+08:00')
        _, _, markup = self.request()
        self.assertIn('今日尚未執行', markup)
        self.assertIn('最近一期 · 2026-11-22', markup)

    def test_empty_report_has_no_fake_briefing(self):
        _, _, payload = self.request(accepts='application/json')
        report = json.loads(payload)
        self.assertEqual(report['cards'], [])
        self.assertIsNone(report['selection_day'])
        _, _, markup = self.request()
        self.assertIn('還沒有已發布的簡報', markup)
