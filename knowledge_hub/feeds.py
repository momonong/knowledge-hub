"""Collect a fixed pool of public candidates and record per-source outcomes."""

from __future__ import annotations

import re
from collections import defaultdict
from functools import partial
from typing import Callable
from urllib.error import HTTPError

from .sources import (
    ARXIV_CATEGORY_URLS, ARXIV_FEED_LIMIT, ARXIV_RSS_URLS, HF_DAILY_LATEST_URL, HF_FEED_LABEL, RELEASE_APIS,
    hf_daily_papers_url, hf_papers_url, parse_arxiv_feed, parse_arxiv_rss,
    parse_framework_release, parse_huggingface_papers,
)
from .store import KnowledgeStore, arxiv_version_number


_QUANT_VALIDATION = re.compile(r"\bout[ -]of[ -]sample\b|\bwalk[ -]forward\b", re.I)
_QUANT_MEASURE = re.compile(r"\b(?:backtest\w*|transaction costs?|slippage|limit order book)\b", re.I)


def _qualify_quant(entry: dict) -> bool:
    abstract = entry["blocks"][0]["text"]
    return bool(_QUANT_VALIDATION.search(abstract) and _QUANT_MEASURE.search(abstract))


def _framework_release(raw: bytes, project: str) -> list[dict]:
    entry = parse_framework_release(raw, project)
    return [entry] if entry else []


def _error_message(error: Exception) -> str:
    if isinstance(error, HTTPError):
        return f"公開來源請求失敗（HTTP {error.code}）。"
    if isinstance(error, TimeoutError):
        return "公開來源請求逾時。"
    if isinstance(error, (ValueError, RecursionError)):
        return "公開來源回應超出限制或格式無效。"
    return "無法連線至公開來源。"


def _attempt(safe_fetch, url, parse):
    try:
        return parse(safe_fetch(url)), None
    except (OSError, ValueError, RecursionError) as error:
        return [], error


def _existing_arxiv_versions(store: KnowledgeStore, keys: set[str]) -> set[tuple[str, str, str]]:
    if not keys:
        return set()
    placeholders = ",".join("?" for _ in keys)
    with store._connect() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT items.key, item_versions.label, item_versions.scope
                FROM items JOIN item_versions ON item_versions.item_id = items.id
                WHERE items.key IN ({placeholders})""",
            sorted(keys),
        )
        return {
            (row["key"], row["label"], row["scope"])
            for row in rows if arxiv_version_number(row["label"]) is not None
        }


def collect(store: KnowledgeStore, safe_fetch: Callable[[str], bytes], day: str) -> int:
    """Upsert candidates from fixed feeds for the Taipei calendar day and return changed versions."""
    hf_feeds = {
        "huggingface:daily": hf_daily_papers_url(day),
        "huggingface:monthly": hf_papers_url(day[:7], "trending"),
    }
    arxiv_parser = partial(
        parse_arxiv_feed, require_explicit_version=True, max_entries=ARXIV_FEED_LIMIT, allow_empty=True,
    )
    fetches = {
        **{f"arxiv:{category}": (url, arxiv_parser) for category, url in ARXIV_CATEGORY_URLS.items()},
        **{name: (url, parse_huggingface_papers) for name, url in hf_feeds.items()},
        **{name: (url, partial(_framework_release, project=name)) for name, url in RELEASE_APIS.items()},
    }

    candidates: dict[tuple[str, str], dict] = {}
    origins: dict[tuple[str, str], set[str]] = defaultdict(set)
    failed = set()
    warnings = {}
    atom_throttled = False
    for name, (url, parse) in fetches.items():
        rss_url = ARXIV_RSS_URLS.get(name.removeprefix("arxiv:"))
        if rss_url and atom_throttled:
            entries, error = [], None
            message = "arXiv Atom 已達請求上限（HTTP 429）。"
        else:
            entries, error = _attempt(safe_fetch, url, parse)
            message = _error_message(error) if error else None
        if rss_url and message:
            if isinstance(error, HTTPError) and error.code == 429:
                atom_throttled = True
            entries, rss_error = _attempt(safe_fetch, rss_url, parse_arxiv_rss)
            if entries:
                warnings[name] = message + "已改用官方 arXiv RSS。"
                message = None
            elif rss_error:
                message += "官方 arXiv RSS：" + _error_message(rss_error)
            else:
                message += "官方 arXiv RSS 本次沒有公告。"
        if name == "huggingface:daily" and not entries:
            entries, latest_error = _attempt(safe_fetch, HF_DAILY_LATEST_URL, parse_huggingface_papers)
            if latest_error:
                message = (message or "指定日期沒有 Hugging Face 日榜。") + _error_message(latest_error)
            else:
                if message or entries:
                    note = message or "指定日期沒有 Hugging Face 日榜。"
                    warnings[name] = note + "已改查最新日榜。"
                message = None
        if message:
            failed.add(name)
            store.update_source_state(name, "failed", message)
            continue
        if name == "arxiv:q-fin.TR":
            entries = [entry for entry in entries if _qualify_quant(entry)]
        for entry in entries:
            key = entry["key"], entry["label"]
            selected = candidates.setdefault(key, entry)
            if selected["content_hash"] == entry["content_hash"]:
                origins[key].add(name)

    existing_versions = _existing_arxiv_versions(store, {key for key, _ in candidates if key.startswith("arxiv:")})
    fixed_keys = {key for key, label in candidates if key.startswith("arxiv:") and label != HF_FEED_LABEL}
    fixed_keys.update(key for key, _, _ in existing_versions)

    changed = 0
    changed_sources = set()
    for (key, label), entry in candidates.items():
        if (key, label, entry["scope"]) in existing_versions or (label == HF_FEED_LABEL and key in fixed_keys):
            continue
        _, _, updated = store.upsert_parsed_entry(entry)
        if updated:
            changed += 1
            changed_sources.update(origins[(key, label)])
    for name in fetches.keys() - failed:
        store.update_source_state(name, "updated" if name in changed_sources else "unchanged", warnings.get(name))

    for group, names in (
        ("arxiv", (f"arxiv:{category}" for category in ARXIV_CATEGORY_URLS)),
        ("huggingface", hf_feeds),
    ):
        members = set(names)
        failures = sorted(members & failed)
        if failures:
            store.update_source_state(group, "failed", "部分固定來源無法更新：" + "、".join(failures))
        else:
            store.update_source_state(group, "updated" if members & changed_sources else "unchanged")
    return changed
