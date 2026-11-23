"""Version-bound evidence and reading guides from local source fixtures."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge_hub import grounding, visuals
from knowledge_hub.sources import parse_arxiv_html
from knowledge_hub.store import KnowledgeStore


PAPER = 'https://arxiv.org/html/2611.01234v1'
PDF = 'https://arxiv.org/pdf/2611.01234v1'
METHOD = 'We propose a retrieval method that compresses the stored documents and verifies surviving candidates.'
LIMIT = 'Our evaluation is limited to static collections and does not cover online updates.'
BASELINE = 'Compared with a standard BM25 baseline, reranking raises recall from 60% to 71% on CoralBench.'
HTML = f"""<article><h1>Document evidence</h1>
<h2 id="method">2 Method</h2>
<p id="p1">{METHOD}</p>
<h2 id="results">3 Evaluation</h2>
<p id="p2">{BASELINE} The paper uses <a href="#S3.T1">Table 1</a> to compare recall.</p>
<figure id="S3.T1" class="ltx_table"><figcaption>Table 1: CoralBench recall.</figcaption>
<table class="ltx_tabular"><tr><th>Method</th><th>Recall</th></tr>
<tr><td>BM25</td><td>60%</td></tr><tr><td>Reranking</td><td>71%</td></tr></table></figure>
<h2 id="limitations">4 Limitations</h2>
<p id="p3">{LIMIT}</p></article>""".encode()


class GroundingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'reading.sqlite3'
        self.store = KnowledgeStore(self.path)
        self.item_id,self.version_id,_ = self.store.upsert_parsed_entry(parse_arxiv_html(HTML,PAPER))
        self.item = self.store.get_item(self.item_id)
        self.edition = self.item['versions'][0]

    def test_chinese_combination_question_requires_both_the_action_and_subject(self):
        entry = parse_arxiv_html(HTML.replace(METHOD.encode(), (
            'FoldStore groups records by their shared references. FoldStore also evaluates random noise.'
        ).encode()), PAPER.replace('v1', 'v2'))
        self.store.upsert_parsed_entry(entry)
        item = self.store.get_item(self.item_id)
        edition = self.store.select_default_version(item['versions'])
        question = 'FoldStore 如何組合記錄？'
        catalog = grounding.make_catalog(item, edition, question)
        evidence = grounding.local_sections(item, edition, question, '', catalog)[0]
        self.assertTrue(evidence['ids'])
        self.assertIn('groups records', catalog[evidence['ids'][0]]['quote'])
        question = 'FoldStore 如何組合量子電路？'
        catalog = grounding.make_catalog(item, edition, question)
        self.assertFalse(grounding.local_sections(item, edition, question, '', catalog)[0]['ids'])

    def test_selected_paragraph_leads_and_cites_original_publisher_anchor(self):
        catalog = grounding.make_catalog(self.item, self.edition, '這篇的做法？', 'p3')
        evidence_id, first = next(iter(catalog.items()))
        self.assertEqual(first['anchor'], 'p3')
        self.assertEqual(first['quote'], LIMIT)
        self.assertEqual(first['source_url'], PAPER + '#p3')
        self.assertEqual(first['href'], f'/items/{self.item_id}?version={self.version_id}#p3')
        verified = grounding.check_section({'kind':'evidence','text':'作者只驗證靜態集合。','ids':[evidence_id]}, catalog)
        self.assertEqual(verified['citations'], [{'id':evidence_id, **first}])
        self.assertFalse(verified['missing'])

    def test_precise_selection_leads_before_other_sentences_at_same_anchor(self):
        fragment = 'compresses the stored documents'
        catalog = grounding.make_catalog(self.item,self.edition,'比較的結果如何？','p1',
                                         selected_quote=fragment)
        first = next(iter(catalog.values()))
        self.assertEqual((first['anchor'],first['quote']),('p1',fragment))
        self.assertTrue(any(cite['quote'] == METHOD for cite in catalog.values()))
        with self.assertRaises(ValueError):
            grounding.make_catalog(self.item,self.edition,anchor='p2',selected_quote=fragment)

    def test_long_selection_is_bounded_without_losing_its_end(self):
        text = ' '.join(f'This is the archived exact citation evidence segment {index}.' for index in range(45))
        self.assertLess(len(text),3000)
        new_url = PAPER[:-1]+'2'
        self.store.upsert_parsed_entry(parse_arxiv_html(
            f'<article><h1>Document evidence</h1><p id="long">{text}</p></article>'.encode(),new_url))
        item = self.store.get_item(self.item_id)
        version = self.store.select_default_version(item['versions'])
        catalog = grounding.make_catalog(item,version,anchor='long',selected_quote=text)
        found = list(catalog.values())[:5]
        self.assertTrue(all(cite['quote'] in text and len(cite['quote'])<=700 for cite in found))
        self.assertTrue(any('segment 44.' in cite['quote'] for cite in found))

    def test_cross_version_reference_ids_are_rejected_even_for_identical_paragraphs(self):
        newer = PAPER[:-1] + '2'
        self.store.upsert_parsed_entry(parse_arxiv_html(HTML,newer))
        item = self.store.get_item(self.item_id)
        latest = self.store.select_default_version(item['versions'])
        current = grounding.make_catalog(item, latest, anchor='p1')
        previous = grounding.make_catalog(item, self.edition, anchor='p1')
        older_id = next(iter(previous))
        self.assertNotIn(older_id,current)
        with self.assertRaises(ValueError):
            grounding.check_section({'kind':'evidence','text':'作者提出檢索方法。','ids':[older_id]}, current)

    def test_rejects_bad_section_schema_missing_claims_and_inserted_urls(self):
        catalog = grounding.make_catalog(self.item, self.edition)
        evidence_id = next(iter(catalog))
        raw = {'kind':'evidence','text':'作者提出檢索方法。','ids':[evidence_id]}
        for changes in (
            {'ids':['E-wrong']}, {'ids':[evidence_id,evidence_id]}, {'ids':[]}, {'ids':[0]}, {'kind':'overview'},
            {'text':'作者提出檢索方法。原文未交代。','ids':[]}, {'text':'原文未交代。'},
            {'text':'詳見 https://other.example/source'}, {'text':'查閱 arxiv.org/abs/9999.12345'},
            {'text':'作者報告' + chr(0xd800)},
            {'text':'請查來源。'*2000}, {'citations':[{'source_url':PAPER}]},
        ):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    grounding.check_section({**raw, **changes},catalog)
        section = grounding.check_section({'kind':'evidence','text':' 原文未交代。 ','ids':[]},catalog)
        self.assertEqual(section, {'kind':'evidence','text':'原文未交代','citations':[],'missing':True})

    def test_anchor_must_exist_and_visuals_must_have_matching_fixed_provenance(self):
        manifest = visuals.extract(HTML,PAPER)
        catalog = grounding.make_catalog(self.item,self.edition,'請讀這張表','S3.T1',manifest)
        first = next(iter(catalog.values()))
        self.assertEqual(first['anchor'],'S3.T1')
        self.assertEqual(first['quote'],'Table 1: CoralBench recall.')
        self.assertEqual(first['source_url'],PAPER+'#S3.T1')
        self.assertTrue(any('71%' in row['quote'] for row in catalog.values() if row['anchor']=='S3.T1'))
        for anchor,panels in [('p999',manifest),('../p1',manifest),('S3.T1',{**manifest,'source':PAPER[:-1]+'2'})]:
            with self.subTest(anchor=anchor):
                with self.assertRaises(ValueError):
                    grounding.make_catalog(self.item,self.edition,anchor=anchor,panels=panels)

    def test_local_visual_evidence_keeps_the_selected_table_and_rejects_unrelated_question(self):
        manifest = visuals.extract(HTML,PAPER)
        anchor = 'S3.T1'
        catalog = grounding.make_catalog(self.item,self.edition,anchor=anchor,panels=manifest)
        raw = grounding.local_sections(self.item,self.edition,'這張表怎麼讀？',anchor,catalog)[0]
        section = grounding.check_section(raw,catalog)
        self.assertEqual({citation['anchor'] for citation in section['citations']},{anchor})
        raw = grounding.local_sections(self.item,self.edition,'A100 的 VRAM 峰值有多少 GB？',anchor,catalog)[0]
        self.assertTrue(grounding.check_section(raw,catalog)['missing'])

    def test_catalog_bounds_total_excerpt_length_and_preserves_selected_body(self):
        paragraphs = ''.join(
            f'<p id="p{n}">We propose phase {n} that compresses documents and verifies surviving candidates. '
            f'This distinct evidence explains retrieval stage {n} using a continuously maintained archive.</p>'
            for n in range(100)
        )
        url = PAPER[:-1]+'2'
        self.store.upsert_parsed_entry(parse_arxiv_html(f'<article><h1>Document evidence</h1>{paragraphs}</article>'.encode(),url))
        item = self.store.get_item(self.item_id)
        version = self.store.select_default_version(item['versions'])
        catalog = grounding.make_catalog(item,version,anchor='p99')
        self.assertEqual(next(iter(catalog.values()))['anchor'],'p99')
        self.assertLessEqual(len(catalog),grounding.MAX_EVIDENCE)
        self.assertLessEqual(sum(len(cite['quote']) for cite in catalog.values()),grounding.MAX_CATALOG_TEXT)
        self.assertTrue(all(cite['source_url'].startswith(url+'#') for cite in catalog.values()))

    def test_a_valid_id_cannot_launder_an_unquoted_measured_value_or_another_unit(self):
        catalog = grounding.make_catalog(self.item,self.edition,anchor='p2')
        ref = next(i for i,e in catalog.items() if '71%' in e['quote'])
        def check(text):
            return grounding.check_section({'kind':'evidence','text':text,'ids':[ref]},catalog)
        self.assertTrue(check('作者報告 recall 為 71％。')['citations'])
        for claim in ('作者報告提升 99%。','作者報告延遲為 71 毫秒。','作者報告提升 71 倍。'):
            with self.assertRaises(ValueError):
                check(claim)

    def test_pdf_visual_citation_uses_actual_original_page(self):
        panel = {'anchor':'page-4-figure-2','kind':'figure','label':'Figure 2','caption':'Figure 2: Method pipeline.',
                 'representation':'pdf_page','page_number':4,'page_url':PDF+'#page=4','rows':[]}
        catalog = grounding.make_catalog(self.item,self.edition,anchor=panel['anchor'],panels=[panel])
        first = next(iter(catalog.values()))
        self.assertEqual(first['source_url'],PDF+'#page=4')
        self.assertEqual(first['href'],f'/items/{self.item_id}?version={self.version_id}#kh-visual-{panel["anchor"]}')
        with self.assertRaises(ValueError):
            grounding.make_catalog(self.item,self.edition,anchor=panel['anchor'],
                                   panels=[{**panel,'page_url':PDF.replace('v1','v2')+'#page=4'}])

    def test_pdf_figure_keeps_its_verified_page_and_bookmark_when_html_recovers(self):
        from knowledge_hub.editions import ArticleEditions, _merge_visuals
        from knowledge_hub.reading import ReadingState
        html = dict(visuals.extract(HTML,PAPER)['panels'][0])
        native = 'page-4-table-1'
        pdf = {**html,'anchor':native,'representation':'pdf_page','page_number':4,'page_url':PDF+'#page=4'}
        service = ArticleEditions(self.store,lambda _: self.fail('network'),lambda _: None)
        def remember(panels):
            return service.remember_panels(self.version_id,{'source':PAPER,'state':'ready','panels':panels})
        remember([pdf])
        merged = remember(_merge_visuals([{**html,'rows':[],'images':[]}],[pdf]))
        for origin in (html['anchor'],native):
            catalog = grounding.make_catalog(self.item,self.edition,anchor=origin,panels=merged)
            self.assertEqual(next(iter(catalog.values()))['source_url'],PDF+'#page=4')
        guide = grounding.cached_guide(self.store,self.item,self.edition,merged)
        self.assertTrue(any(c['source_url']==PDF+'#page=4' for s in guide['sections'] for c in s['citations']))
        current = remember([html])
        self.assertEqual(len(current['panels']),1)
        for origin, url in ((html['anchor'],PAPER+'#'+html['anchor']),(native,PDF+'#page=4')):
            catalog = grounding.make_catalog(self.item,self.edition,anchor=origin,panels=current)
            self.assertEqual(next(iter(catalog.values()))['source_url'],url)
        ReadingState(self.store).save(self.item_id,self.version_id,0,{'anchor':'kh-visual-'+native})

    def test_precise_figure_quote_is_bound_to_its_parsed_table(self):
        manifest = visuals.extract(HTML,PAPER)
        catalog = grounding.make_catalog(self.item,self.edition,anchor='S3.T1',
                                         panels=manifest,selected_quote='Reranking | 71%')
        first = next(iter(catalog.values()))
        self.assertEqual(first['quote'],'Reranking | 71%')
        self.assertEqual(first['source_url'],PAPER+'#S3.T1')
        with self.assertRaises(ValueError):
            grounding.make_catalog(self.item,self.edition,anchor='S3.T1',
                                   panels=manifest,selected_quote='Reranking | 99%')

    def test_unresolved_arxiv_index_cannot_supply_citations(self):
        version = {**self.edition,'label':'hf-index','source_url':'https://huggingface.co/papers/2611.01234'}
        item = {**self.item,'versions':[version]}
        self.assertEqual(grounding.make_catalog(item,version), {})

    def test_mismatched_parsed_paragraph_source_is_left_out(self):
        newer = PAPER[:-1] + '2'
        version = {**self.edition,'blocks':[
            {**block,'source_url':block['source_url'].replace(PAPER,newer)} if block['anchor']=='p1' else block
            for block in self.edition['blocks']
        ]}
        item = {**self.item,'versions':[version]}
        catalog = grounding.make_catalog(item,version,anchor='p1')
        self.assertFalse(any(cite['anchor']=='p1' for cite in catalog.values()))

    def test_local_reader_returns_three_grounded_sections_or_an_explicit_gap(self):
        catalog = grounding.make_catalog(self.item,self.edition,'作者的方法是什麼？','p1')
        raw_sections = grounding.local_sections(self.item,self.edition,'作者的方法是什麼？','p1',catalog)
        sections = [grounding.check_section(section,catalog) for section in raw_sections]
        self.assertEqual([section['kind'] for section in sections], ['evidence','limits','inference'])
        self.assertIn(METHOD, sections[0]['citations'][0]['quote'])
        self.assertIn(LIMIT, sections[1]['citations'][0]['quote'])
        question = 'A100 的 VRAM 峰值有多少 GB？'
        spans = grounding.make_catalog(self.item,self.edition,question)
        evidence = grounding.local_sections(self.item,self.edition,question,'',spans)[0]
        self.assertTrue(grounding.check_section(evidence,spans)['missing'])

    def test_guide_is_persisted_and_another_version_gets_its_own_citations(self):
        manifest = visuals.extract(HTML,PAPER)
        guide = grounding.cached_guide(self.store,self.item,self.edition,manifest)
        self.assertEqual(guide['version_id'],self.version_id)
        self.assertEqual({s['kind'] for s in guide['sections']},set(grounding.KINDS))
        self.assertTrue(any('機制' in section['title'] and section['citations'] for section in guide['sections']))
        self.assertTrue(any('圖表' in section['title'] and section['citations'] for section in guide['sections']))
        self.assertTrue(any('基準' in section['title'] and section['citations'] for section in guide['sections']))
        self.assertTrue(all(section['citations'] or section['text']=='原文未交代' for section in guide['sections']))

        restarted = KnowledgeStore(self.path)
        with patch('knowledge_hub.source_reader.lesson', side_effect=AssertionError('guide was rebuilt')):
            self.assertEqual(guide,grounding.cached_guide(restarted,self.item,self.edition,manifest))
        new_url = PAPER[:-1] + '2'
        restarted.upsert_parsed_entry(parse_arxiv_html(HTML,new_url))
        item = restarted.get_item(self.item_id)
        version = restarted.select_default_version(item['versions'])
        new_guide = grounding.cached_guide(restarted,item,version)
        self.assertNotEqual(guide['version_id'],new_guide['version_id'])
        self.assertTrue(all(cite['source_url'].startswith(new_url) for section in new_guide['sections']
                            for cite in section['citations']))
        self.assertTrue(any('圖表' in section['title'] and section['missing'] for section in new_guide['sections']))


if __name__ == '__main__':
    unittest.main()
