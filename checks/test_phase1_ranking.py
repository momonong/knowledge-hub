"""Daily selection using public entries and synthetic local reading state."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from knowledge_hub.discovery import rank
from knowledge_hub.reading import ReadingState
from knowledge_hub.sources import compute_content_hash
from knowledge_hub.store import KnowledgeStore


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def entry(key, title, text, *, source='arxiv', label='v1', age=0):
    if source == 'arxiv':
        url = f'https://arxiv.org/abs/{key[6:]}{label}'
    elif source == 'huggingface':
        url = f'https://huggingface.co/papers/{key[6:]}'
    else:
        url = f'https://github.com/pytorch/pytorch/releases/tag/{label}'
    scope = 'abstract' if key.startswith('arxiv:') else 'full'
    blocks = [{'anchor': 'abstract', 'text': text, 'source_url': url}]
    return {'key':key,'source_name':source,'title':title,'label':label,'scope':scope,
            'source_url':url,'published_at':(NOW-timedelta(days=age)).isoformat(),
            'blocks':blocks,'content_hash':compute_content_hash(title,scope,label,blocks)}


class PhaseOneRanking(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = KnowledgeStore(Path(tmp.name) / 'ranking.sqlite3')

    def test_five_public_picks_keep_core_and_frontier_subcategories(self):
        rows = [entry(f'arxiv:2611.1000{i}', f'KV Cache Scheduling {i}',
                      'We reduce decode latency by 22% on a throughput benchmark.') for i in range(6)]
        rows += [
            entry('arxiv:2611.10100', 'Multimodal Alignment with Interleaved Captions',
                  'We report a 7% accuracy gain using aligned text and visual features.'),
            entry('arxiv:2611.10101', 'Long-Term Memory for Multi-Agent Collaboration',
                  'We introduce memory consolidation for concurrent tool calls.'),
            entry('arxiv:2611.10102', 'World Models for Physical Interaction',
                  'We measure planning accuracy in held-out environments.'),
            entry('arxiv:2611.10103', 'Automated Data Curation with Data Flywheels',
                  'We prototype a feedback loop that selects data for the next training round.'),
        ]
        picks = rank(self.store, extra=rows, now=NOW)
        self.assertEqual(len(picks), 5)
        self.assertFalse(any(pick['personalized'] for pick in picks))
        self.assertEqual(len({pick['category'] for pick in picks}), 5)
        self.assertTrue(all('「' in pick['spectrum_reason'] for pick in picks))
        self.assertTrue(any('World Models' in pick['spectrum_reason'] for pick in picks))

    def test_category_cap_survives_sparse_pool(self):
        rows = [entry(f'arxiv:2611.1000{i}', f'KV Cache Scheduling {i}',
                      'We report lower decoding latency in a throughput evaluation.') for i in range(6)]
        rows.append(entry('arxiv:2611.10010', 'Speculative Decoding with Dynamic Verification',
                          'We evaluate the effect of variable draft lengths on accelerator throughput.'))
        picks = rank(self.store, extra=rows, now=NOW)
        self.assertEqual(list(Counter(pick.get('category') for pick in picks).values()), [2])

    def test_domain_exception_requires_reported_cross_domain_evidence(self):
        rows = [
            entry('arxiv:2611.10001', 'Eye Tracking and Reading Comprehension',
                  'We analyze eye movements during reading and report 10% higher accuracy on a benchmark.'),
            entry('arxiv:2611.10002', 'Clinical Question Answering with GraphRAG',
                  'We use retrieval-augmented generation and evaluate across several clinical datasets.'),
            entry('arxiv:2611.10003', 'GraphRAG for Clinical and Open-Domain Question Answering',
                  'We propose a domain-agnostic citation verifier for GraphRAG. '
                  'We evaluate it on clinical, legal, and open-domain question answering benchmarks.'),
            entry('arxiv:2611.10004', 'Spectral Analysis of Ocean Tides',
                  'We evaluate a benchmark on 10 datasets, using a GPU for training and evaluation.'),
            entry('arxiv:2611.10005', 'Cognitive Science with Clinical Question Answering',
                  'We propose a general GraphRAG method. We discuss future evaluation across medical and non-medical tasks.'),
            entry('arxiv:2611.10006', 'Evaluating GraphRAG for Clinical Software',
                  'We evaluate across five diverse domains confined to clinical subfields.'),
            entry('arxiv:2611.10007', 'GraphRAG for Clinical Software',
                  'We introduce a more efficient GraphRAG verifier. '
                  'We evaluate it on clinical corpora, but did not test it on open-domain datasets.'),
        ]
        picks = rank(self.store, extra=rows, now=NOW)
        self.assertEqual([p['item']['key'] for p in picks], ['arxiv:2611.10003'])
        self.assertIn('GraphRAG', picks[0]['spectrum_reason'])
        self.assertNotIn('臨床', picks[0]['spectrum_reason'])

    def test_framework_can_surface_after_a_full_research_feed(self):
        release = entry('pytorch:v3.0.0', 'PyTorch 3.0.0',
                        'This release introduces a new compiler backend that reduces kernel launch '
                        'overhead by 30% on the supported GPUs. It also adds documentation for CUDA graphs.',
                        source='pytorch', label='v3.0.0', age=4)
        self.store.upsert_parsed_entry(release)
        self.store.upsert_parsed_entry(entry('pytorch:v2.9.1', 'PyTorch 2.9.1',
            'This patch adds documentation for CUDA graphs.',
            source='pytorch', label='v2.9.1', age=4))
        for i in range(161):
            self.store.upsert_parsed_entry(entry(f'arxiv:2611.{10000+i}',
                f'KV Cache Scheduling {i}', 'We report 20% lower latency on a throughput benchmark.'))
        picks = rank(self.store, now=NOW)
        self.assertIn('pytorch:v3.0.0', [p['item']['key'] for p in picks])
        self.assertNotIn('pytorch:v2.9.1', [p['item']['key'] for p in picks])
        framework = next(p for p in picks if p['item']['key'] == 'pytorch:v3.0.0')
        self.assertIn('compiler backend', framework['spectrum_reason'])

    def test_finance_requires_robustness_and_execution_evidence(self):
        rows = [
            entry('arxiv:2611.10001', 'Quantitative Finance Backtest with 30% Returns',
                  'We report 30% returns in a backtest on a single market.'),
            entry('arxiv:2611.10002', 'Portfolio Backtest Across Market Regimes',
                  'We run a walk-forward evaluation out of sample over several market regimes, '
                  'account for transaction costs and slippage, and report lower drawdown than two baselines.'),
            entry('arxiv:2611.10003', 'Portfolio Backtest with Realistic Sizing',
                  'We report risk-adjusted returns from a walk-forward evaluation, ignoring transaction costs.'),
        ]
        picks = rank(self.store, extra=rows, now=NOW)
        self.assertEqual([p['item']['key'] for p in picks], ['arxiv:2611.10002'])
        self.assertIn('量化研究', picks[0]['category'])

    def test_blind_spot_uses_bound_local_note_and_keeps_text_private(self):
        context = entry('arxiv:2501.10001', 'Production decoding notes',
                        'A note on verifying batch output in a sequence generator.', age=350)
        item_id, version_id, _ = self.store.upsert_parsed_entry(context)
        self.store.add_note(item_id, version_id, 'abstract', '#盲點/待查 Speculative decoding 的 KV Cache 延遲 PRIVATE_SENTINEL')
        self.store.add_note(item_id, version_id, 'abstract', '#想法 世界模型如何模擬物理環境？')
        rows = [
            entry('arxiv:2611.10001', 'Speculative Decoding with a Shared KV Cache',
                  'We propose a shared cache that adapts to sequences with different draft lengths.'),
            entry('arxiv:2611.10002', 'World Models with Differentiable Simulation',
                  'We evaluate action planning in held-out interactive environments.'),
        ]
        picks = rank(self.store, extra=rows, now=NOW)
        self.assertEqual(len(picks), 2)
        self.assertEqual(picks[0]['item']['key'], 'arxiv:2611.10001')
        self.assertEqual(picks[0]['reasons'][0]['href'], f'/items/{item_id}?version={version_id}#abstract')
        self.assertFalse(picks[1]['personalized'])
        self.assertNotIn('PRIVATE_SENTINEL', str(picks))

    def test_tag_only_blind_spot_uses_bound_source_paragraph(self):
        context = entry('arxiv:2501.10001', 'Production decoding notes',
                        'Can we verify speculative decoding without retaining the KV Cache?', age=350)
        item_id, version_id, _ = self.store.upsert_parsed_entry(context)
        self.store.add_note(item_id, version_id, 'abstract', '#盲點/待查')
        candidate = entry('arxiv:2611.10001', 'Speculative Decoding with Cache Sharing',
                          'We reduce metadata overhead in sequence verification.')
        pick, = rank(self.store, extra=[candidate], now=NOW)
        self.assertEqual(pick['reasons'][0]['href'], f'/items/{item_id}?version={version_id}#abstract')

    def test_generic_and_lab_history_do_not_supply_personalization(self):
        clinical = entry('arxiv:2501.10001', 'Clinical Reading with Visual Scores',
                         'This eye tracking study reports multimodal clinical evaluation.', age=350)
        item_id, version_id, _ = self.store.upsert_parsed_entry(clinical)
        ReadingState(self.store).save(item_id, version_id, 0, {
            'open_questions':'Does a larger clinical cohort change multimodal evaluation results?',
        })
        projects = [
            {'name':'eye-tracking-experiment','topics':['vision','eval']},
            {'name':'benchmark-tests','topics':['eval']},
        ]
        candidate = entry('arxiv:2611.10001', 'Multimodal Alignment with Interleaved Captions',
                          'We report a 7% accuracy gain on multimodal evaluation benchmarks.')
        pick, = rank(self.store, projects, extra=[candidate], now=NOW)
        self.assertEqual(pick['reasons'], [])

    def test_dedup_prefers_fixed_arxiv_edition_in_any_source_order(self):
        abstract = 'We evaluate retrieval-augmented generation across three benchmarks.'
        hf = entry('arxiv:2611.10001', 'GraphRAG Verification', abstract, source='huggingface', label='hf-index')
        arxiv = entry('arxiv:2611.10001', 'GraphRAG Verification', abstract, label='v3')
        undated = {**entry('arxiv:2611.10001', 'GraphRAG Verification', abstract, label='v4'), 'published_at':''}
        self.store.upsert_parsed_entry(hf)
        for extra in ([hf, arxiv], [arxiv, hf], [arxiv, undated]):
            with self.subTest(extra=[row['label'] for row in extra]):
                picks = rank(self.store, extra=extra, now=NOW)
                self.assertEqual(len(picks), 1)
                self.assertEqual(picks[0]['version']['source_url'], 'https://arxiv.org/abs/2611.10001v3')

    def test_recency_exclusion_keeps_other_eligible_articles(self):
        rows = [
            entry('arxiv:2611.10001', 'KV Cache Pruning', 'We report throughput under different batch sizes.'),
            entry('arxiv:2611.10002', 'World Models for Interactive Simulation',
                  'We evaluate multi-step plans for new tasks.'),
        ]
        picks = rank(self.store, extra=rows, now=NOW, exclude_keys={'arxiv:2611.10002'})
        self.assertEqual([p['item']['key'] for p in picks], ['arxiv:2611.10001'])


if __name__ == '__main__':
    unittest.main()
