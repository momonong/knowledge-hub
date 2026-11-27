from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from knowledge_hub import guided
except ImportError:
    guided = None
from knowledge_hub import visuals
from knowledge_hub.captures import CaptureStore
from knowledge_hub.editions import ArticleEditions
from knowledge_hub.followup import Followups
from knowledge_hub.library import KnowledgeLibrary
from knowledge_hub.model_stream import ModelConfig, ModelStreamError
from knowledge_hub.sources import parse_arxiv_html
from knowledge_hub.store import KnowledgeStore


SOURCE = 'https://arxiv.org/html/2611.01234v2'
HTML = b"""<article><h1>TideCache indexing study</h1>
<h2 id="overview">1 Introduction</h2>
<p id="context">Conventional eviction loses reusable keys when workload shifts across sessions.</p>
<h2 id="method">2 Method</h2>
<p id="approach">We propose a segmented gate that compresses stored documents and verifies surviving candidates.</p>
<h2 id="results">3 Evaluation</h2>
<p id="measure">Compared with a BM25 baseline, reranking raises recall from 60% to 71% on CoralBench.</p>
<figure id="S3.T1" class="ltx_table"><figcaption>Table 1: CoralBench recall.</figcaption>
<table class="ltx_tabular"><tr><th>Method</th><th>Recall</th></tr>
<tr><td>BM25</td><td>60%</td></tr><tr><td>Reranking</td><td>71%</td></tr></table></figure>
<h2 id="limitations">4 Limitations</h2>
<p id="limit">Our evaluation is limited to static collections and does not cover online updates.</p>
</article>"""


class GuidedRoomTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(guided, 'The v1.1 model guide is missing')
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'reading.db'
        self.store = KnowledgeStore(self.db)
        self.item_id, self.version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(HTML, SOURCE))
        self.primary = ModelConfig('http://127.0.0.1:8489/v1', 'fixture-primary')
        self.fallback = ModelConfig('http://127.0.0.1:8490/v1', 'fixture-fallback')
        self.routes = {'primary': self.primary, 'fallback': self.fallback}

    def run_guide(self, **kwargs):
        return guided.guide(self.store, self.item_id, self.version_id, self.routes, **kwargs)

    def remember_visuals(self, html=HTML, url=SOURCE):
        editions = ArticleEditions(self.store, lambda _: self.fail('The guide must not fetch by itself'), lambda _: None)
        editions.remember_panels(self.version_id, {**visuals.extract(html, url), 'state': 'ready'})

    def author_rows(self, messages):
        payload = json.loads(messages[-1]['content'])
        evidence = payload['evidence']

        def ref(needle):
            return next(item['id'] for item in evidence if needle in item['quote'])

        rows = [
            {'type': 'hook', 'text': '工作負載換了階段時，傳統 eviction 容易丟掉下一階段可重用的 key。',
             'ids': [ref('Conventional eviction')]},
            {'type': 'mechanism', 'text': '先用 segmented gate 壓縮已存文件，再逐一核對保留下來的候選。',
             'check': {'question':'被閘門淘汰的文件還能參加重排嗎？','answer':'閘門只把保留下來的候選交給重排。'},
             'ids': [ref('segmented gate')]},
            {'type': 'evidence', 'text': 'CoralBench 比較中，重排把 recall 從 60% 提升到 71%。',
             'ids': [ref('CoralBench.')]},
            {'type': 'limits', 'text': '評估限於 static collections，線上更新尚未驗證。',
             'ids': [ref('static collections')]},
            {'type': 'chips', 'questions': [
                {'role': 'mechanism', 'focus': 'segmented gate', 'question': 'segmented gate 如何核對留下的候選？',
                 'id': ref('segmented gate')},
                {'role': 'evidence', 'focus': 'BM25', 'question': 'BM25 對照中，提升的是哪項評量？',
                 'id': ref('BM25 baseline')},
                {'role': 'boundary', 'focus': 'static collections', 'question': '離開 static collections 後，本文有驗證線上更新嗎？',
                 'id': ref('static collections')},
            ]},
        ]
        if payload['visuals']:
            panel = payload['visuals'][0]
            resolved = next(item for item in evidence if item['id'] == panel['ids'][0])
            self.assertIn('CoralBench', resolved['quote'])
            rows.insert(2, {'type': 'visual', 'panel': panel['id'], 'focus': 'CoralBench',
                            'text': '先找 CoralBench 的 recall，比較 BM25 和重排兩列。',
                            'ids': panel['ids']})
        return rows

    def transport(self, _config, messages, *, timing, **_kwargs):
        timing.update(ttft_ms=127.5, first_line_ms=270.0, total_ms=982.0)
        yield from map(json.dumps, self.author_rows(messages))

    def test_model_guide_cites_fixed_edition_and_reopens_same_cache_for_capture(self):
        self.remember_visuals()
        self.store.add_note(self.item_id, self.version_id, 'approach', 'PRIVATE_DRAFT_SENTINEL')
        with patch.object(guided, 'stream_model', side_effect=self.transport) as send:
            result = self.run_guide()
            with patch.object(guided, '_catalog', side_effect=AssertionError('Cached guide re-extracted the article')):
                restored = guided.guide(KnowledgeStore(self.db), self.item_id, self.version_id, self.routes)
        self.assertEqual(result['mode'], 'model', result.get('model'))
        self.assertEqual(result['model']['key'], 'fixture-primary')
        self.assertEqual(result['model']['ttft_ms'], 127.5)
        self.assertEqual(result['model']['attempt_timings'][0]['total_ms'], 982.0)
        self.assertGreater(result['model']['total_ms'], 0)
        self.assertEqual(result['sections'], restored['sections'])
        self.assertTrue(restored['model']['cache_hit'])
        send.assert_called_once()
        provider_input = send.call_args.args[1]
        self.assertNotIn('PRIVATE_DRAFT_SENTINEL', str(provider_input))
        self.assertNotIn('https://', provider_input[-1]['content'])

        roles = [section['role'] for section in result['sections']]
        self.assertEqual(roles, ['hook', 'mechanism', 'visual', 'evidence', 'limits'])
        self.assertEqual([question['role'] for question in result['chips']], ['mechanism', 'evidence', 'boundary'])
        card = result['visual']
        self.assertEqual((card['anchor'], card['panel_kind']), ('S3.T1', 'table'))
        self.assertEqual(card['href'], f'/items/{self.item_id}?version={self.version_id}#kh-visual-S3.T1')
        self.assertEqual(card['source_url'], SOURCE + '#S3.T1')
        self.assertEqual(card['label'], '表 1')
        captures = CaptureStore(self.store, Followups(self.store))
        reminder = captures.act(self.store.get_item(self.item_id), {'id': self.version_id}, {
            'action': 'tag', 'request_id': 'figure-capture-1', 'tag': '#想法',
            'anchor': 'S3.T1', 'quote': card['text'],
        })
        self.assertEqual(reminder['href'], card['href'])

    def test_absent_and_wrong_edition_visuals_are_not_invented(self):
        self.remember_visuals(url=SOURCE[:-1] + '3')
        with patch.object(guided, 'stream_model', side_effect=self.transport) as send:
            result = self.run_guide()
        self.assertEqual(result['mode'], 'model')
        self.assertIsNone(result['visual'])
        self.assertEqual(json.loads(send.call_args.args[1][-1]['content'])['visuals'], [])

    def test_offline_reader_receives_same_lesson_and_publisher_anchors_without_private_fields(self):
        self.remember_visuals()
        with patch.object(guided, 'stream_model', side_effect=self.transport):
            guide = self.run_guide()
        packet = guided.portable_lesson(guide)
        self.assertEqual(packet['version'], str(self.version_id))
        lesson = packet['lesson']
        mechanism = next(step for step in guide['sections'] if step['role'] == 'mechanism')
        self.assertEqual(lesson['check'], mechanism['check'])
        self.assertIn(SOURCE + '#approach', lesson['sections'][1]['links'])
        self.assertEqual(lesson['sections'][1]['text'], mechanism['text'])
        for mobile, section in zip(lesson['sections'], guide['sections'], strict=True):
            self.assertEqual(mobile['passages'], [
                {'quote': cite['quote'], 'url': cite['source_url']} for cite in section['citations']
            ])
        self.assertEqual(lesson['questions'], [
            {'role': chip['role'], 'question': chip['question']} for chip in guide['chips']
        ])
        self.assertEqual([q['role'] for q in lesson['questions']], ['mechanism', 'evidence', 'boundary'])
        self.assertNotIn('canonical_id', json.dumps(packet))
        for change in ({'scope':'abstract'}, {'format':5}, {'mode':'local'}):
            self.assertIsNone(guided.portable_lesson({**guide, **change})['lesson'])

    def test_mechanism_includes_nearby_variable_definition_and_preserves_its_anchor(self):
        html = HTML.replace(b'We propose a segmented gate that compresses stored documents and verifies surviving candidates.</p>',
                            b'We compute a gate score from the query and each document. The gate score determines which candidates survive for verification.</p>'
                            b'<p id="scale">Here, the gate score is in log units and the threshold is a calibrated margin measured on the held-out corpus.</p>')
        _, version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(html, SOURCE))
        item, version = Followups(self.store).public_edition(self.item_id, str(version_id))
        catalog, evidence, figures = guided._catalog(item, version, {})
        definition = guided._tasks(evidence, figures)['mechanism']['definition']
        self.assertIn('calibrated margin', definition['quote'])
        self.assertEqual(catalog[definition['id']]['source_url'], SOURCE + '#scale')

    def test_publisher_fraction_and_signed_math_duplicates_keep_the_displayed_measurement(self):
        self.assertEqual(guided._wire_quote('74.38% (90/12190/121) and +3.2+3.2 points'),
                         '74.38% (90/121) and +3.2 points')

    def test_caption_without_renderable_evidence_is_omitted(self):
        href = b'<figure id="S3.T1" class="ltx_table"><figcaption>Table 1: CoralBench recall.</figcaption></figure>'
        start = HTML.index(b'<figure')
        end = HTML.index(b'</figure>') + len(b'</figure>')
        self.remember_visuals(HTML[:start] + href + HTML[end:])
        with patch.object(guided, 'stream_model', side_effect=self.transport) as send:
            result = self.run_guide()
        self.assertEqual(result['mode'], 'model')
        self.assertIsNone(result['visual'])
        self.assertEqual(json.loads(send.call_args.args[1][-1]['content'])['visuals'], [])

    def test_translated_visual_and_chips_use_separate_literal_publisher_focus(self):
        self.remember_visuals()
        for index, (focus, visual_text) in enumerate((
            ('CoralBench recall', '先固定同一資料集，再在同一欄比較原基準與重新排序後的回想率。'),
            ('Relative changes (%)% INVENTED_FOCUS', '先找回想率指標，再看兩個方法各自對應的資料列。'),
            (None, '請先讀表頭，確認各列是在比較方法還是評量分數。'),
        )):
            def translated(_config, messages, **_kwargs):
                rows = self.author_rows(messages)
                card = next(row for row in rows if row['type'] == 'visual')
                card['text'] = visual_text
                if focus is not None:
                    card['focus'] = focus
                else:
                    del card['focus']
                questions = rows[-1]['questions']
                questions[0]['question'] = '分段閘門如何檢查保留下來的候選文件？'
                questions[0]['focus'] = 'PRIVATE_FORGED_FOCUS'
                questions[1]['question'] = '作者用原始排序當基準時，主要在哪個指標上比較？'
                del questions[1]['focus']
                yield from map(json.dumps, rows)

            with self.subTest(focus=focus):
                # Separate public route identity for each provider response,
                # so a valid previous guide cannot mask this fixture.
                self.routes['primary'] = ModelConfig(self.primary.base_url, 'fixture-' + str(index))
                with patch.object(guided, 'stream_model', side_effect=translated):
                    result = self.run_guide()
                self.assertEqual(result['mode'], 'model', result['model'])
                card = result['visual']
                self.assertEqual(card['text'], visual_text)
                self.assertTrue(any(card['focus'] in cite['quote'] for cite in card['citations']))
                self.assertNotIn('INVENTED_FOCUS', str(result))
                self.assertNotIn('PRIVATE_FORGED_FOCUS', str(result))
                for chip in result['chips']:
                    self.assertTrue(chip['focus'])
                    self.assertIn(chip['focus'], chip['citations'][0]['quote'])

    def test_private_attachment_does_not_reach_model_even_with_same_publisher_url(self):
        KnowledgeLibrary(self.store)
        with self.store._connect() as conn:
            conn.execute('INSERT INTO library_documents VALUES (?,?,?,?,?,?,?)', (
                'fixture-private', 'a' * 64, 'text/html', self.item_id, self.version_id, '{}', '2026-11-23',
            ))
        with patch.object(guided, 'stream_model') as send:
            result = self.run_guide()
        self.assertEqual(result['mode'], 'local_source')
        self.assertEqual(result['model']['reason'], 'source_not_public')
        send.assert_not_called()

    def test_wrong_citations_do_not_enter_cache_and_operator_can_retry(self):
        def corrupt(_config, messages, **_kwargs):
            rows = self.author_rows(messages)
            rows[0]['ids'] = ['invented-anchor']
            yield from map(json.dumps, rows)

        with patch.object(guided, 'stream_model', side_effect=corrupt) as send:
            failed = self.run_guide()
            self.assertEqual(self.run_guide()['model']['reason'], 'invalid_citation')
            send.assert_called_once()
        self.assertEqual((failed['mode'], failed['model']['state'], failed['model']['reason']),
                         ('local_source', 'failed', 'invalid_citation'))
        with patch.object(guided, 'stream_model', side_effect=self.transport):
            retried = self.run_guide(retry=True)
        self.assertEqual(retried['mode'], 'model')

    def test_altered_numbers_visual_ids_and_generic_chip_text_fail_closed(self):
        self.remember_visuals()

        def forged(field, _config, messages, **_kwargs):
            rows = self.author_rows(messages)
            if field == 'numbers':
                next(row for row in rows if row['type'] == 'evidence')['text'] = '回想率由 60% 變成 99%。'
            elif field == 'visual':
                next(row for row in rows if row['type'] == 'visual')['panel'] = 'V999'
            elif field == 'cross-panel-cite':
                card = next(row for row in rows if row['type'] == 'visual')
                card['ids'] = next(row['ids'] for row in rows if row['type'] == 'mechanism')
            elif field == 'visual-number':
                next(row for row in rows if row['type'] == 'visual')['text'] = '先查看表 99 的數據。'
            else:
                rows[-1]['questions'][0]['question'] = '這篇論文的內容是什麼？'
            yield from map(json.dumps, rows)

        for field in ('numbers', 'visual', 'cross-panel-cite', 'visual-number', 'unbound-question'):
            with self.subTest(field=field), patch.object(guided, 'stream_model',
                    side_effect=lambda *args, **kwargs: forged(field, *args, **kwargs)):
                result = self.run_guide(retry=True)
                self.assertEqual(result['mode'], 'local_source')
                self.assertIn(result['model']['reason'], ('invalid_response', 'invalid_citation'))

    def test_provider_and_visual_changes_invalidate_model_cache(self):
        with patch.object(guided, 'stream_model', side_effect=self.transport) as send:
            self.assertEqual(self.run_guide()['mode'], 'model')
            self.remember_visuals()
            self.assertIsNotNone(self.run_guide()['visual'])
            self.routes['primary'] = ModelConfig('http://127.0.0.1:8491/v1', 'fixture-primary')
            self.assertIsNotNone(self.run_guide()['visual'])
            self.assertEqual(self.run_guide(route='fallback')['model']['key'], 'fixture-fallback')
        self.assertEqual(send.call_count, 4)

    def test_running_request_does_not_dispatch_a_duplicate(self):
        ready, finish = Event(), Event()

        def slow(config, messages, **kwargs):
            ready.set()
            if not finish.wait(5):
                raise ModelStreamError('first_token_timeout')
            yield from self.transport(config, messages, **kwargs)

        with patch.object(guided, 'stream_model', side_effect=slow) as send, ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.run_guide)
            try:
                self.assertTrue(ready.wait(3))
                self.assertEqual(self.run_guide()['model']['state'], 'running')
            finally:
                finish.set()
            self.assertEqual(first.result(timeout=3)['mode'], 'model')
            send.assert_called_once()

    def enable_schema(self):
        self.routes['primary'] = SimpleNamespace(base_url=self.primary.base_url, model=self.primary.model, structured=True)

    def structured_answer(self, messages):
        tasks = json.loads(messages[-1]['content'])['tasks']
        sections = {
            'hook':'傳統淘汰程序在工作負載切換時容易丟失可重用的鍵。',
            'mechanism':'先用分段閘門壓縮已存文件，再核對保留下來的候選。',
            'evidence':'在 CoralBench，重排回想率從 60% 提升至 71%。',
            'limits':'評估限於靜態集合，尚未涵蓋線上更新。',
            'visual':'先確定比較使用同一套資料，再在回想率欄定位原基準與重排。',
        }
        answer = {
            role: ({'missing': True} if task is None else {'id':task['id'],'text':sections[role]})
            for role, task in tasks.items() if role != 'chips'
        }
        if 'mechanism' in tasks:
            answer['mechanism']['check'] = {'question':'被閘門淘汰的文件還能參加重排嗎？',
                                        'answer':'重排只會讀到閘門保留下來的候選。'}
        if 'visual' in tasks:
            answer['visual']['panel'] = tasks['visual']['panel']
        questions = {
            'mechanism':'分段閘門如何核對留下的文件候選？',
            'evidence':'用原始排名當基準時，重排改善的是哪項評量？',
            'boundary':'離開靜態集合後，本文有驗證線上更新嗎？',
        }
        if 'chips' in tasks:
            answer['chips'] = {
                key:{'id':target['id'],'question':questions[key]} for key,target in tasks['chips'].items()
            }
        return answer

    def test_walkthrough_can_take_two_steps_without_borrowing_a_second_source(self):
        self.enable_schema()
        walkthrough = '先看閘門怎麼分段保存文件。接著沿著留下的文件檢查候選排序。'
        def model(_config, messages, **_kwargs):
            answer = self.structured_answer(messages)
            answer['mechanism']['text'] = walkthrough
            yield json.dumps(answer)
        with patch.object(guided, 'stream_model', side_effect=model):
            result = self.run_guide()
        self.assertEqual(result['mode'], 'model')
        mechanism = next(section for section in result['sections'] if section['role'] == 'mechanism')
        self.assertEqual(mechanism['text'], walkthrough)
        self.assertEqual(len(mechanism['citations']), 1)

    def test_structured_provider_receives_a_single_closed_schema_with_fixed_aliases(self):
        self.enable_schema()
        self.remember_visuals()

        def model(_config, messages, *, response_schema, **_kwargs):
            self.assertEqual(response_schema['type'], 'object')
            self.assertFalse(response_schema['additionalProperties'])
            self.assertEqual(response_schema['required'], ['hook','mechanism','visual','evidence','limits','chips'])
            properties = response_schema['properties']
            first = properties['visual']['properties']
            payload = json.loads(messages[-1]['content'])['tasks']
            self.assertEqual(first['panel']['enum'], [payload['visual']['panel']])
            self.assertEqual(first['id']['enum'], [payload['visual']['id']])
            self.assertEqual(properties['hook']['required'], ['id','text'])
            self.assertEqual(properties['hook']['properties']['id']['enum'], [payload['hook']['id']])
            self.assertEqual(properties['chips']['required'], ['mechanism','evidence','boundary'])
            self.assertIn('CoralBench', payload['evidence']['quote'])
            yield json.dumps(self.structured_answer(messages))

        with patch.object(guided, 'stream_model', side_effect=model) as send:
            result = self.run_guide()
        self.assertEqual(result['mode'], 'model', result['model'])
        self.assertEqual(len(result['chips']), 3)
        self.assertEqual(result['model']['attempts'], 1)
        send.assert_called_once()

    def test_structured_repairs_an_invalid_cited_result_once_under_shared_deadline(self):
        self.enable_schema()
        remaining = []

        def model(_config, messages, *, response_schema, total_timeout, timing, **_kwargs):
            remaining.append(total_timeout)
            answer = self.structured_answer(messages)
            timing.update(ttft_ms=35.0, total_ms=100.0)
            if len(remaining) == 1:
                answer['evidence']['text'] = '表 99 顯示召回率由 60% 提升至 99%。'
            else:
                self.assertEqual(set(json.loads(messages[-1]['content'])['tasks']), {'evidence'})
                self.assertIn('invalid_citation', messages[-1]['content'])
                self.assertNotIn('99%', messages[-1]['content'])
            yield json.dumps(answer)

        with patch.object(guided, 'stream_model', side_effect=model):
            result = self.run_guide()
        self.assertEqual(result['mode'], 'model', result['model'])
        self.assertEqual(result['model']['attempts'], 2)
        self.assertEqual(result['model']['repair_reason'], 'invalid_citation')
        self.assertEqual(result['model']['ttft_ms'], 35.0)
        self.assertLess(remaining[1], remaining[0])
        self.assertLessEqual(remaining[0], 45)
        self.assertNotIn('99%', str(result['sections']))

    def test_no_repair_is_sent_when_transport_dies_with_unverified_partial_answer(self):
        self.enable_schema()

        def model(*_args, **_kwargs):
            yield '{"hook":{}}'
            raise ModelStreamError('total_timeout')

        with patch.object(guided, 'stream_model', side_effect=model) as send:
            result = self.run_guide()
        self.assertEqual(result['model']['reason'], 'total_timeout')
        self.assertEqual(result['model']['attempts'], 1)
        send.assert_called_once()

    def test_available_quantitative_outcome_requires_a_cited_result_in_the_guide(self):
        self.enable_schema()

        def model(_config, messages, **_kwargs):
            rows = self.structured_answer(messages)
            rows['evidence']['text'] = '作者比較了兩種方法。'
            yield json.dumps(rows)

        with patch.object(guided, 'stream_model', side_effect=model) as send:
            result = self.run_guide()
        self.assertEqual(result['mode'], 'local_source')
        self.assertEqual(result['model']['reason'], 'missing_quantitative_result')
        self.assertEqual(result['model']['attempts'], 2)
        self.assertEqual(send.call_count, 2)

    def test_main_result_keeps_its_dataset_and_numeric_table_rows_win_budget(self):
        self.remember_visuals()
        with patch.object(guided, 'stream_model', side_effect=self.transport) as send:
            self.assertEqual(self.run_guide()['mode'], 'model')
        payload = json.loads(send.call_args.args[1][-1]['content'])
        rows = payload['evidence']
        text_rows = [r for r in rows if r.get('quantitative') and r['role'] == 'results']
        self.assertTrue(any('CoralBench' in r['quote'] and '71%' in r['quote'] for r in text_rows))
        table_rows = [r for r in rows if r.get('quantitative') and r['role'] == 'table']
        self.assertTrue(any('Reranking | 71%' in r['quote'] for r in table_rows))

    def test_matched_intervention_beats_numeric_model_setup_as_an_outcome(self):
        comparison = ('Removing the learned update increases error by a factor of 17, '
                      'whereas a random update of the same magnitude increases it by only 2.1×.')
        html = HTML.replace(b'Compared with a BM25 baseline, reranking raises recall from 60% to 71% on CoralBench.',
                            comparison.encode()).replace(b'CoralBench recall.', b'Models used during evaluation.')
        html = html.replace(b'<th>Recall</th>', b'<th>Hidden width</th>').replace(b'60%', b'512').replace(b'71%', b'1024')
        _, self.version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(html, SOURCE))
        self.remember_visuals(html)
        item, version = Followups(self.store).public_edition(self.item_id, self.version_id)
        panels = guided._renderable(item, version, guided._panels(self.store, self.version_id))
        _, entries, figures = guided._catalog(item, version, panels)
        result = guided._tasks(entries, figures)['evidence']
        self.assertEqual(result['quote'], comparison)
        table = [entry for entry in entries if 'Reranking | 1024' in entry['quote']]
        self.assertTrue(table)
        self.assertFalse(any(entry['quantitative'] for entry in table))

    def test_measurement_table_after_two_setup_tables_keeps_an_unfamiliar_metric(self):
        def table(anchor, caption, header, value):
            return (f'<figure id="{anchor}" class="ltx_table"><figcaption>{caption}</figcaption>'
                    f'<table class="ltx_tabular"><tr><th>Model</th><th>{header}</th></tr>'
                    f'<tr><td>TideCache</td><td>{value}</td></tr></table></figure>').encode()
        start, end = HTML.index(b'<figure'), HTML.index(b'</figure>') + len(b'</figure>')
        html = HTML[:start] + b''.join((
            table('S3.T1', 'Table 1: Models used for evaluation.', 'Hidden width', 512),
            table('S3.T2', 'Table 2: Checkpoints used.', 'Layers', 12),
            table('S3.T3', 'Table 3: Held-out observations.', 'ZQ index', 73),
        )) + HTML[end:]
        _, self.version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(html, SOURCE))
        self.remember_visuals(html)
        item, version = Followups(self.store).public_edition(self.item_id, self.version_id)
        panels = guided._renderable(item, version, guided._panels(self.store, self.version_id))
        rows = [(cite['quote'], measured) for (_, cite), measured in guided._table_quotes(item, version, panels)]
        self.assertIn(('TideCache | 73', True), rows)
        self.assertNotIn(('TideCache | 512', True), rows)

    def test_evidence_window_rejects_other_comparison_values_under_the_same_citation(self):
        html = HTML.replace(b'on CoralBench.', b'on CoralBench, but decreases from 58% to 49% on alternate data.')
        _, self.version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(html, SOURCE))
        self.enable_schema()

        def model(_config, messages, **_kwargs):
            task = json.loads(messages[-1]['content'])['tasks']['evidence']
            self.assertIn('71%', task['quote'])
            self.assertNotIn('49%', task['quote'])
            rows = self.structured_answer(messages)
            rows['evidence']['text'] = '在另一組資料上，回想率由 58% 降至 49%。'
            yield json.dumps(rows)

        with patch.object(guided, 'stream_model', side_effect=model):
            result = self.run_guide()
        self.assertEqual(result['mode'], 'local_source')
        self.assertEqual(result['model']['reason'], 'invalid_citation')

    def test_duplicated_tex_values_are_simplified_for_model_without_rewriting_the_citation(self):
        html = HTML.replace(b'60% to 71%', b'60%60\\% to 71%71\\%')
        _, self.version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(html, SOURCE))
        self.enable_schema()

        def model(_config, messages, **_kwargs):
            task = json.loads(messages[-1]['content'])['tasks']['evidence']
            self.assertIn('60% to 71%', task['quote'])
            self.assertNotIn('71%71', task['quote'])
            yield json.dumps(self.structured_answer(messages))

        with patch.object(guided, 'stream_model', side_effect=model):
            result = self.run_guide()
        self.assertEqual(result['mode'], 'model', result['model'])
        evidence = next(section for section in result['sections'] if section['role'] == 'evidence')
        self.assertIn('71%71', evidence['citations'][0]['quote'])

    def test_metric_from_one_model_is_not_attributed_to_a_different_named_model(self):
        html = HTML.replace(b'<h2 id="limitations">', b'<p id="other-model">AltBackbone serves as an independent benchmark for sparse inputs.</p><h2 id="limitations">')
        _, self.version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(html, SOURCE))
        self.enable_schema()

        def model(_config, messages, **_kwargs):
            rows = self.structured_answer(messages)
            rows['evidence']['text'] = '在 AltBackbone 評估上，回想率由 60% 提升到 71%。'
            yield json.dumps(rows)

        with patch.object(guided, 'stream_model', side_effect=model):
            result = self.run_guide()
        self.assertEqual(result['mode'], 'local_source')
        self.assertEqual(result['model']['reason'], 'invalid_citation')

    def test_transport_error_discloses_only_classification_and_no_implicit_route_switch(self):
        def fail(*_args, **_kwargs):
            raise ModelStreamError('first_token_timeout')

        with patch.object(guided, 'stream_model', side_effect=fail) as send:
            result = self.run_guide()
        self.assertEqual(result['model']['reason'], 'first_token_timeout')
        self.assertEqual(result['model']['route'], 'primary')
        send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
