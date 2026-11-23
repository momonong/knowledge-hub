"""Fixed arXiv editions and authenticated PDF evidence with synthetic input."""
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub import sources
from knowledge_hub.hosting import MountedApplication
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app

PAPER = '2601.12345'
HTML_URL = f'https://arxiv.org/html/{PAPER}v3'
PDF_URL = f'https://arxiv.org/pdf/{PAPER}v3'
RAW = b'%PDF-1.4 synthetic fixed PDF'
PNG = b'\x89PNG\r\n\x1a\nsynthetic'
META = f'<meta name="citation_arxiv_id" content="{PAPER}v3"><meta property="og:url" content="https://arxiv.org/abs/{PAPER}v3">'.encode()


def pdf_result(paper=PAPER, label='v3'):
    url=f'https://arxiv.org/pdf/{paper}{label}'
    blocks = [{'anchor':'page-2','text':'Page two has the result and Figure 1.',
               'source_url':url+'#page=2'}]
    entry = {'key':'arxiv:'+paper,'source_name':'arxiv','title':'Memory retrieval paper',
             'label':label,'scope':'partial','fulltext_status':'partial','author':'',
             'published_at':'','source_url':url,'blocks':blocks,
             'content_hash':sources.compute_content_hash('Memory retrieval paper','partial',label,blocks)}
    panels = [{'kind':'figure','anchor':'pdf-figure-1','label':'Figure 1','caption':'Observed result.',
               'representation':'pdf_page','page_number':2,'page_url':url+'#page=2',
               'steps':['Read the result.'],'images':[], 'rows':[]}]
    return entry, {'source':f'https://arxiv.org/html/{paper}{label}','pdf_url':url,
                   'panels':panels,'state':'ready','page_count':4,'truncated':False}


class EditionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'knowledge.sqlite3'
        self.store=KnowledgeStore(self.path)
        self.date=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.item,self.index,_ = self.insert(PAPER)
        self.calls=[]

    def insert(self, paper):
        feed = json.dumps([{'paper':{'id':paper,'title':'Memory retrieval paper',
            'summary':'Memory retrieval works over stored observations with dated actions.',
            'publishedAt':self.date}}]).encode()
        return self.store.upsert_parsed_entry(sources.parse_huggingface_papers(feed)[0])

    def request(self, app, path, *, query='', fields=None, mounted=False):
        env={};setup_testing_defaults(env)
        env.update(PATH_INFO=path, QUERY_STRING=query,
                   HTTP_HOST='momonong.me' if mounted else 'localhost',
                   REMOTE_ADDR='127.0.0.1', HTTP_X_FORWARDED_PROTO='https')
        if fields is not None:
            content=urlencode({'csrf':self.store.get_csrf_token(),**fields}).encode()
            env.update(REQUEST_METHOD='POST', CONTENT_TYPE='application/x-www-form-urlencoded',
                       CONTENT_LENGTH=str(len(content)),
                       HTTP_ORIGIN='https://momonong.me' if mounted else 'http://localhost')
            env['wsgi.input']=BytesIO(content)
        response={}
        def start(status,headers):
            response.update(status=int(status.split()[0]), headers=dict(headers))
        raw=b''.join(app(env,start))
        return response,raw

    def test_first_guide_stage_serves_text_while_visual_originals_are_still_unavailable(self):
        content = b'<article><h1>Fixed source</h1><p id="p1">We introduce a retrieval method that reduces the cost of searching archived documents.</p></article>'
        item, version, _ = self.store.upsert_parsed_entry(sources.parse_arxiv_html(content,HTML_URL))
        app = create_app(self.path,fetch=lambda url:self.fail('Text stage fetched '+url))
        response, raw = self.request(app,f'/items/{item}/guide',query=f'version={version}&stage=text')
        self.assertEqual(response['status'],200)
        guide = json.loads(raw)
        self.assertFalse(guide['visuals_ready'])
        self.assertTrue(any(c['anchor']=='p1' for section in guide['sections'] for c in section['citations']))

    def test_html_math_keeps_the_visible_measure_once_at_its_publisher_anchor(self):
        html = b'''<article><h1>Controlled measurement</h1><p id="cost">Output was
            <math><semantics><mn>371.7</mn><annotation encoding="application/x-tex">371.7</annotation>
            </semantics></math> words.</p></article>'''
        result = sources.parse_arxiv_html(html, HTML_URL)
        self.assertEqual(result['blocks'][0]['text'], 'Output was 371.7 words.')
        self.assertEqual(result['blocks'][0]['source_url'], HTML_URL + '#cost')

    def test_fixed_url_edition_validation_and_network_caps(self):
        self.assertEqual(sources.arxiv_abs_url(PAPER),f'https://arxiv.org/abs/{PAPER}')
        self.assertEqual(sources.fetch_response_limit(PDF_URL),20*1024*1024)
        self.assertEqual(sources.fetch_response_limit(HTML_URL),2*1024*1024)
        self.assertEqual(sources.parse_arxiv_abs_version(META,PAPER),'v3')
        for bad in (META.replace(b'v3"><',b'v4"><'),META.replace(b'12345v3"><',b'12346v3"><'),
                    META.replace(b'v3',b'')):
            with self.assertRaises(ValueError):
                sources.parse_arxiv_abs_version(bad,PAPER)
        for bad in (PDF_URL[:-2],PDF_URL+'?',PDF_URL+'#',PDF_URL+'?v=3',PDF_URL+'#page=2',
                    PDF_URL.replace('arxiv.org','other.example')):
            with self.assertRaises(ValueError):
                sources.validate_fetch_url(bad)

    def fixture_fetch(self,url):
        self.calls.append(url)
        if url == sources.arxiv_metadata_url(PAPER):
            # Well-formed unversioned Atom is not grounds for assuming v1.
            return f'''<feed><entry><id>https://arxiv.org/abs/{PAPER}</id>
                <title>Memory retrieval paper</title><summary>Public abstract text.</summary>
                </entry></feed>'''.encode()
        if url==f'https://arxiv.org/abs/{PAPER}':
            return META
        if url==PDF_URL:
            return RAW
        raise OSError('Synthetic fixed upstream failure')

    def test_pdf_fallback_persists_fixed_date_and_only_serves_listed_pages(self):
        app=create_app(self.path,fetch=self.fixture_fetch)
        with patch('knowledge_hub.pdf_evidence.parse',return_value=pdf_result()) as parser, \
             patch('knowledge_hub.pdf_evidence.render_page',return_value=PNG) as render:
            response,_=self.request(app,f'/items/{self.item}/fetch',fields={'version_id':self.index})
            self.assertEqual(response['status'],303)
            item=self.store.get_item(self.item)
            chosen=self.store.select_default_version(item['versions'])
            self.assertEqual((chosen['label'],chosen['scope'],chosen['published_at']),('v3','partial',self.date))
            self.assertEqual(chosen['source_url'],PDF_URL)
            self.assertIn(self.index,[version['id'] for version in item['versions']])
            query='version='+chosen['id']
            path=f'/knowledge/items/{self.item}/visuals'
            for authorize,expected in ((False,401),(True,200)):
                mounted=MountedApplication(app,origin='https://momonong.me',prefix='/knowledge',
                                           authorize=lambda _: authorize)
                response,body=self.request(mounted,path,query=query,mounted=True)
                self.assertEqual(response['status'],expected)
            manifest=json.loads(body)
            self.assertEqual(manifest['panels'][0]['preview_url'],path[len('/knowledge'):]+'/pdf/2?'+query)
            self.assertEqual(manifest['panels'][0]['page_url'],PDF_URL+'#page=2')
            for suffix,expected in (('/pdf/1',404),('/pdf/2',200),('/pdf/2',200)):
                response,body=self.request(mounted,path+suffix,query=query,mounted=True)
                self.assertEqual(response['status'],expected)
            self.assertEqual(body,PNG)
            render.assert_called_once_with(RAW,2)
            parser.assert_called_once_with(RAW,HTML_URL)
            response,_=self.request(mounted,path+'/pdf/2',query='version='+self.index,mounted=True)
            self.assertEqual(response['status'],404)
            self.assertEqual(self.calls.count(PDF_URL),1)
            # Persisted public raw bytes survive app restart and avoid a download.
            restarted=create_app(self.path,fetch=self.fixture_fetch)
            response,_=self.request(restarted,path[len('/knowledge'):],query=query)
            self.assertEqual(response['status'],200)
            self.assertEqual(self.calls.count(PDF_URL),1)

    def test_unresolved_source_reports_failure_without_creating_v1(self):
        def fetch(url):
            self.calls.append(url)
            raise OSError('Neither official metadata format available')
        app=create_app(self.path,fetch=fetch)
        response,_=self.request(app,f'/items/{self.item}/fetch',fields={'version_id':self.index})
        self.assertEqual(response['status'],303)
        item=self.store.get_item(self.item)
        self.assertEqual([ver['label'] for ver in item['versions']],['hf-index'])
        self.assertEqual(item['fulltext_status'],'failed')
        response,raw=self.request(app,f'/items/{self.item}/visuals',query='version='+self.index)
        self.assertEqual(response['status'],200)
        self.assertEqual(json.loads(raw)['reason'],'edition_unresolved')
        self.assertFalse(any('/html/' in url or '/pdf/' in url for url in self.calls))

    def test_mixed_caption_and_image_table_use_only_same_edition_pdf_page(self):
        html = b"""<article><h1>Memory retrieval paper</h1><p>Measured observations.</p>
        <figure class="ltx_figure" id="F1"><img src="2601.12345v3/plot.png">
        <figcaption>Figure 1: Metric.</figcaption></figure>
        <figure class="ltx_figure" id="F2"><figcaption>Figure 2: Projection.</figcaption></figure>
        <figure class="ltx_table" id="T1"><figcaption>Table 1: Outcomes.</figcaption>
        <table><tr><th><img src="2601.12345v3/header.png"></th></tr><tr><td>12</td></tr></table>
        </figure></article>"""
        _, version, _ = self.store.upsert_parsed_entry(sources.parse_arxiv_html(html, HTML_URL))
        chosen = {'/html/':html, '/pdf/':RAW}
        calls = []
        def fetch(url):
            calls.append(url)
            return next(raw for route, raw in chosen.items() if route in url)
        entry, pdf = pdf_result()
        example = pdf['panels'][0]
        pdf['panels'] = [
            {**example,'caption':'Figure 2: Projection.','page_number':2},
            {**example,'kind':'table','caption':'Table 1: Outcomes.','page_number':3,
             'page_url':PDF_URL+'#page=3'},
        ]
        app=create_app(self.path,fetch=fetch)
        with patch('knowledge_hub.pdf_evidence.parse',return_value=(entry,pdf)):
            response,raw=self.request(app,f'/items/{self.item}/visuals',query='version='+version)
        self.assertEqual(response['status'],200)
        panels=json.loads(raw)['panels']
        self.assertEqual([panel['anchor'] for panel in panels], ['F1','F2','T1'])
        self.assertEqual(panels[0]['images'],[HTML_URL+'/plot.png'])
        self.assertEqual([panel['page_number'] for panel in panels[1:]],[2,3])
        self.assertEqual(calls,[HTML_URL,PDF_URL])

    def test_daily_worker_prefetches_selected_versions_once_and_preserves_rank_dates(self):
        papers = [PAPER,*[f'2601.12{n}' for n in (346,347,348)]]
        for paper in papers[1:]:
            self.insert(paper)
        def fetch(url):
            self.calls.append(url)
            if 'id_list=' in url:
                paper=url.split('id_list=',1)[1].split('&',1)[0]
                return (f'<feed><entry><id>https://arxiv.org/abs/{paper}v3</id>'
                        '<title>Memory retrieval paper</title>'
                        '<summary>Public abstract.</summary></entry></feed>').encode()
            if '/html/' in url:
                return b'<article><h1>Memory retrieval paper</h1><p>Memory retrieval evidence.</p></article>'
            if url.startswith(sources.HF_PAPERS_API):
                return b'[]'
            raise OSError('Synthetic source missing')
        app=create_app(self.path,fetch=fetch)
        self.assertEqual(app.refresh_daily_sources()['state'],'partial')
        # Four eligible articles have one subcategory. The two selected originals are prefetched.
        self.assertEqual(sum('/html/' in url for url in self.calls), 2)
        self.assertEqual(sum('id_list=' in url for url in self.calls), 2)
        latest=[self.store.select_default_version(item['versions']) for item in self.store.get_all_items()]
        self.assertEqual(sum(version['label']=='v3' for version in latest), 2)
        self.assertTrue(all(version['published_at']==self.date for version in latest))
        before=list(self.calls)
        create_app(self.path,fetch=fetch).refresh_daily_sources()
        self.assertEqual(self.calls,before)

    def test_actual_fetcher_uses_separate_caps_and_disables_redirects(self):
        class Response(BytesIO):
            status = 200
            def __init__(self, contents, length=None):
                super().__init__(contents)
                self.headers = {} if length is None else {'Content-Length':str(length)}

        class Opener:
            def __init__(self):
                self.requests=[]
            def open(self, request, timeout):
                self.requests.append(request)
                return Response(b'%PDF-'+b'A'*(3*1024*1024))

        opener = Opener()
        with patch('knowledge_hub.sources.urllib.request.build_opener',return_value=opener) as build:
            self.assertGreater(len(sources.default_fetch(PDF_URL)),2*1024*1024)
            self.assertEqual(opener.requests[0].get_header('Accept'),'application/pdf')
            self.assertIsInstance(build.call_args.args[-1],sources._NoRedirectHandler)
            with self.assertRaisesRegex(ValueError,'2 MiB'):
                sources.default_fetch(HTML_URL)
        with patch.object(opener,'open',return_value=Response(b'',21*1024*1024)), \
             patch('knowledge_hub.sources.urllib.request.build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'20 MiB'):
                sources.default_fetch(PDF_URL)

    def test_teach_preserves_existing_record_and_reports_missing_original_without_dispatch(self):
        from test_study import FakeProvider
        provider=FakeProvider()
        def unavailable(url):
            if url==sources.arxiv_abs_url(PAPER):
                return META
            raise OSError('Synthetic source missing')
        app=create_app(self.path,fetch=unavailable,study_provider=provider)
        response,body=self.request(app,f'/items/{self.item}/teach',
            fields={'version_id':self.index,'request_id':'not-dispatched'})
        self.assertEqual(response['status'],409)
        self.assertIn('同版 HTML 和 PDF 暫時沒有可讀正文'.encode(),body)
        self.assertEqual(provider.calls,[])
        self.assertEqual(self.store.get_item(self.item)['versions'][0]['id'],self.index)

    def test_partial_html_body_preserves_publisher_anchors_without_fetching_pdf(self):
        html = b"""<article><h1>Memory retrieval paper</h1><div class="ltx_ERROR">Equation unavailable.</div>
        <section id="S1"><h2>Methods</h2><p id="S1.p1">The retrieval operator reads stored observations
        before computing evidence for the query, with one threshold shared across candidate records.</p>
        </section></article>"""
        _, version, _ = self.store.upsert_parsed_entry(sources.parse_arxiv_html(html, HTML_URL))
        self.assertEqual(self.store.get_item(self.item)['versions'][-1]['scope'], 'partial')

        def fetch(url):
            self.calls.append(url)
            return {HTML_URL:html, PDF_URL:RAW}[url]

        app = create_app(self.path,fetch=fetch)
        with patch('knowledge_hub.pdf_evidence.parse',return_value=pdf_result()):
            response,_ = self.request(app,f'/items/{self.item}/fetch',fields={'version_id':version})

        self.assertEqual(response['status'],303)
        self.assertIn('version='+version,response['headers']['Location'])
        self.assertEqual(self.calls,[HTML_URL])
        saved = self.store.get_item(self.item)
        selected = self.store.select_default_version(saved['versions'])
        self.assertEqual(selected['scope'],'partial')
        self.assertIn(HTML_URL+'#S1.p1',[block['source_url'] for block in selected['blocks']])

    def test_notice_without_readable_body_uses_pdf(self):
        html = b'<title>Memory retrieval paper</title><p>HTML is not available.</p>'
        _, version, _ = self.store.upsert_parsed_entry(sources.parse_arxiv_html(html, HTML_URL))
        app = create_app(self.path,fetch={HTML_URL:html,PDF_URL:RAW}.__getitem__)
        with patch('knowledge_hub.pdf_evidence.parse',return_value=pdf_result()):
            response,_ = self.request(app,f'/items/{self.item}/fetch',fields={'version_id':version})
        self.assertEqual(response['status'],303)
        saved = self.store.get_item(self.item)
        self.assertEqual(self.store.select_default_version(saved['versions'])['source_url'],PDF_URL)

    def test_preserves_both_author_tables_inside_latexml_table_float(self):
        from knowledge_hub import visuals
        html = b"""<figure class="ltx_table" id="A2.T8"><div class="ltx_flex_table">
        <figure class="ltx_figure ltx_figure_panel ltx_minipage" id="A2.T8.fig1">
        <figcaption><span class="ltx_tag_figure">Table 7: </span>Dataset accuracy.</figcaption>
        <table><tr><td>Seven</td></tr></table></figure>
        <figure class="ltx_figure ltx_figure_panel ltx_minipage" id="A2.T8.fig2">
        <figcaption><span class="ltx_tag_figure">Table 8: </span>Document accuracy.</figcaption>
        <table><tr><td>Eight</td></tr></table></figure>
        </div></figure>"""
        panels = visuals.extract(html,HTML_URL)['panels']
        self.assertEqual([(p['anchor'],p['kind']) for p in panels],
                         [('A2.T8.fig1','table'),('A2.T8.fig2','table')])
        self.assertEqual([p['rows'][0][0]['text'] for p in panels],['Seven','Eight'])
        self.assertEqual([p['caption'].split(':')[0] for p in panels],['Table 7','Table 8'])

    def test_pdf_retains_unmatched_and_only_pairs_unique_normalized_captions(self):
        html = b"""<article><h1>Memory retrieval paper</h1><div class="ltx_ERROR">Partial conversion</div>
        <p id="S1.p1">Observed evidence in the paper.</p>
        <figure id="F1"><img src="2601.12345v3/plot.png"><figcaption>Figure 1: Common metric.</figcaption></figure>
        <figure id="F2a"><figcaption>Figure 2: Query projection.</figcaption></figure>
        <figure id="F2b"><figcaption>Figure 2: Another observation.</figcaption></figure>
        <figure id="T3a" class="ltx_table"><figcaption>Table 3: Duplicate caption.</figcaption></figure>
        <figure id="T3b" class="ltx_table"><figcaption>Table 3: Duplicate caption.</figcaption></figure>
        </article>"""
        _, version, _ = self.store.upsert_parsed_entry(sources.parse_arxiv_html(html,HTML_URL))
        entry, manifest = pdf_result()
        prototype = manifest['panels'][0]
        manifest['panels'] = [
            {**prototype,'anchor':'pdf-f1','caption':'Figure 1 Common metric.'},
            {**prototype,'anchor':'pdf-f2','caption':'Figure 2 Query projection.'},
            {**prototype,'anchor':'pdf-t3','kind':'table','caption':'Table 3 Duplicate caption.'},
            {**prototype,'anchor':'pdf-t7','kind':'table','caption':'Table 7 PDF-only results.'},
        ]
        app = create_app(self.path,fetch={HTML_URL:html,PDF_URL:RAW}.__getitem__)
        with patch('knowledge_hub.pdf_evidence.parse',return_value=(entry,manifest)):
            response,raw = self.request(app,f'/items/{self.item}/visuals',query='version='+version)
        self.assertEqual(response['status'],200)
        panels = json.loads(raw)['panels']
        self.assertEqual([p['anchor'] for p in panels],['F1','F2a','F2b','T3a','T3b','pdf-t3','pdf-t7'])
        self.assertEqual([p['anchor'] for p in panels if p.get('representation')=='pdf_page'],
                         ['F2a','pdf-t3','pdf-t7'])
        self.assertTrue(all(p.get('incomplete') for p in (panels[2],panels[3],panels[4])))
