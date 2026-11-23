"""Mounted article visuals use fixed public sources and stay behind reader auth."""
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest
from wsgiref.util import setup_testing_defaults

from knowledge_hub.hosting import MountedApplication
from knowledge_hub.sources import parse_arxiv_html
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app


PAPER = 'https://arxiv.org/html/2601.01234v1'
HTML = b'''<article><h1>Evidence in memory retrieval</h1>
<p id="p1">We compare recall using <a href="#S2.T1">Table 1</a> on the same dataset.</p>
<figure id="S2.F1" class="ltx_figure"><img src="2601.01234v1/ranking.png">
<img src="https://example.org/leak.png"><figcaption>Figure 1: Candidates after reranking.</figcaption></figure>
<figure id="S2.T1" class="ltx_table"><figcaption>Table 1: Recall on Dataset A.</figcaption>
<table class="ltx_tabular"><tr><th colspan="2">Dataset A</th></tr><tr><td>Method</td><td>Recall</td></tr>
<tr><td>Baseline</td><td>60</td></tr><tr><td>Reranking</td><td>71</td></tr></table></figure>
<table class="ltx_equation"><tr><td>equation</td></tr></table></article>'''
PNG = b'\x89PNG\r\n\x1a\n'


class VisualEvidenceTest(unittest.TestCase):
    def test_fixed_version_extracts_source_figure_table_and_author_reference(self):
        from knowledge_hub.visuals import extract
        result = extract(HTML, PAPER)
        self.assertEqual([row['kind'] for row in result['panels']], ['figure', 'table'])
        figure, table = result['panels']
        self.assertEqual(figure['images'], [PAPER+'/ranking.png'])
        self.assertEqual(figure['caption'], 'Figure 1: Candidates after reranking.')
        self.assertEqual(table['rows'][0][0], {'text':'Dataset A','header':True,'colspan':2,'rowspan':1})
        self.assertEqual(table['rows'][-1][-1]['text'], '71')
        self.assertEqual(table['references'][0]['anchor'], 'p1')
        self.assertIn('same dataset', table['references'][0]['text'])

    def test_protected_article_link_and_asset_stay_on_fixed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'knowledge.sqlite3'
            item,version,_=KnowledgeStore(db).upsert_parsed_entry(parse_arxiv_html(HTML,PAPER))
            calls=[]
            def fetch(url):
                calls.append(url)
                return {PAPER:HTML,PAPER+'/ranking.png':PNG}[url]
            base=create_app(db,fetch=fetch)
            allow=True
            app=MountedApplication(base,origin='https://momonong.me',prefix='/knowledge',authorize=lambda env:allow)
            path=f'/knowledge/items/{item}'
            def get(route,query=f'version={version}'):
                env={};setup_testing_defaults(env)
                env.update(PATH_INFO=route,QUERY_STRING=query,HTTP_HOST='momonong.me',HTTP_X_FORWARDED_PROTO='https',REMOTE_ADDR='127.0.0.1')
                result={}
                def start(status,headers):result.update(status=int(status.split()[0]),headers=dict(headers))
                return result,b''.join(app(env,start))
            result, body=get(path)
            self.assertEqual(result['status'],HTTPStatus.OK)
            self.assertIn(b'data-visual-evidence',body)
            self.assertEqual(calls,[],'Opening the article must not wait for arXiv')
            allow=False
            self.assertEqual(get(path+'/visuals')[0]['status'],401)
            self.assertEqual(calls,[])
            allow=True
            self.assertEqual(get(path+'/visuals',query='version=9999')[0]['status'],404)
            result,body=get(path+'/visuals')
            self.assertEqual(result['status'],200)
            self.assertIn(b'S2.T1',body)
            self.assertNotIn(b'example.org',body)
            result,data=get(path+'/visuals/0/0')
            self.assertEqual((result['status'],result['headers']['Content-Type'],data),(200,'image/png',PNG))
            self.assertEqual(calls,[PAPER,PAPER+'/ranking.png'])


if __name__ == '__main__': unittest.main()
