"""Rendered daily cards preserve native controls and fixed-edition links."""
from html.parser import HTMLParser
import unittest
from unittest.mock import Mock, patch

from knowledge_hub.daily_views import story
from knowledge_hub.reader_views import home


def candidate():
    return {
        'item': {'id': 7, 'key': 'arxiv:2611.26427', 'title': 'Cache with tenants', 'source_name': 'arxiv'},
        'version': {'id': '19', 'label': 'v2', 'scope': 'abstract',
                    'published_at': '2026-11-20T20:00:00Z', 'source_url': 'https://arxiv.org/abs/2611.26427v2'},
        'category': '推論與系統',
        'spectrum_reason': '可追蹤多租戶推論的資源管理。',
        'reasons': [{'kind': 'project', 'title': 'public-reader', 'href': '', 'matches': ['快取', '快取']}],
        'evidence': '作者提到分割 KV 快取。',
        'evidence_strength': 2,
        'personalized': True,
        'brief': {
            'state': 'ready', 'attempts': 1, 'error': '',
            'l1': {'change': '根據租戶分割快取。', 'highlight': '在請求入場時回收區塊。',
                   'relevance': '可追蹤多租戶推論的資源管理。'},
            'l2': {'mechanism': '各租戶維持自己的快取分區。', 'metrics': 'P99 延遲降低 18%。',
                   'comparison': '比較基準為 FIFO。', 'limits': '現階段僅支援單張 GPU。'},
        },
    }


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def attributes(self, tag):
        return [attrs for name, attrs in self.tags if tag == name]


class DailyCardTest(unittest.TestCase):
    def render(self, value=None):
        return story(candidate() if value is None else value, 0, 'csrf-test', '2026-11-21')

    def test_l1_is_visible_and_l2_is_server_rendered_inside_native_disclosure(self):
        markup = self.render()
        before, details = markup.split('<details', 1)
        self.assertIn('根據租戶分割快取。', before)
        self.assertIn('推論與系統', before)
        self.assertIn('v2 · 僅有摘要', before)
        self.assertIn('在請求入場時回收區塊。', before)
        self.assertEqual(markup.count('可追蹤多租戶推論的資源管理。'), 1)
        self.assertIn('展開 1 分鐘速讀', details)
        l2, _ = details.split('</details>', 1)
        self.assertNotIn('在請求入場時回收區塊。', l2)
        for text in candidate()['brief']['l2'].values():
            self.assertIn(text, l2)
            self.assertNotIn(text, before)
        tags = Tags(markup)
        self.assertEqual(tags.attributes('article')[0]['id'], 'daily-7')
        self.assertNotIn('open', tags.attributes('details')[0])
        self.assertNotIn('tabindex', tags.attributes('summary')[0])
        self.assertEqual(tags.attributes('script'), [])
        self.assertIn('接上專案</strong> public-reader', markup)
        self.assertNotIn('快取、快取', markup)

    def test_abstract_deep_read_keeps_post_fetch_and_direct_fixed_edition_link(self):
        markup = self.render()
        tags = Tags(markup)
        self.assertIn('href="/items/7?version=19">開始一起讀', markup)
        self.assertNotIn('data-open-edition', markup)
        self.assertTrue(any(f['action'] == '/items/7/fetch' and f['method'] == 'post'
                            for f in tags.attributes('form')))
        self.assertIn('取得全文並更新速讀', markup)
        self.assertIn('href="https://arxiv.org/abs/2611.26427v2"', markup)

    def test_full_article_links_directly_to_the_selected_immutable_edition(self):
        value = candidate()
        value['version']['scope'] = 'full'
        markup = self.render(value)
        self.assertNotIn('data-open-edition', markup)
        self.assertNotIn('/items/7/fetch', markup)
        self.assertIn('href="/items/7?version=19">開始一起讀', markup)

    def test_post_actions_bind_day_and_edition_and_offer_undo_for_saved_choices(self):
        for choice in ('', 'skip', 'later'):
            with self.subTest(choice=choice):
                value = candidate()
                value['choice'] = choice
                markup = self.render(value)
                tags = Tags(markup)
                self.assertTrue(any(f['action']=='/items/7/daily-choice' and f['method']=='post'
                                    for f in tags.attributes('form')))
                hidden = {(field['name'], field['value']) for field in tags.attributes('input')}
                self.assertTrue({('day','2026-11-21'), ('version_id','19'), ('csrf','csrf-test')} <= hidden)
                controls = {b['value'] for b in tags.attributes('button') if b.get('name') == 'choice'}
                self.assertEqual(controls, {'undo'} if choice else {'skip', 'later'})
                if choice == 'skip':
                    self.assertIn('已略過', markup)
                    self.assertNotIn('核心機制', markup)
                elif choice == 'later':
                    self.assertIn('已加入待讀', markup)
                    self.assertIn('核心機制', markup)

    def test_degraded_reason_remains_visible_and_legacy_cards_show_actual_excerpt(self):
        value = candidate()
        value['brief'].update({'state': 'degraded', 'error': '生成器逾時。'})
        markup = self.render(value)
        self.assertIn('速讀未完成', markup.split('<details', 1)[0])
        self.assertIn('生成器逾時。', markup)
        value.pop('brief')
        old = self.render(value)
        self.assertIn('作者來源節錄', old)
        self.assertIn(value['evidence'], old)
        self.assertNotIn('展開 1 分鐘速讀', old)

    def test_metrics_link_matches_the_article_version_and_canonical_anchor(self):
        value = candidate()
        value['brief']['metrics_source'] = {'version_id': '19', 'anchor': 'S4.T1',
                                           'href': '/items/7?version=19#kh-visual-S4.T1'}
        markup = self.render(value)
        self.assertIn('href="/items/7?version=19#kh-visual-S4.T1">核對量測原文', markup)
        self.assertIn('P99 延遲降低 18%。', markup.split('<details', 1)[1])
        for bad in ({'version_id': '20'}, {'href': '/items/8?version=19#kh-visual-S4.T1'},
                    {'href': '/items/7?version=19#arbitrary'}, {'anchor': 'S4/T1'}):
            with self.subTest(bad=bad):
                case = candidate()
                case['brief']['metrics_source'] = value['brief']['metrics_source'] | bad
                self.assertNotIn('核對量測原文', self.render(case))

    def test_untrusted_text_and_urls_cannot_create_markup_or_unsafe_navigation(self):
        value = candidate()
        value['item']['title'] = '"><img src=x onerror=alert(1)>'
        value['brief']['l2']['limits'] = '<script>alert(1)</script>'
        value['version']['source_url'] = 'https://publisher.org@evil.example/x'
        value['reasons'][0]['href'] = 'javascript:alert(1)'
        value['reasons'].append({'kind': 'reading', 'title': 'evil', 'href': '/\\evil.example/x', 'matches': []})
        markup = self.render(value)
        tags = Tags(markup)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', markup)
        self.assertEqual(tags.attributes('img'), [])
        self.assertEqual(tags.attributes('script'), [])
        self.assertTrue(all('evil.' not in link['href'] and 'javascript:' not in link['href']
                            for link in tags.attributes('a')))


class BriefingHomeTest(unittest.TestCase):
    def test_home_marks_publication_date_and_requested_styles_on_an_earlier_deck(self):
        store, library, reading = Mock(), Mock(), Mock()
        store.get_sources.return_value = {}
        library.list_sources.return_value = []
        with patch('knowledge_hub.reader_views.resumable', return_value=[]), patch(
            'knowledge_hub.reader_views.shell', side_effect=lambda title, csrf, body, **kwargs: body
        ) as shell:
            html = home(store, library, reading, 'csrf', picks=[candidate()],
                        selected_day='2026-11-21', today='2026-11-22')
        self.assertEqual(shell.call_args.kwargs['styles'], ('briefing',))
        self.assertIn('<h1 id="briefing-heading">科技簡報</h1>', html)
        self.assertIn('<time datetime="2026-11-21">2026 年 11 月 21 日</time>', html)
        self.assertIn('1 篇', html)
        self.assertIn('今日簡報尚未發布', html)
        self.assertIn('name="day" value="2026-11-21"', html)
        self.assertIn('<details', html)
        self.assertIn('/discovery/status', html)


if __name__ == '__main__':
    unittest.main()
