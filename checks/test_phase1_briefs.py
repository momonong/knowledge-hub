"""L1/L2 precomputation from versioned public evidence."""
import unittest

from knowledge_hub.briefs import build_brief


ABSTRACT = (
    'Static KV cache eviction wastes memory during bursty inference. '
    'We introduce QueueCache, which partitions the KV cache by tenant and evicts blocks at admission time. '
    'QueueCache reduces p99 latency by 18% against the FIFO baseline on Trace-A. '
    'The method currently supports single-GPU serving only.'
)


def pick(abstract=ABSTRACT, **overrides):
    return {
        'item': {'key': 'arxiv:2611.90001', 'title': 'QueueCache for Bursty Inference'},
        'version': {'scope': 'abstract', 'blocks': [{'anchor': 'abstract', 'text': abstract}]},
        'category': '推論與系統',
        'spectrum_reason': '可追蹤推論與系統的資源管理。',
        **overrides,
    }


class Phase1Briefs(unittest.TestCase):
    def test_author_claims_are_used_in_their_own_fields(self):
        result = build_brief(pick())
        self.assertEqual((result['state'], result['attempts'], result['error']), ('ready', 1, ''))
        self.assertIn('快取', result['l1']['change'])
        self.assertIn('QueueCache', result['l1']['highlight'])
        self.assertIn('推論與系統', result['l1']['relevance'])
        self.assertIn('按租戶分割', result['l2']['mechanism'])
        self.assertIn('p99 延遲降低 18%', result['l2']['metrics'])
        self.assertIn('以 FIFO 為比較基準', result['l2']['comparison'])
        self.assertIn('目前僅支援單 GPU 推論服務', result['l2']['limits'])
        self.assertIn('single-GPU', result['l2']['limits'])

    def test_metrics_and_limits_are_not_guessed_from_data_sizes_or_problem_scope(self):
        result = build_brief(pick(
            'We propose a robotic manipulation policy that predicts end-effector waypoints from depth images. '
            'We use 12 demonstrations for evaluation. Conventional systems struggle with narrow passages.'
        ))
        self.assertEqual(result['state'], 'ready')
        self.assertIn('深度影像', result['l2']['mechanism'])
        self.assertIn('摘要', result['l2']['metrics'])
        self.assertIn('未', result['l2']['metrics'])
        self.assertNotIn('12', result['l2']['metrics'])
        self.assertIn('未', result['l2']['comparison'])
        self.assertIn('未', result['l2']['limits'])

    def test_metric_transition_and_limits_in_an_evaluation_summary(self):
        result = build_brief(pick(
            'We introduce a vision system that tracks occluded objects in video. '
            'We study 16 benchmarks and release an updated tracker whose object recall rises from 38% to 73%. '
            'Our evaluation also reveals its current limitations, including fast camera motion and long occlusions.'
        ))
        self.assertIn('38%', result['l2']['metrics'])
        self.assertIn('73%', result['l2']['metrics'])
        self.assertIn('fast camera motion and long occlusions', result['l2']['limits'])

    def test_retention_tradeoff_and_unnamed_baseline_have_distinct_context(self):
        result = build_brief(pick(
            'We introduce TraceSampler, a joint telemetry pipeline that sequentially reduces record count and context length. '
            'On Fleet-A, TraceSampler retains 96.2% of the original average accuracy with 45% fewer records, '
            'exceeding the strongest baseline by 2.3 percentage points in accuracy retention.'
        ))
        self.assertIn('依序縮減紀錄筆數與脈絡長度', result['l1']['highlight'])
        self.assertIn('平均準確率', result['l2']['metrics'])
        self.assertIn('Fleet-A', result['l2']['metrics'])
        self.assertIn('紀錄筆數減少 45%', result['l2']['metrics'])
        self.assertIn('2.3 個百分點', result['l2']['comparison'])
        self.assertIn('最強基準', result['l2']['comparison'])
        self.assertNotIn('45%', result['l2']['comparison'])

    def test_two_reported_ranges_keep_their_own_metrics_and_proper_names(self):
        result = build_brief(pick(
            'We introduce TraceLens, a runtime profiler for edge workloads. '
            "Its profiler maps intermediate states to a shared semantic space defined by the decoder's output vocabulary. "
            'On Model-Q and Model-R, TraceLens reduces P95 per-token latency by 17--33% '
            'and improves TTFT by 1.2--1.6x compared with StreamKit with FeatureStore. '
            'Generated KV remains resident.'
        ))
        self.assertIn('將中間狀態映射至', result['l2']['mechanism'])
        self.assertIn('P95 每 token 延遲降低 17–33%', result['l2']['metrics'])
        self.assertIn('TTFT 改善 1.2–1.6×', result['l2']['metrics'])
        self.assertIn('Model-Q', result['l2']['metrics'])
        self.assertIn('StreamKit with FeatureStore', result['l2']['comparison'])
        self.assertIn('常駐', result['l2']['limits'])

    def test_negated_actions_and_unproven_targets_are_not_reported_as_findings(self):
        result = build_brief(pick(
            'We introduce Policy-X for manipulation from depth images. '
            'The existing decoder cannot predict end-effector waypoints from depth images. '
            'Our approach is designed to reduce p99 latency by 18%.'
        ))
        self.assertIn('Policy-X', result['l1']['highlight'])
        self.assertNotIn('用深度影像預測', result['l2']['mechanism'])
        self.assertNotIn('18%', result['l2']['metrics'])

    def test_full_text_side_experiment_cannot_replace_the_main_proposal(self):
        result = build_brief(pick(version={
            'scope': 'full',
            'blocks': [
                {'anchor': 'abstract', 'text':
                    'We introduce UnifiedModes, which interleaves image encoding with audio decoding.'},
                {'anchor': 'S3', 'text': '3 Methodology'},
                {'anchor': 'S3.p1', 'text':
                    'We train the reward classifier on a small side dataset to analyze one ablation.'},
            ],
        }))
        self.assertIn('UnifiedModes', result['l1']['highlight'])
        self.assertIn('交錯', result['l1']['highlight'])
        self.assertNotIn('reward classifier', result['l2']['mechanism'])

    def test_official_release_excerpt_can_describe_a_concrete_change(self):
        result = build_brief(pick(
            item={'key': 'sample:v3', 'title': 'Sample v3'},
            version={
                'scope': 'full',
                'blocks': [{'anchor': 'features.p1', 'text':
                            '- Added paged attention support for CUDA graphs.'}],
            },
        ))
        self.assertEqual(result['state'], 'ready')
        self.assertIn('分頁注意力', result['l1']['highlight'])
        self.assertIn('CUDA graphs', result['l2']['mechanism'])

    def test_release_metrics_and_tradeoffs_come_from_the_focused_feature(self):
        result = build_brief(pick(
            item={'key': 'sample:v3', 'title': 'Sample v3'},
            version={
                'scope': 'full',
                'blocks': [
                    {'anchor': 'summary.p1', 'text':
                     '- **3 new operations** (import, export, and inspect!) '
                     '- **Adaptive buffer sizing is on by default:** The server sizes the buffer from available memory'
                     ' - in most cases configuration needs no changes. '
                     '- **Faster images:** 9× faster decoder throughput.'},
                    {'anchor': 'adaptive-buffer-sizing', 'text': 'Adaptive buffer sizing'},
                    {'anchor': 'adaptive-buffer-sizing.p1', 'text':
                     '- The buffer is sized from available memory after initialization and warmup, not from a startup estimate. '
                     '- **Result:** Buffer throughput increases by 12% on Trace-Y. '
                     '- **Trade-off:** peak memory usage can rise with other jobs running.'},
                ],
            },
        ))
        self.assertEqual(result['state'], 'ready')
        self.assertIn('預設啟用', result['l1']['highlight'])
        self.assertNotIn('3 new operations', result['l1']['highlight'])
        self.assertIn('初始化與暖機後', result['l2']['mechanism'])
        self.assertIn('12%', result['l2']['metrics'])
        self.assertNotIn('9×', result['l2']['metrics'])
        self.assertIn('startup estimate', result['l2']['comparison'])
        self.assertIn('other jobs running', result['l2']['limits'])

    def test_prior_work_is_not_reported_as_this_papers_result(self):
        result = build_brief(pick(version={
            'scope': 'full',
            'blocks': [
                {'anchor': 'abstract', 'text': 'We propose a set-based decoder to track occluded objects.'},
                {'anchor': 'S1', 'text': '1 Related Work'},
                {'anchor': 'S1.p1', 'text': 'Another model achieves 92% accuracy on our benchmark.'},
                {'anchor': 'S2', 'text': '2 Limitations'},
                {'anchor': 'S2.p1', 'text': 'Our study is limited to clips from stationary cameras.'},
                {'anchor': 'bib.1', 'text': 'Baseline comparisons show 99% accuracy.'},
            ],
        }))
        self.assertNotIn('92%', result['l2']['metrics'])
        self.assertNotIn('99%', result['l2']['metrics'])
        self.assertIn('stationary cameras', result['l2']['limits'])

    def test_malformed_provider_is_retried_and_never_leaks_exception_details(self):
        calls = []

        def generate(candidate):
            calls.append(candidate['item']['key'])
            if len(calls) == 1:
                return {'l1': {'change': ' '}, 'l2': {}}
            raise TimeoutError('SECRET_SENTINEL')

        result = build_brief(pick(), generate)
        self.assertEqual(calls, ['arxiv:2611.90001'] * 3)
        self.assertEqual((result['state'], result['attempts']), ('degraded', 3))
        self.assertTrue(result['error'])
        self.assertNotIn('SECRET_SENTINEL', str(result))
        self.assertIn('作者摘要', result['l2']['mechanism'])
        self.assertIn(ABSTRACT, result['l2']['mechanism'])

    def test_valid_provider_response_after_retry_has_the_exact_schema(self):
        candidate = pick()
        content = {
            'l1': {'change': '降低尖峰排程的延遲。', 'highlight': '按租戶分割 KV cache。',
                   'relevance': '可對照推論服務的多租戶策略。'},
            'l2': {'mechanism': '請求進場時決定哪些區塊要回收。',
                   'metrics': '在 Trace-A 上 p99 latency 降低 18%。',
                   'comparison': '作者對照 FIFO baseline。',
                   'limits': '僅支援單張 GPU。'},
        }
        responses = iter([{'l1': {**content['l1'], 'relevance': ''}, 'l2': content['l2']}, content])

        def generate(row):
            self.assertIs(row, candidate)
            return next(responses)

        result = build_brief(candidate, generate)
        self.assertEqual(result, {**content, 'state': 'ready', 'attempts': 2, 'error': ''})

    def test_no_abstract_is_disclosed_without_using_the_title_as_an_abstract(self):
        result = build_brief(pick(''))
        self.assertEqual(result['state'], 'degraded')
        self.assertIn('摘要', result['l2']['mechanism'])
        self.assertNotIn('QueueCache for Bursty Inference', result['l2']['mechanism'])
        for layer in ('l1', 'l2'):
            self.assertTrue(all(isinstance(field, str) and field.strip() for field in result[layer].values()))


if __name__ == '__main__':
    unittest.main()
