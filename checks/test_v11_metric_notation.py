"""Generated metrics render as text without migrating citations or user records."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from checks import test_v11_guided as guide_fixtures
from knowledge_hub import guided, grounding, metric_notation
from knowledge_hub.captures import CaptureError, CaptureStore
from knowledge_hub.followup import Followups
from knowledge_hub.model_stream import ModelConfig
from knowledge_hub.sources import parse_arxiv_html
from knowledge_hub.store import KnowledgeStore


AUTHOR = r'reranking raises recall $\delta_{1}$ from 60% to 71% on CoralBench.'
GENERATED = r'CoralBench 的 $\delta_{1}$ recall 從 $60\%$ 提升到 $71\%$。'
DISPLAY = 'CoralBench 的 δ1 recall 從 60% 提升到 71%。'


class MetricNotationTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'knowledge.sqlite3'
        self.store = KnowledgeStore(self.path)
        html = guide_fixtures.HTML.replace(b'reranking raises recall from 60% to 71% on CoralBench.', AUTHOR.encode())
        self.item_id, self.version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(html, guide_fixtures.SOURCE))
        self.item = self.store.get_item(self.item_id)
        self.version = self.store.select_version(self.item['versions'], self.version_id)
        self.routes = {'primary': ModelConfig('http://127.0.0.1:8489/v1', 'test-math-display')}

    def test_only_supported_math_notation_is_projected(self):
        original = r'以 $\delta_{1}$ 對照 \(δ_{2}\)，差距 $23.1\%$，吞吐 $5.05\times$，保留 $\frac{2}{3}$。'
        displayed = r'以 δ1 對照 δ2，差距 23.1%，吞吐 5.05×，保留 $\frac{2}{3}$。'
        self.assertEqual(metric_notation.plain_metric(original), displayed)
        self.assertEqual(metric_notation.plain_metric(displayed), displayed)
        self.assertEqual(metric_notation.plain_metric(r'δ1\delta_{1}'), 'δ1')

    def test_new_guide_reopened_cache_and_old_tags_share_the_same_original_citation(self):
        def transport(_config, messages, **kwargs):
            rows = guide_fixtures.GuidedRoomTests.author_rows(self, messages)
            next(row for row in rows if row['type'] == 'evidence')['text'] = GENERATED
            rows[-1]['questions'][1]['question'] = r'觀察 $\delta_{1}$ 時應如何比較兩個方法？'
            yield from map(json.dumps, rows)

        with patch.object(guided, 'stream_model', side_effect=transport) as send:
            guide = guided.guide(self.store, self.item_id, self.version_id, self.routes)
            reopened = guided.guide(KnowledgeStore(self.path), self.item_id, self.version_id, self.routes)
        send.assert_called_once()
        self.assertTrue(reopened['model']['cache_hit'])
        self.assertIn('δ1', reopened['chips'][1]['question'])
        evidence = next(section for section in guide['sections'] if section['role'] == 'evidence')
        self.assertEqual(reopened['sections'], guide['sections'])
        self.assertEqual(evidence['text'], DISPLAY)
        cite = evidence['citations'][0]
        self.assertEqual(cite['anchor'], 'measure')
        self.assertIn(AUTHOR, cite['quote'])
        self.assertTrue(cite['href'].endswith('#measure'))
        with self.store._connect() as conn:
            payload = json.loads(conn.execute('SELECT payload FROM grounded_guides').fetchone()[0])
        raw = next(section for section in payload['sections'] if section['role'] == 'evidence')
        self.assertEqual(raw['text'], GENERATED)
        self.assertEqual(raw['citations'], evidence['citations'])

        captures = CaptureStore(self.store, Followups(self.store))
        tag = {'action': 'tag', 'request_id': 'legacy-metric-capture', 'tag': '#想法',
               'anchor': 'measure', 'quote': GENERATED, 'comment': r'私人註記 $\delta_{3}$'}
        old = captures.act(self.item, self.version, tag)
        with self.store._connect() as conn:
            old_receipt = {key: value for key, value in old.items() if key != 'display_quote'}
            conn.execute("UPDATE capture_intents SET receipt=? WHERE request_id=?",
                         (json.dumps(old_receipt), tag['request_id']))
        self.assertEqual(captures.act(self.item, self.version, tag), old)
        refreshed = CaptureStore(KnowledgeStore(self.path), Followups(self.store)).list(self.item_id, self.version_id)
        self.assertEqual(refreshed[0]['quote'], GENERATED)
        self.assertEqual(refreshed[0]['display_quote'], DISPLAY)
        self.assertEqual(refreshed[0]['comment'], tag['comment'])
        self.assertIn(GENERATED, self.store.get_item(self.item_id)['notes'][0]['text'])

        new = captures.act(self.item, self.version, {**tag, 'request_id': 'current-metric-capture', 'quote': DISPLAY})
        self.assertEqual(new['href'], cite['href'])
        for selected in (DISPLAY, GENERATED):
            recovered = grounding.make_catalog(self.item, self.version, anchor='measure',
                                                selected_quote=selected, guide_sections=payload['sections'])
            self.assertEqual(next(iter(recovered)), cite['id'])
            self.assertIn(AUTHOR, recovered[cite['id']]['quote'])
        with self.assertRaisesRegex(ValueError, '不屬於'):
            grounding.make_catalog(self.item, self.version, anchor='limit',
                                   selected_quote=DISPLAY, guide_sections=payload['sections'])
        with self.assertRaises(CaptureError):
            captures.act(self.item, self.version,
                         {**tag, 'request_id': 'fabricated-metric-capture', 'quote': DISPLAY.replace('71%', '81%')})

    def test_followup_stream_history_and_replay_use_the_same_display_only_projection(self):
        def transport(_config, messages, **kwargs):
            packet = json.loads(messages[-1]['content'])
            ref = next(candidate['id'] for candidate in packet['evidence'] if AUTHOR in candidate['quote'])
            yield json.dumps({'kind': 'evidence', 'text': GENERATED, 'ids': [ref]})
            for kind in ('limits', 'inference'):
                yield json.dumps({'kind': kind, 'text': '原文未交代', 'ids': []})

        service = Followups(self.store, self.routes)
        args = self.item, self.version, '這個指標的結果是多少？', 'measure', 'math-followup'
        with patch('knowledge_hub.model_stream.stream_model', side_effect=transport) as send:
            events = list(service.run(*args))
            self.assertEqual(events[-1][0], 'done', events[-1])
            second = Followups(KnowledgeStore(self.path), self.routes)
            replay = list(second.run(*args))
        send.assert_called_once()
        section = events[1][1]
        self.assertEqual(section['text'], DISPLAY)
        self.assertEqual(replay[1][1], section)
        self.assertEqual(second.history(self.item_id, self.version_id)[0]['sections'][0], section)
        self.assertIn(AUTHOR, section['citations'][0]['quote'])
        self.assertEqual(second.get(events[0][1]['id'])['sections'][0]['text'], GENERATED)
        with self.assertRaisesRegex(ValueError, '量化數字'):
            cite = section['citations'][0]
            grounding.check_section({'kind': 'evidence', 'text': GENERATED.replace('71', '81'),
                                     'ids': [cite['id']]}, {cite['id']: cite})


if __name__ == '__main__':
    unittest.main()
