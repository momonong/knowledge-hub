"""Host regressions for observed broken excerpts; no network or private data."""
import unittest
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from types import SimpleNamespace
from knowledge_hub.connections import excerpts
from knowledge_hub.connection_views import render


def version(blocks):
    return {'id':'17','scope':'partial','source_url':'https://example.org/paper',
            'blocks':[{'anchor':a,'text':t,'source_url':'https://example.org/paper#'+a} for a,t in blocks]}


class ReadingQualityAcceptance(unittest.TestCase):
    def test_explanatory_sentence_beats_keyword_dense_bibliography(self):
        method='The retriever encodes each question and passage, then ranks the passages by their vector similarity.'
        refs='References\n'+''.join(f'[{i}] A. Author. 2020. Retriever question passage vector similarity. Journal of Retrieval.\n' for i in range(15))
        v=version([('method',method),('refs',refs)])
        got=excerpts(v,3,{'retriever','question','passage','vector','similarity'})
        self.assertTrue(got)
        self.assertEqual(got[0]['anchor'],'method')
        self.assertFalse(any(x['anchor']=='refs' for x in got))
        self.assertEqual(got[0]['quote'],method)

    def test_complete_late_sentence_keeps_words_and_boundaries(self):
        wanted='RareNeedle identifies a relevant passage after the unrelated introduction, allowing the reader to verify the exact evidence.'
        text=('Background material is unrelated to this question. '*110)+wanted+' The method has limited coverage.'
        got=excerpts(version([('late',text)]),4,{'rareneedle'},'RareNeedle')
        self.assertTrue(got)
        self.assertIn(wanted,got[0]['quote'])
        self.assertIn(got[0]['quote'],text)
        start=text.index(got[0]['quote']);end=start+len(got[0]['quote'])
        self.assertTrue(start==0 or text[:start].rstrip().endswith('.'))
        self.assertTrue(got[0]['quote'].rstrip().endswith('.'))
        self.assertLessEqual(len(got[0]['quote']),700)

    def test_chinese_sentence_boundary_and_reference_tail(self):
        good='檢索器先把問題與段落轉成向量，再用相似度挑選候選段落。生成器仍須核對候選是否支持答案。'
        text=('這段只交代文章背景。'*100)+good+'\n參考文獻\n[1] 作者，2020，檢索器、向量、候選段落。'*7
        got=excerpts(version([('page',text)]),5,{'檢索器','向量','候選段落'},'檢索器')
        self.assertTrue(got)
        self.assertIn('檢索器先把問題與段落轉成向量',got[0]['quote'])
        self.assertNotIn('參考文獻',got[0]['quote'])
        self.assertTrue(got[0]['quote'].endswith('。'))

    def test_conclusion_before_references_is_not_discarded(self):
        good='The retrieval method improves access to relevant evidence, but it can still miss rare topics.'
        text='Conclusion\n'+good+'\n\nReferences\n'+('[1] Evidence retrieval topics. A. Author. 2020.\n'*20)
        got=excerpts(version([('last-page',text)]),6,{'retrieval','evidence','topics'})
        self.assertTrue(got)
        self.assertIn(good,got[0]['quote'])
        self.assertNotIn('References',got[0]['quote'])

    def test_unpunctuated_fragment_is_explicit_and_stays_bounded(self):
        text=' '.join(['unpunctuated']*180)+' RareNeedle '+ ' '.join(['explanation']*120)
        got=excerpts(version([('fragment',text)]),7,{'rareneedle'},'RareNeedle')
        self.assertTrue(got)
        self.assertIn('RareNeedle',got[0]['quote'])
        self.assertEqual(got[0].get('quality'),'fragment')
        self.assertTrue(got[0].get('quality_note'))
        self.assertLessEqual(len(got[0]['quote']),700)
        self.assertIn(got[0]['quote'],text)
        start=text.index(got[0]['quote']);end=start+len(got[0]['quote'])
        self.assertTrue(start==0 or text[start-1].isspace())
        self.assertTrue(end==len(text) or text[end].isspace())

    def test_visible_preview_does_not_cut_first_sentence_at_160(self):
        first='The retriever selects passages that match the question, while the generator must still check whether those passages support the answer and preserve the conditions under which the evidence applies.'
        source={'item_id':2,'version_id':17,'title':'Background paper','scope':'partial','relation_kind':'background','reason':'先理解檢索與回答的差別。','assertion_status':'curator_proposal','concepts':[],'excerpts':[{'anchor':'p1','quote':first+' A second sentence follows.','reading_url':'/items/2?version=17#p1','source_url':'https://example.org/paper#p1'}]}
        service=SimpleNamespace(provider=None,records=lambda i:[],build_context=lambda *a:{'focal':{},'related':[source],'notes':[],'limitations':'partial'})
        page=render(service,1,1,'csrf','',lambda x:x)
        class FirstQuote(HTMLParser):
            def __init__(self):super().__init__();self.active=False;self.seen=False;self.text=''
            def handle_starttag(self,t,a):
                if t=='blockquote' and not self.seen:self.active=True;self.seen=True
                elif t=='br':self.active=False
            def handle_endtag(self,t):
                if t=='blockquote':self.active=False
            def handle_data(self,d):
                if self.active:self.text+=d
        parser=FirstQuote();parser.feed(page)
        self.assertIn(first,parser.text)

    def test_formula_variable_ends_sentence_without_gluing_next_paragraph(self):
        good = 'We use retrieved documents as additional context when generating the target sequence y.'
        got = excerpts(version([('method', good + ' This sentence describes a different topic.')]),
                       8, {'retrieved', 'documents', 'generating'})
        self.assertEqual(got[0]['quote'], good)

    def test_clean_sentence_beats_alternating_pdf_columns(self):
        clean = 'The retriever uses independent encoders to represent questions and passages as vectors.'
        mixed = 'The model uses retrieval\n                                                     The generator uses passages\nand questions from data\n                                                     to produce output.'
        got = excerpts(version([('columns', mixed), ('clean', clean)]), 9,
                       {'model', 'retrieval', 'generator', 'passages', 'questions', 'data'})
        self.assertEqual(got[0]['anchor'], 'clean')
        self.assertTrue(next(e for e in got if e['anchor'] == 'columns')['quality_note'])

    def test_references_continue_across_pages_but_appendix_resumes(self):
        v = version([('p1', 'References\nA. Author. Retrieval methods.'),
                     ('p2', 'Retrieval-augmented language model pre-training.'),
                     ('p3', 'A    Method Details\nWe propose a retriever that ranks passages before generating answers.')])
        got = excerpts(v, 10, {'retrieval', 'model', 'retriever', 'passages'})
        self.assertTrue(got)
        self.assertEqual({e['anchor'] for e in got}, {'p3'})

    def test_unheaded_author_year_entry_is_not_body_evidence(self):
        method = 'The retrieval component selects passages that are relevant to the input question.'
        refs = 'Vladimir Karpukhin, Barlas Oguz, Sewon Min. 2020. Dense Passage Retrieval for Open-Domain Question Answering. In Proceedings of EMNLP.'
        # The method on the same page must survive filtering the entry.
        got = excerpts(version([('mixed', method + '\n\n' + refs)]), 11,
                       {'dense', 'passage', 'retrieval', 'open-domain', 'question', 'answering'}, 'retrieval')
        self.assertEqual([e['quote'] for e in got], [method])

    def test_method_with_author_like_title_and_year_remains_available(self):
        text = 'Dense Passage Retrieval uses encoders, and its first paper appeared in 2020.'
        got = excerpts(version([('method', text)]), 12, {'retrieval', 'encoders'})
        self.assertEqual(got[0]['quote'], text)

    def test_two_line_column_jump_is_marked_even_with_zero_first_indent(self):
        text = 'The retriever encodes questions\n' + ' ' * 53 + 'and the generator produces answers.'
        got = excerpts(version([('columns', text)]), 13, {'retriever', 'generator'})
        self.assertTrue(got[0]['quality_note'])

    def test_equal_frequency_terms_give_same_excerpts_across_processes(self):
        from knowledge_hub.store import KnowledgeStore
        words = ['topic' + chr(97 + i // 26) + chr(97 + i % 26) for i in range(128)]
        body = ' '.join('The method uses ' + ' '.join(words[i:i+8]) + ' to explain the mechanism.'
                        for i in range(0, 128, 8))
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'hub.sqlite3'
            store = KnowledgeStore(db)
            item, revision, _ = store.upsert_parsed_entry({
                'key': 'deterministic-fixture', 'source_name': 'manual', 'title': 'Method selection',
                'label': 'v1', 'scope': 'partial', 'source_url': 'https://example.org/method',
                'content_hash': hashlib.sha256(body.encode()).hexdigest(),
                'blocks': [{'anchor': 'p1', 'text': body, 'source_url': 'https://example.org/method'}]})
            script = ('import json,sys; from knowledge_hub.store import KnowledgeStore; '
                      'from knowledge_hub.connections import ConnectionService; '
                      'print(json.dumps(ConnectionService(KnowledgeStore(sys.argv[1])).build_context('
                      'int(sys.argv[2]),sys.argv[3])["focal"]["excerpts"],sort_keys=True))')
            outputs = [subprocess.check_output([sys.executable, '-B', '-c', script, str(db), str(item), str(revision)],
                       env={**os.environ, 'PYTHONHASHSEED': seed}, cwd=Path(__file__).resolve().parents[1])
                       for seed in ['1', '2']]
            self.assertEqual(outputs[0], outputs[1])


if __name__=='__main__':unittest.main()
