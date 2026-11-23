"""L1/L2 source selection and model receipts at the stream boundary."""
import json
from types import SimpleNamespace
import unittest

from knowledge_hub import briefs, visuals
from knowledge_hub.model_stream import ModelStreamError


ANSWER = {
    'l1': {
        'change': '在高併發下，原先的配置方式容易讓 KV cache 碎片化。',
        'highlight': '這篇把 cache 分配粒度縮到固定尺寸的 block。',
        'relevance': '如果你的模型混跑長短不一的請求，可以用它的比較來決定要不要改排程。',
    },
    'l2': {
        'mechanism': '每輪先量出剩餘 block，再把請求放進可重用的 pool。',
        'metrics': '原文未交代',
        'comparison': '舊配置以請求為單位，這個方法改成重用 block。',
        'limits': '作者只評估單節點，跨節點的調度成本仍未知。',
    },
}


def pick(blocks, *, scope='full', title='KV Cache allocation', key='arxiv:2611.12345'):
    return {
        'item': {'key': key, 'title': title, 'status': 'PRIVATE_STATUS'},
        'category': '推論最佳化與 ML 系統',
        'spectrum_reason': '可以核對推論時的資源取捨',
        'reasons': [{'title': 'PRIVATE_NOTE'}],
        'version': {
            'scope': scope, 'label': 'v1',
            'blocks': [{'anchor': anchor, 'text': content} for anchor, content in blocks],
        },
    }


class ModelBriefEvidence(unittest.TestCase):
    def test_configured_structured_route_receives_the_two_brief_layers(self):
        options = {}
        def stream(_config, _messages, **kw):
            options.update(kw)
            yield json.dumps(ANSWER, ensure_ascii=False)
        generate = briefs.model_generator(SimpleNamespace(model='test', structured=True), stream)
        result = briefs.build_brief(pick([{'anchor':'method','text':'We propose cache block reuse.'}]), generate)
        self.assertEqual(result['mode'], 'model')
        schema = options['response_schema']
        self.assertEqual(schema['required'], ['l1','l2'])
        self.assertEqual(set(schema['properties']['l2']['required']), set(ANSWER['l2']))
        self.assertFalse(schema['properties']['l1']['additionalProperties'])

    def collect(self, item):
        requests = []

        def stream(_config, messages):
            requests.append(json.loads(messages[-1]['content']))
            yield json.dumps(ANSWER)

        result = briefs.build_brief(item, briefs.model_generator(object(), stream))
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(len(requests), 1)
        self.assertNotIn('PRIVATE', json.dumps(requests[0]))
        self.assertIsInstance(requests[0]['source'], list)
        return requests[0]['source']

    def test_full_article_keeps_late_method_results_and_limits_with_anchors(self):
        filler = [(f'S1.p{i}', 'The motivation for the field is considered here. ' * 100) for i in range(20)]
        blocks = [
            ('abstract', 'We introduce a cache allocation policy that tracks fragmentation.'),
            ('S1', '1 Introduction'), *filler,
            ('S2', '2 Methodology'),
            ('S2.p1', 'The allocator keeps a shared free list and assigns blocks one at a time.'),
            ('S3', '3 Results'),
            ('S3.p4', 'On the mixed workload, p95 latency fell from 91 ms to 64 ms with the new allocator.'),
            ('S4', '4 Limitations'),
            ('S4.p1', 'We do not evaluate deployments with multiple nodes or disaggregated memory.'),
            ('bib.1', 'A bibliography reference would report a dramatic 100% improvement.'),
        ]
        source = self.collect(pick(blocks))
        by_passage = {(entry['role'], entry['anchor']): entry['text'] for entry in source}
        self.assertIn('shared free list', by_passage['mechanism', 'S2.p1'])
        self.assertIn('91 ms to 64 ms', by_passage['results', 'S3.p4'])
        self.assertIn('multiple nodes', by_passage['limits', 'S4.p1'])
        self.assertNotIn('100% improvement', json.dumps(source))
        self.assertLess(len(json.dumps(source)), 15000)

    def test_pdf_pages_without_standalone_headings_keep_late_measured_comparison(self):
        first_page = 'Long context serving motivates this work. ' * 500
        blocks = [
            ('page-1', first_page),
            ('page-6', 'We use a hash map to group requests by prefix length. '
                       'Against vanilla batching, the allocator reduces latency by 31% on ShareGPT. '
                       'We only test data-parallel inference on H100 accelerators.'),
        ]
        source = self.collect(pick(blocks, scope='partial'))
        by_role = {e['role']: e['text'] for e in source if e['anchor'] == 'page-6'}
        self.assertIn('hash map', by_role['mechanism'])
        self.assertIn('31% on ShareGPT', by_role['results'])
        self.assertIn('H100 accelerators', by_role['limits'])

    def test_release_uses_feature_changes_and_compatibility_late_in_changelog(self):
        blocks = [
            ('overview.p1', 'The release includes updates to documentation and formatting. ' * 220),
            ('new-engine.p2', '- Add prefix caching to distributed inference.'),
            ('benchmarks.p1', 'Measured throughput increases to 132 requests/s on the 8-GPU setup.'),
            ('compatibility.p1', 'This release currently only supports Linux with CUDA 12.9.'),
        ]
        source = self.collect(pick(blocks, key='vllm:v0.13.0', title='vLLM v0.13.0'))
        by_anchor = {entry['anchor']: entry['text'] for entry in source}
        self.assertIn('prefix caching', by_anchor['new-engine.p2'])
        self.assertIn('132 requests/s', by_anchor['benchmarks.p1'])
        self.assertIn('CUDA 12.9', by_anchor['compatibility.p1'])


    def test_only_same_version_table_rows_are_sent_with_their_column_headers(self):
        url = 'https://arxiv.org/html/2611.12345v1'
        html = b"""<article><figure id="S3.T1" class="ltx_table">
          <figcaption>Table 1: CoralBench recall and latency.</figcaption>
          <table><tr><th>Method</th><th>Recall (%)</th><th>Latency (ms)</th></tr>
          <tr><td>Baseline</td><td>70.5</td><td>81</td></tr>
          <tr><td>Allocator</td><td>84.9</td><td>63</td></tr></table>
          </figure></article>"""
        article = pick([('abstract', 'We introduce a cache allocation policy.')])
        article['version']['source_url'] = url
        article['brief_panels'] = visuals.extract(html, url)
        tables = [entry for entry in self.collect(article) if entry['role'] == 'table']
        self.assertEqual([table['anchor'] for table in tables], ['S3.T1'])
        self.assertIn('Recall (%): 84.9%', tables[0]['text'])
        self.assertIn('Latency (ms): 63 ms', tables[0]['text'])
        article['brief_panels']['source'] = url[:-1] + '2'
        self.assertNotIn('84.9', json.dumps(self.collect(article)))


class ModelBriefReceipts(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(model='configured-model')
        self.pick = pick([('abstract', 'We introduce a method to group the waiting jobs.')], scope='abstract')

    def test_receipt_uses_the_successful_attempt_and_completed_stream_timing(self):
        calls = 0

        def stream(_config, _messages, *, timing=None):
            nonlocal calls
            calls += 1
            if timing is not None:
                timing['ttft_ms'] = 112 if calls == 1 else 245
            yield '{}' if calls == 1 else json.dumps(ANSWER)
            if timing is not None:
                timing['total_ms'] = 1200 if calls == 1 else 2503

        result = briefs.build_brief(self.pick, briefs.model_generator(self.config, stream))
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['mode'], 'model')
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['model'], 'configured-model')
        self.assertEqual((result['ttft_ms'], result['total_ms']), (245, 2503))

    def test_failed_refresh_records_measured_timeout_without_a_second_dispatch(self):
        calls = 0

        def stream(_config, _messages, *, timing=None):
            nonlocal calls
            calls += 1
            if timing is not None:
                timing['total_ms'] = 15000
            raise ModelStreamError('first_token_timeout')
            yield

        result = briefs.build_brief(self.pick, briefs.model_generator(self.config, stream), max_attempts=1)
        self.assertEqual(calls, 1)
        self.assertEqual(result['state'], 'degraded')
        self.assertEqual(result['mode'], 'abstract_fallback')
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(result['model'], 'configured-model')
        self.assertIsNone(result['ttft_ms'])
        self.assertEqual(result['total_ms'], 15000)
        self.assertIn('逾時', result['error'])

    def test_legacy_stream_with_model_configuration_does_not_invent_timings(self):
        def stream(_config, _messages):
            yield json.dumps(ANSWER)

        result = briefs.build_brief(self.pick, briefs.model_generator(self.config, stream))
        self.assertEqual(result['mode'], 'model')
        self.assertIsNone(result['ttft_ms'])
        self.assertIsNone(result['total_ms'])

    def test_model_output_with_an_unpaired_surrogate_is_rejected_before_caching(self):
        broken = {**ANSWER, 'l2': {**ANSWER['l2'], 'limits': '缺少' + chr(0xd800)}}
        def stream(_config, _messages):
            yield json.dumps(broken)

        result = briefs.build_brief(self.pick, briefs.model_generator(self.config, stream))
        self.assertEqual(result['state'], 'degraded')
        json.dumps(result, ensure_ascii=False).encode('utf-8')

    def test_invented_measured_value_does_not_become_ready(self):
        answer = {**ANSWER, 'l2': {**ANSWER['l2'], 'metrics': '測得平均延遲只剩 21 ms。'}}

        def stream(_config, _messages):
            yield json.dumps(answer)

        result = briefs.build_brief(self.pick, briefs.model_generator(self.config, stream))
        self.assertEqual(result['state'], 'degraded')
        self.assertNotIn('21 ms', result['l2']['metrics'])

    def test_shared_unit_from_arrow_transition_accepts_both_reported_values(self):
        candidate = pick([
            ('prefill.p1', 'Prefill graph memory 15.52 → 9.88 GiB per rank on GB200.'),
            ('benchmarks.p1', 'Decode ITL drops by 3.0–8.8% with breakable CUDA graphs.'),
        ], key='vllm:v0.34.0', title='vLLM v0.34.0')
        for feedback, state in (
            ('Prefill graph memory 從 15.52 GiB 降至 9.88 GiB，decode ITL 降低 3.0–8.8%。', 'ready'),
            ('Prefill graph memory 從 15.52 GB 降至 9.88 GB。', 'degraded'),
            ('Prefill graph memory 從 15.52 GiB 降至 9.87 GiB。', 'degraded'),
            ('Decode ITL 降低 3.0–8.9%。', 'degraded'),
        ):
            with self.subTest(feedback=feedback):
                def stream(_config, _messages):
                    yield json.dumps({**ANSWER, 'l2': {**ANSWER['l2'], 'metrics': feedback}})
                result = briefs.build_brief(candidate, briefs.model_generator(self.config, stream), max_attempts=1)
                self.assertEqual(result['state'], state)

    def test_model_sees_and_returns_plain_notation_for_the_same_measurements(self):
        candidate = pick([(
            'abstract',
            r'Authors report $5.05 \times$ compression, reducing latency by $23.1\%$ and using $5.2\,\mathrm{GiB}$.',
        )], scope='abstract')
        plain_sources = []
        def stream(_config, messages):
            material = json.loads(messages[-1]['content'])['source']
            plain_sources.extend(entry['text'] for entry in material)
            metrics = r'實測達 $5.05 \times$ compression，延遲下降 $23.1\%$，佔用 $5.2\,\mathrm{GiB}$。'
            yield json.dumps({**ANSWER, 'l2': {**ANSWER['l2'], 'metrics': metrics}})
        result = briefs.build_brief(candidate, briefs.model_generator(self.config, stream), max_attempts=1)
        self.assertTrue(any('5.05 ×' in text and '23.1%' in text and '5.2 GiB' in text for text in plain_sources))
        self.assertEqual(result['l2']['metrics'], '實測達 5.05 × compression，延遲下降 23.1%，佔用 5.2 GiB。')

    def test_single_json_backslash_times_is_rejected_before_whitespace_cleanup(self):
        candidate = pick([('abstract', 'The model reports a 5.05× compression ratio.')], scope='abstract')
        def stream(_config, _messages):
            metrics = r'壓縮率達 $5.05 \times$。'
            response = json.dumps({**ANSWER, 'l2': {**ANSWER['l2'], 'metrics': metrics}})
            yield response.replace(r'\\times', r'\times')
        result = briefs.build_brief(candidate, briefs.model_generator(self.config, stream), max_attempts=1)
        self.assertEqual(result['state'], 'degraded')
        self.assertNotIn('imes', result['l2']['metrics'])


class StructuredMetrics(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(model='configured-model', structured=True)
        self.paper = pick([
            ('experiments', '2 Results'),
            ('deep-ood', 'On the OOD split, direct binocular depth supervision reaches δ1 of 45.89%, '
                         'while binocular disparity supervision reaches δ1 of 74.92%.'),
            ('unrelated', 'With extra labels, monocular supervision improves the confidence score to 95.00%.'),
        ], title='Binocular depth')
        self.paper['item']['id'] = 3
        self.paper['version']['id'] = '7'
        self.measurement = '在 OOD 測試集，直接雙目深度監督的 δ1 是 45.89%，改以雙目視差監督後升至 74.92%。'

    def generate(self, candidate, metric, repaired=None):
        requests = []

        def stream(_config, messages, *, response_schema, timing=None):
            public = json.loads(messages[-1]['content'])
            requests.append(public)
            if set(response_schema['properties']) == {'metrics'}:
                yield json.dumps({'metrics': repaired if repaired is not None else metric})
            else:
                schema = response_schema['properties']['l2']['properties']['metrics']
                self.assertEqual(schema['type'], 'string')
                yield json.dumps({**ANSWER, 'l2': {**ANSWER['l2'], 'metrics': metric}}, ensure_ascii=False)

        result = briefs.build_brief(candidate, briefs.model_generator(self.config, stream), max_attempts=1)
        return result, requests

    def test_measurement_preserves_both_methods_condition_indicator_and_same_edition_link(self):
        result, requests = self.generate(self.paper, self.measurement)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['l2']['metrics'], self.measurement)
        self.assertEqual(result['metrics_source']['href'], '/items/3?version=7#deep-ood')
        self.assertEqual(result['metrics_source']['version_id'], '7')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['metric_source']['anchor'], 'deep-ood')

    def test_qualitative_comparisons_cannot_crowd_a_measured_result_out_of_the_model_budget(self):
        title = 'Zero-shot multilingual prompting model alignment'
        candidate = pick([('results', '4 Results'), *[
            (f'comparison-{i}', f'{title} consistently outperforms baseline {name} on task accuracy.')
            for i, name in enumerate(('English', 'German', 'French', 'Italian', 'Spanish'))
        ], ('transitions', 'Refinement leaves 80.0% of predictions unchanged and corrects '
                            '2.0% of the previous errors in held-out prompts.')], title=title)
        metric = '在保留的提示上，精煉後有 80.0% 的預測維持原樣。'
        result, requests = self.generate(candidate, metric)
        self.assertEqual(requests[0]['metric_source']['anchor'], 'transitions')
        self.assertEqual(result['l2']['metrics'], metric)

    def test_long_results_block_keeps_adjacent_method_and_measured_sentences_together(self):
        blocks = self.paper['version']['blocks']
        blocks[1]['text'] = (
            'Compared with monocular supervision, direct binocular depth supervision reaches δ1 of 45.89% on the OOD split. '
            'The alternative training approach is binocular disparity supervision. '
            'On the OOD split that method reaches δ1 of 74.92%, with the hardest scene held out from training. '
            + 'The authors discuss the implications for future work and describe possibilities for additional tests. ' * 6
        )
        self.assertGreater(len(blocks[1]['text']), 650)
        result, requests = self.generate(self.paper, self.measurement)
        self.assertEqual(result['metrics_source']['presentation'], 'summary')
        self.assertIn('45.89%', requests[0]['metric_source']['text'])
        self.assertIn('74.92%', requests[0]['metric_source']['text'])

    def test_measurement_revision_gets_only_the_selected_excerpt_once(self):
        for edited in (self.measurement.replace('74.92%', '95.00%'),
                       self.measurement.replace('δ1', '深度誤差 δ1')):
            with self.subTest(edited=edited):
                result, requests = self.generate(self.paper, edited, self.measurement)
                self.assertEqual(result['state'], 'ready')
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[1]['metric_source'], requests[0]['metric_source'])
                self.assertNotIn('source', requests[1])
                self.assertNotIn('95.00%', json.dumps(requests[1]['metric_source']))
                self.assertEqual(result['l2']['metrics'], self.measurement)

    def test_invalid_revision_keeps_reference_without_long_english_copy(self):
        result, requests = self.generate(self.paper, '原先的準確率為 98.1%。')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(len(requests), 2)
        self.assertNotIn('98.1%', result['l2']['metrics'])
        self.assertNotIn('binocular', result['l2']['metrics'])
        self.assertEqual(result['metrics_source']['href'], '/items/3?version=7#deep-ood')
        self.assertIn('74.92%', result['metrics_source']['quote'])

    def test_formal_metric_in_tex_is_rendered_without_tex_delimiters(self):
        self.paper['version']['blocks'][1]['text'] = self.paper['version']['blocks'][1]['text'].replace(
            'δ1', r'δ1\delta_{1}')
        latex = self.measurement.replace('δ1', r'$\delta_{1}$')
        result, requests = self.generate(self.paper, latex)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['l2']['metrics'], self.measurement)
        self.assertNotIn('\\delta', result['metrics_source']['quote'])

    def test_preexisting_mathml_duplicate_is_read_once_in_the_linked_excerpt(self):
        self.paper['version']['blocks'][1]['text'] = (
            r'In the measured generation test, refinement increases output from 371.7371.7 words '
            r'to 885.1885.1 words, or 2.38×2.38\times as much.')
        result, _ = self.generate(self.paper, '在生成測試裡，精煉後的輸出從 371.7 字增至 885.1 字。')
        quote = result['metrics_source']['quote']
        self.assertIn('371.7 words to 885.1 words, or 2.38×', quote)
        self.assertNotIn('2.38×2.38', quote)
        self.assertEqual(result['metrics_source']['presentation'], 'summary')

    def test_release_metric_does_not_inherit_a_neighboring_bullets_component(self):
        release = pick([('overview.p1',
                         '- **Model Runner V2 is on by default.** '
                         '- **Measured throughput for Step5 on GB200 reaches up to 1.95×.**')],
                       key='vllm:v0.71.0', title='vLLM v0.71.0')
        result, requests = self.generate(
            release, 'Model Runner V2 讓 Step5 在 GB200 上的吞吐量最高達 1.95×。',
            '在 GB200 測試，Step5 的吞吐量最高達 1.95×。',
        )
        self.assertNotIn('Model Runner', requests[0]['metric_source']['text'])
        self.assertEqual(len(requests), 2)
        self.assertEqual(result['l2']['metrics'], '在 GB200 測試，Step5 的吞吐量最高達 1.95×。')

    def test_release_without_measurements_keeps_the_qualitative_brief(self):
        release = pick([('overview', 'This release adds a new configuration mode for distributed inference.')],
                       key='vllm:v0.71.0', title='vLLM v0.71.0')
        result, requests = self.generate(release, '原文未交代')
        self.assertEqual(result['state'], 'ready')
        self.assertIn('未提供實測數據', result['l2']['metrics'])
        self.assertNotIn('metrics_source', result)
        self.assertEqual(len(requests), 1)
