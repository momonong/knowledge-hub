"""Reading order and collapsed evidence checks; host executes, no browser needed."""
import html
from html.parser import HTMLParser
import unittest

from knowledge_hub.connection_views import render


class Visible(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.depth = 0
        self.text = []
        self.buttons = []
        self.button = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        if tag == 'details':
            self.depth += 1
        if tag == 'button':
            self.button = {'attrs': dict(attrs), 'text': ''}

    def handle_endtag(self, tag):
        if tag == 'details':
            self.depth -= 1
        if tag == 'button' and self.button is not None:
            self.buttons.append(self.button)
            self.button = None

    def handle_data(self, data):
        if self.depth == 0:
            self.text.append(data)
        if self.button is not None:
            self.button['text'] += data


class ReadingViewTests(unittest.TestCase):
    def context(self):
        related = []
        for n in (2, 3, 4):
            windows = []
            for p in (1, 2, 3):
                text = ''.join(f'原文{n}段{p}-{k:03d} ' for k in range(100))[:700]
                windows.append({'anchor': f'p{p}', 'quote': text,
                                'reading_url': f'/items/{n}?version={n}#p{p}',
                                'source_url': f'https://example.org/{n}#p{p}'})
            related.append({'source_id': f'source-{n}', 'item_id': n, 'version_id': n,
                            'title': f'來源 {n}', 'scope': 'partial', 'relation_kind': 'background',
                            'reason': '可先閱讀的問題背景。', 'assertion_status': 'curator_proposal',
                            'concepts': [{'title': '檢索'}], 'excerpts': windows})
        return {'focal': {}, 'related': related, 'limitations': '有限片段，不代表全文。', 'notes': []}

    def saved(self, state='completed'):
        source = self.context()['related'][0]
        cite = dict(source['excerpts'][0], source_id='S2', item_id=2, version_id=2)
        cite['quote'] = cite['quote'][:20]
        return {'id': 7, 'item_id': 1, 'version_id': '1', 'focus': '合成焦點',
                'request_id': 'saved-request', 'operation_id': 'original-op', 'state': state,
                'reason_code': 'transport_unknown' if state == 'unknown' else None,
                'http_status': None, 'usage_state': 'unknown',
                'payload': {'sources': [dict(source, id='S2')]},
                'answer': {'claims': [{'kind': 'comparison', 'text': '兩份公開證據的比較。',
                                       'citations': [cite]}], 'limitations': '合成顯示案例'}
                          if state == 'completed' else None}

    def page(self, provider, rows):
        context = self.context()

        class Service:
            def build_context(self, *args):
                return context

            def records(self, *args):
                return rows

        service = Service()
        service.provider = provider
        page = render(service, 1, '1', 'synthetic-csrf', '', lambda url: html.escape(url, quote=True))
        return page, Visible(page), context

    def test_action_and_answer_precede_concise_cards_with_full_evidence_in_details(self):
        page, parser, context = self.page(object(), [self.saved()])
        visible = ''.join(parser.text)
        self.assertLess(page.index('action="/items/1/connections"'), page.index('兩份公開證據的比較。'))
        self.assertLess(page.index('兩份公開證據的比較。'), page.index('<h3>找回相關原文</h3>'))
        self.assertNotIn('curator_proposal', visible)
        self.assertNotIn('background', visible)
        self.assertIn('建議先讀的背景', visible)
        self.assertIn('實際費用', visible)
        for source in context['related']:
            self.assertIn(source['excerpts'][0]['quote'][:160], visible)
            self.assertNotIn(source['excerpts'][0]['quote'][300:350], visible)
            self.assertNotIn(source['excerpts'][1]['quote'][:160], visible)
            for excerpt in source['excerpts']:
                self.assertIn(html.escape(excerpt['quote'], quote=True), page)
                self.assertIn(html.escape(excerpt['reading_url'], quote=True), page)
                self.assertIn(html.escape(excerpt['source_url'], quote=True), page)
        generate = next(b for b in parser.buttons if '產生雙來源' in b['text'])
        self.assertNotIn('disabled', generate['attrs'])

    def test_explicit_broker_unknown_has_actionable_wording_and_only_original_refresh(self):
        row = self.saved('unknown')
        row['reason_code'] = 'provider_unknown'
        row['http_status'] = 200
        page, parser, _ = self.page(object(), [row])
        visible = ''.join(parser.text)
        self.assertIn('Broker 回報執行情況未知', visible)
        self.assertIn('只能核對原操作', visible)
        self.assertIn('不會重新提交或改用其他提供者', visible)
        self.assertNotIn('回應狀態無法辨識', visible)
        self.assertIn('狀態：結果待核對', visible)
        self.assertIn('action="/connections/7/refresh"', page)
        generate = next(b for b in parser.buttons if '產生雙來源' in b['text'])
        self.assertIn('disabled', generate['attrs'])
        refresh = next(b for b in parser.buttons if '核對原操作' in b['text'])
        self.assertNotIn('disabled', refresh['attrs'])

    def test_unavailable_and_unresolved_disable_generate_but_keep_original_refresh(self):
        _, parser, _ = self.page(None, [])
        generate = next(b for b in parser.buttons if '產生雙來源' in b['text'])
        self.assertIn('disabled', generate['attrs'])
        self.assertIn('尚未配置', ''.join(parser.text))
        for state in ('pending', 'unknown'):
            page, parser, _ = self.page(object(), [self.saved(state)])
            visible = ''.join(parser.text)
            generate = next(b for b in parser.buttons if '產生雙來源' in b['text'])
            self.assertIn('disabled', generate['attrs'])
            self.assertIn('action="/connections/7/refresh"', page)
            refresh = next(b for b in parser.buttons if '核對原操作' in b['text'])
            self.assertNotIn('disabled', refresh['attrs'])
            self.assertIn('暫停新生成', visible)
            if state == 'unknown':
                self.assertIn('連線中斷或逾時', visible)
