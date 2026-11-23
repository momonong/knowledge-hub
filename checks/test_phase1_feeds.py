import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

from knowledge_hub import sources
from knowledge_hub.feeds import collect
from knowledge_hub.store import KnowledgeStore


DAY = '2026-11-21'
STAMP = '2026-11-20T15:00:00Z'
PAST = '2026-11-19T15:00:00Z'
ARXIV = 'https://export.arxiv.org/api/query'
HF = 'https://huggingface.co/api/daily_papers'
RSS = 'https://rss.arxiv.org/rss/'
PROJECTS = {
    'pytorch': 'pytorch/pytorch',
    'vllm': 'vllm-project/vllm',
    'sglang': 'sgl-project/sglang',
    'tensorrt-llm': 'NVIDIA/TensorRT-LLM',
    'transformers': 'huggingface/transformers',
}


def atom(*entries):
    return ('<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(
        f'<entry><id>http://arxiv.org/abs/{paper}</id><title>{escape(title)}</title>'
        f'<summary>{escape(summary)}</summary><updated>{STAMP}</updated>'
        f'<published>{PAST}</published><author><name>Fixture Author</name></author></entry>'
        for paper, title, summary in entries
    ) + '</feed>').encode()


def huggingface(*papers):
    return json.dumps([
        {'paper': {'id': identifier, 'title': title, 'summary': abstract, 'publishedAt': STAMP,
                   'ai_summary': 'AI_GENERATED_SENTINEL'}}
        for identifier, title, abstract in papers
    ]).encode()


def release(repo, tag='v1.4.0', body='## Highlights\n\nAdds an optimized distributed inference backend.',
            **fields):
    return json.dumps({'tag_name': tag, 'name': f'{repo} {tag}', 'body': body,
                       'published_at': STAMP, 'updated_at': STAMP, 'draft': False,
                       'prerelease': False, 'author': {'login': 'maintainer'},
                       'html_url': f'https://github.com/{repo}/releases/tag/{tag}', **fields}).encode()


class Phase1Feeds(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = KnowledgeStore(Path(temp.name) / 'knowledge.sqlite3')

    def collect(self, fetch, day=DAY):
        return collect(self.store, fetch, day)

    def test_sources_are_fixed_and_failures_are_isolated(self):
        requested = []

        def fetch(url):
            self.assertEqual(sources.validate_fetch_url(url), url)
            requested.append(url)
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            if parsed.netloc == 'export.arxiv.org':
                category = query['search_query'][0]
                if category == 'cat:cs.PF':
                    raise HTTPError(url, 503, 'PROVIDER_SECRET_SENTINEL', {}, None)
                if category == 'cat:cs.DC':
                    return b'<html>unavailable</html>'
                return atom(('2611.10001v3', 'A portable caching runtime', 'Uses a compressed KV cache.'))
            if parsed.netloc == 'huggingface.co':
                if 'date' in query:
                    return huggingface(('2611.10002', 'A curated daily entry', 'Mechanism and evidence.'))
                return huggingface(('2611.10003', 'A curated monthly entry', 'Mechanism and evidence.'))
            repo = parsed.path.split('/releases/')[0].removeprefix('/repos/')
            return release(repo)

        self.collect(fetch)
        arxiv_queries = {parse_qs(urlsplit(url).query)['search_query'][0]
                         for url in requested if url.startswith(ARXIV)}
        self.assertTrue({'cat:cs.CL', 'cat:cs.LG', 'cat:cs.AI', 'cat:cs.CV',
                         'cat:cs.RO', 'cat:cs.DC', 'cat:cs.PF'} <= arxiv_queries)
        self.assertIn(HF + '?date=2026-11-20&sort=publishedAt&limit=50&p=0', requested)
        self.assertIn(HF + '?month=2026-11&sort=trending&limit=50&p=0', requested)
        status = self.store.get_sources()
        self.assertEqual(status['arxiv:cs.PF']['state'], 'failed')
        self.assertIn('503', status['arxiv:cs.PF']['last_error'])
        self.assertNotIn('PROVIDER_SECRET_SENTINEL', status['arxiv:cs.PF']['last_error'])
        self.assertEqual(status['arxiv:cs.DC']['state'], 'failed')
        self.assertEqual(status['arxiv']['state'], 'failed')
        self.assertEqual(status['huggingface']['state'], 'updated')
        self.assertEqual(status['huggingface:daily']['state'], 'updated')
        self.assertEqual(status['huggingface:monthly']['state'], 'updated')
        self.assertTrue(all(status[name]['state'] == 'updated' for name in PROJECTS))
        items = {item['key'] for item in self.store.get_all_items()}
        self.assertEqual(items, {'arxiv:2611.10001', 'arxiv:2611.10002', 'arxiv:2611.10003',
                                 *(f'{name}:v1.4.0' for name in PROJECTS)})
        self.collect(fetch)
        self.assertEqual(self.store.get_sources()['huggingface']['state'], 'unchanged')
        self.assertEqual(self.store.get_sources()['pytorch']['state'], 'unchanged')
        self.assertEqual(len(self.store.get_all_items()), len(items))

    def test_empty_atom_is_successful_and_daily_can_use_latest_available_papers(self):
        requests = []

        def fetch(url):
            requests.append(url)
            if url.startswith(ARXIV):
                return b'<feed xmlns="http://www.w3.org/2005/Atom" />'
            if url == HF + '?date=2026-11-20&sort=publishedAt&limit=50&p=0':
                raise HTTPError(url, 400, 'No edition at this date', {}, None)
            if url == HF + '?sort=publishedAt&limit=50&p=0':
                return huggingface(('2611.90001', 'Latest published papers', 'Author abstract.'))
            if url.startswith(HF):
                return b'[]'
            raise OSError('No release fixture')

        self.collect(fetch)
        self.assertFalse(any(url.startswith(RSS) for url in requests))
        self.assertEqual([item['key'] for item in self.store.get_all_items()], ['arxiv:2611.90001'])
        state = self.store.get_sources()
        self.assertEqual(state['arxiv']['state'], 'unchanged')
        self.assertEqual(state['huggingface:daily']['state'], 'updated')
        self.assertIn('400', state['huggingface:daily']['last_error'])
        self.assertEqual(state['huggingface:monthly']['state'], 'unchanged')

    def test_fixed_versions_take_precedence_over_unversioned_feeds(self):
        include_arxiv = True

        def fetch(url):
            if url.startswith(ARXIV):
                category = parse_qs(urlsplit(url).query)['search_query'][0]
                if not include_arxiv:
                    raise OSError('arXiv unavailable')
                if category == 'cat:cs.LG':
                    return atom(('2611.12345v2', 'Revision two', 'Original abstract for version two.'),
                                ('2611.12346', 'Unpinned arXiv metadata', 'Unknown revision.'))
                return atom(('2611.12345v10', 'Revision ten', 'Original abstract for version ten.'))
            if url.startswith(HF):
                return huggingface(('2611.12345', 'Unversioned index title', 'Indexer metadata.'),
                                   ('2611.12347', 'Only in HF', 'Original abstract for unknown version.'))
            raise OSError('No release fixture')

        self.collect(fetch)
        include_arxiv = False
        self.collect(fetch)
        items = {item['key']: item for item in self.store.get_all_items()}
        self.assertEqual(set(items), {'arxiv:2611.12345', 'arxiv:2611.12347'})
        paper = items['arxiv:2611.12345']
        self.assertEqual(paper['title'], 'Revision ten')
        self.assertEqual({v['label']: v['source_url'] for v in paper['versions']},
                         {'v2': 'https://arxiv.org/abs/2611.12345v2',
                          'v10': 'https://arxiv.org/abs/2611.12345v10'})
        hf_only = items['arxiv:2611.12347']['versions']
        self.assertEqual([v['label'] for v in hf_only], ['hf-index'])
        self.assertEqual(hf_only[0]['blocks'][0]['text'], 'Original abstract for unknown version.')

    def test_rss_fallback_only_keeps_verified_fixed_editions(self):
        valid = (
            '<item><title>Cached inference with smaller memory</title>'
            '<link>https://arxiv.org/abs/2611.23456</link>'
            '<description>arXiv:2611.23456v4 Announce Type: replace&#10;'
            'Abstract: We evaluate attention and KV cache reuse.</description>'
            '<pubDate>Fri, 20 Nov 2026 00:00:00 -0500</pubDate>'
            '<dc:creator>Fixture Author</dc:creator></item>'
        )
        mismatched = valid.replace('arXiv:2611.23456v4', 'arXiv:2611.23457v2')
        versionless = valid.replace('2611.23456', '2611.23458').replace('23458v4', '23458')
        rss_header = '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
        rss_end = '</channel></rss>'
        requested = []
        atom_recovered = False

        def fetch(url):
            requested.append(url)
            if url == RSS + 'cs.CV':
                self.assertEqual(sources.validate_fetch_url(url), url)
                return (rss_header + mismatched + versionless + valid + rss_end).encode()
            if url == RSS + 'cs.PF':
                return (rss_header + '<title>cs.PF updates on arXiv.org</title>' + rss_end).encode()
            if url.startswith(ARXIV) and 'cs.CV' in url:
                if atom_recovered:
                    return atom(('2611.23456v4', 'Cached inference', 'We evaluate attention and KV cache reuse.'))
                raise HTTPError(url, 429, 'Too Many Requests', {}, None)
            if url.startswith(ARXIV) and 'cs.LG' in url:
                return atom(('2611.23455v2', 'An Atom entry', 'An author abstract from the API.'))
            raise OSError('No fixture for other categories')

        self.collect(fetch)
        items = {item['key']: item for item in self.store.get_all_items()}
        self.assertEqual(set(items), {'arxiv:2611.23456', 'arxiv:2611.23455'})
        version = items['arxiv:2611.23456']['versions'][0]
        self.assertEqual(version['label'], 'v4')
        self.assertEqual(version['author'], 'Fixture Author')
        self.assertEqual(version['source_url'], 'https://arxiv.org/abs/2611.23456v4')
        self.assertEqual(version['published_at'], '2026-11-20T05:00:00Z')
        self.assertEqual(version['blocks'][0]['text'], 'We evaluate attention and KV cache reuse.')
        self.assertNotIn(RSS + 'cs.LG', requested)
        self.assertFalse(any(url.startswith(ARXIV) and 'cs.RO' in url for url in requested))
        state = self.store.get_sources()
        self.assertEqual(state['arxiv:cs.CV']['state'], 'updated')
        self.assertIn('429', state['arxiv:cs.CV']['last_error'])
        self.assertIn('RSS', state['arxiv:cs.CV']['last_error'])
        self.assertEqual(state['arxiv:cs.PF']['state'], 'failed')
        self.assertIn('RSS', state['arxiv:cs.PF']['last_error'])

        atom_recovered = True
        self.collect(fetch)
        paper = self.store.get_item(items['arxiv:2611.23456']['id'])
        self.assertEqual([v['id'] for v in paper['versions']], [version['id']])
        self.assertEqual(self.store.get_sources()['arxiv:cs.CV']['state'], 'unchanged')

    def test_release_signal_gate_and_repository_binding(self):
        payloads = {
            'pytorch/pytorch': release('pytorch/pytorch', 'v2.8.1', '## Fixes\n\nUpdate documentation.'),
            'vllm-project/vllm': release('vllm-project/vllm', 'v0.8.1',
                                         '## Performance\n\nAdds paged KV cache reuse to improve inference throughput.',
                                         name='v0.8.1'),
            'sgl-project/sglang': release('sgl-project/sglang', 'v0.8.0', prerelease=True),
            'NVIDIA/TensorRT-LLM': release('NVIDIA/TensorRT-LLM', 'v1.2.0', body=''),
            'huggingface/transformers': release('another/transformers', 'v5.0.0'),
        }

        def fetch(url):
            if not url.startswith('https://api.github.com/repos/'):
                raise OSError('No paper fixture')
            repo = urlsplit(url).path.split('/releases/')[0].removeprefix('/repos/')
            return payloads[repo]

        self.collect(fetch)
        items = self.store.get_all_items()
        self.assertEqual([i['key'] for i in items], ['vllm:v0.8.1'])
        self.assertEqual(items[0]['title'], 'vLLM v0.8.1')
        version = items[0]['versions'][0]
        self.assertEqual(version['scope'], 'full')
        self.assertEqual(version['source_url'], 'https://github.com/vllm-project/vllm/releases/tag/v0.8.1')
        state = self.store.get_sources()
        self.assertEqual(state['pytorch']['state'], 'unchanged')
        self.assertEqual(state['sglang']['state'], 'unchanged')
        self.assertEqual(state['tensorrt-llm']['state'], 'unchanged')
        self.assertEqual(state['transformers']['state'], 'failed')

    def test_legacy_pytorch_stable_release_keeps_unavailable_edition(self):
        def fetch(url):
            if url == 'https://api.github.com/repos/pytorch/pytorch/releases/latest':
                return release('pytorch/pytorch', 'v2.16.0', body='<script>untrusted()</script>', name='')
            raise OSError('No other fixture')

        self.collect(fetch)
        items = self.store.get_all_items()
        self.assertEqual([(item['key'], item['title']) for item in items],
                         [('pytorch:v2.16.0', 'PyTorch v2.16.0')])
        self.assertEqual(items[0]['versions'][0]['scope'], 'unavailable')

    def test_quant_research_requires_stronger_out_of_sample_evidence(self):
        def fetch(url):
            if 'q-fin.TR' not in url:
                raise OSError('No fixture for other categories')
            return atom(
                ('2611.56781v1', 'Market microstructure benchmark',
                 'An out-of-sample backtest compares slippage-adjusted returns on two years of limit order book data.'),
                ('2611.56782v1', 'Portfolio return prediction',
                 'Our training approach improves portfolio metrics.'),
            )

        self.collect(fetch)
        self.assertEqual([item['key'] for item in self.store.get_all_items()], ['arxiv:2611.56781'])

    def test_only_exact_new_urls_are_allowed(self):
        bad_urls = (
            RSS + 'cs.GT',
            RSS + 'cs.CV?query=PRIVATE_SENTINEL',
            ARXIV + '?search_query=cat%3Acs.CV&max_results=10&sortBy=PRIVATE_SENTINEL',
            ARXIV + '?search_query=cat%3Acs.GT&max_results=10&sortBy=submittedDate&sortOrder=descending',
            HF + '?date=2026-02-30&sort=publishedAt&limit=50&p=0',
            HF + '?date=2026-11-21&sort=PRIVATE_SENTINEL',
            'https://api.github.com/repos/unknown/vllm/releases/latest',
            'https://api.github.com/repos/vllm-project/vllm/releases/latest?token=PRIVATE_SENTINEL',
        )
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                sources.validate_fetch_url(url)


if __name__ == '__main__':
    unittest.main()
