"""Acquire three fixed editions through the normal reader and print a public browser manifest."""
import argparse
from collections import Counter
import json
from pathlib import Path

from knowledge_hub import pdf_evidence, visuals
from knowledge_hub.daily import DailyRefresh
from knowledge_hub.editions import ArticleEditions
from knowledge_hub.sources import default_fetch
from knowledge_hub.store import KnowledgeStore


def prepare(path):
    store = KnowledgeStore(path)
    daily = DailyRefresh(store)
    editions = ArticleEditions(store, default_fetch, lambda url: None)
    results = []
    for card in daily.selection(daily.latest_day()):
        item, chosen = card['item'], card['version']
        html = visuals.paper_url(item['key'], chosen['label'])
        if not html:
            continue
        pdf = pdf_evidence.pdf_source(item['key'], chosen['label'])
        version_id, _ = editions.acquire(item, chosen)
        version = store.select_version(store.get_item(item['id'])['versions'], str(version_id))
        assert version['label'] == chosen['label'] and version['scope'] == 'full'
        panels = editions.visual_manifest(html, pdf)
        editions.remember_panels(version_id, panels)
        counts = Counter(p['kind'] for p in panels['panels'])
        assert counts['figure'] and counts['table']
        results.append(dict(item_id=item['id'], version_id=version['id'], key=item['key'], edition=version['label'],
                            scope=version['scope'], blocks=len(version['blocks']), html=html, pdf=pdf, panels=dict(counts)))
        if len(results) == 3:
            return results
    raise ValueError('Daily edition needs three full version-pinned papers')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.db), ensure_ascii=False, indent=2))
