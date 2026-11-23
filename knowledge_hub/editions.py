"""Resolve and cache official arXiv originals for the same fixed edition."""
from collections import Counter
from contextlib import contextmanager
from functools import lru_cache
import json
import hashlib
import os
import re
import stat
from threading import BoundedSemaphore, Lock
from time import monotonic
from unicodedata import normalize
from urllib.error import HTTPError

from . import hive_reader, pdf_evidence, visuals
from .library import directory, read_at
from .sources import (
    HF_FEED_LABEL, arxiv_abs_url, arxiv_metadata_url, compute_content_hash, fetch_response_limit,
    parse_arxiv_abs_version, parse_arxiv_feed, parse_arxiv_html,
)

CACHE_FILES = 48
CACHE_BYTES = 160 * 1024 * 1024
_CACHE_NAME = re.compile(r'[0-9a-f]{64}')
_LABEL = re.compile(r'v[1-9][0-9]{0,8}')
_ABSTRACT = re.compile(r'(?:^|[._:-])abstract[0-9]*(?:[._:-]|$)', re.I)


def _has_readable_body(entry):
    if entry is None:
        return False
    if entry['scope'] == 'full':
        return True
    # A local conversion error does not invalidate intact publisher paragraphs.
    # Prefer an anchored paragraph, or several unanchored sentences. Ignore
    # abstract-only content and short publisher notices.
    unanchored_letters = 0
    for block in entry['blocks']:
        if _ABSTRACT.search(block['anchor']):
            continue
        letters = sum(character.isalpha() for character in block['text'])
        if letters >= 40 and '#' in block['source_url']:
            return True
        unanchored_letters += letters
    return unanchored_letters >= 160


def _needs_pdf(panel):
    return bool(panel.get('image_cells')) or not (panel['images'] or panel['rows'])


def _caption_key(panel):
    parts = visuals.caption_parts(panel['caption'])
    if parts is None:
        return None
    kind, label, tail = parts
    # Normalize the LaTeXML / Poppler presentation differences without using
    # fuzzy similarity or merging two panels solely by their author numbers.
    tail = normalize('NFKC', tail.replace(r'\pm', '±').replace(r'\times', '×')).casefold()
    text = ''.join(character for character in tail if character.isalnum() or character == '%')
    return (kind, label, text) if text else None


def _pdf_origin(panel):
    if panel.get('pdf_origin'):
        return panel['pdf_origin']
    if panel.get('representation') == 'pdf_page':
        return {key:panel[key] for key in ('anchor','page_url','page_number','caption')}
    return {}


def _merge_visuals(panels, fallback):
    html_keys = [_caption_key(panel) for panel in panels]
    pdf_keys = [_caption_key(panel) for panel in fallback]
    html_counts, pdf_counts = Counter(html_keys), Counter(pdf_keys)
    pdf_matches = {key:index for index, key in enumerate(pdf_keys)
                   if key is not None and pdf_counts[key] == html_counts[key] == 1}
    paired = set()
    result = []
    for panel, key in zip(panels, html_keys, strict=True):
        index = pdf_matches.get(key)
        if index is not None:
            paired.add(index)
        if not _needs_pdf(panel):
            result.append({**panel,'pdf_origin':_pdf_origin(fallback[index])} if index is not None else panel)
        elif index is not None:
            pdf = fallback[index]
            result.append({**pdf, 'anchor':panel['anchor'] or pdf['anchor'], 'pdf_origin':_pdf_origin(pdf),
                           'references':panel.get('references',[])})
        else:
            result.append({**panel,'incomplete':True,'reason':'pdf_panel_not_verified'})
    result.extend(panel for index, panel in enumerate(fallback) if index not in paired)
    return result


class EditionError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def failure_code(error):
    if isinstance(error, (EditionError, pdf_evidence.PDFEvidenceError)):
        return error.code
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, HTTPError):
        return 'missing' if error.code == 404 else 'upstream_http'
    return 'fetch_failed' if isinstance(error, OSError) else 'invalid_source'


class ArticleEditions:
    def __init__(self, store, fetch, local_html):
        self.store = store
        self.fetch = fetch
        self.local_html = local_html
        self.root = store.db_path.parent / 'article-editions'
        self._locks = tuple(Lock() for _ in range(16))
        self._disk_lock = Lock()
        self._pdf_slots = BoundedSemaphore(1)
        # Keep successful parses until eviction and briefly share upstream failures.
        # Store parsed data rather than several large PDF buffers in memory.
        self._html = lru_cache(maxsize=8)(self._parse_html)
        self._try_html = lru_cache(maxsize=16)(self._load_html)
        self._pdf = lru_cache(maxsize=8)(self._parse_pdf)
        self._try_pdf = lru_cache(maxsize=16)(self._load_pdf)
        self._page = lru_cache(maxsize=4)(self._render_page)

    def remember_panels(self, version_id, manifest):
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT manifest FROM version_visuals WHERE version_id=?', (version_id,)).fetchone()
            saved = json.loads(old[0]) if old else {}
            if saved.get('source') != manifest.get('source'):
                saved = {}
            if manifest.get('state') != 'ready':
                return saved or manifest
            if saved and saved != manifest:
                current = list(manifest['panels'])
                # Keep published bookmarks when a fixed edition later gains HTML or PDF.
                # Pair by a unique complete caption; ambiguous originals keep separate IDs.
                keys = [_caption_key(panel) for panel in current]
                previous = saved['panels']
                old_keys = Counter(_caption_key(panel) for panel in previous)
                anchors = {panel['anchor']:i for i,panel in enumerate(current)}
                for panel in previous:
                    key = _caption_key(panel)
                    index = anchors.get(panel['anchor'])
                    if index is None and key and old_keys[key] == 1 and keys.count(key) == 1:
                        index = keys.index(key)
                    if index is None:
                        if len(current) < visuals.MAX_PANELS:
                            current.append(panel)
                        continue
                    latest = current[index]
                    origin = _pdf_origin(latest) or _pdf_origin(panel)
                    if origin:
                        anchor = panel['anchor'] if latest['anchor'] == origin['anchor'] else latest['anchor']
                        current[index] = {**latest,'anchor':anchor,'pdf_origin':origin}
                manifest = {**manifest,'panels':current}
            conn.execute("""INSERT INTO version_visuals VALUES (?,?) ON CONFLICT(version_id)
                         DO UPDATE SET manifest=excluded.manifest""",
                         (version_id, json.dumps(manifest, ensure_ascii=False)))
        return manifest

    def saved_panels(self, version_id):
        with self.store._connect() as conn:
            row = conn.execute('SELECT manifest FROM version_visuals WHERE version_id=?', (version_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _lock(self, url):
        return self._locks[hash(url) % len(self._locks)]

    @staticmethod
    def _cache_key(url):
        return hashlib.sha256(url.encode()).hexdigest()

    def _raw(self, url):
        limit = fetch_response_limit(url)
        try:
            with directory(self.root) as fd:
                return read_at(fd, self._cache_key(url), limit), True
        except FileNotFoundError:
            return self.fetch(url), False

    def _cached_original(self, url):
        if not url:
            return False
        try:
            with directory(self.root) as fd:
                info = os.stat(self._cache_key(url), dir_fd=fd, follow_symlinks=False)
                return stat.S_ISREG(info.st_mode)
        except FileNotFoundError:
            return False

    def _save(self, url, raw):
        key = self._cache_key(url)
        with self._disk_lock, directory(self.root, create=True) as fd:
            pending = '.tmp-' + os.urandom(12).hex()
            rawfd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(rawfd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(pending, key, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(pending, dir_fd=fd)
                except FileNotFoundError:
                    pass
            with os.scandir(fd) as entries:
                records = [(file.stat(follow_symlinks=False), file.name) for file in entries
                           if _CACHE_NAME.fullmatch(file.name) and file.is_file(follow_symlinks=False)]
            total, count = sum(info.st_size for info, _ in records), len(records)
            for info, name in sorted(records, key=lambda record: record[0].st_mtime_ns):
                if count <= CACHE_FILES and total <= CACHE_BYTES:
                    break
                if name != key:
                    os.unlink(name, dir_fd=fd)
                    total -= info.st_size
                    count -= 1

    def _parse_html(self, source):
        local = self.local_html(source)
        raw, cached = (local, True) if local is not None else self._raw(source)
        try:
            entry = parse_arxiv_html(raw, source)
        except ValueError:
            entry = None
        try:
            manifest = visuals.extract(raw, source)
        except ValueError:
            manifest = None
        if manifest is not None and not manifest['panels']:
            manifest = None
        if entry is None and manifest is None:
            raise EditionError('no_usable_html')
        if not cached:
            self._save(source, raw)
        return entry, manifest

    @contextmanager
    def _pdf_slot(self):
        if not self._pdf_slots.acquire(blocking=False):
            raise EditionError('busy')
        try:
            yield
        finally:
            self._pdf_slots.release()

    def _parse_pdf(self, source, url):
        with self._pdf_slot():
            raw, cached = self._raw(url)
            entry, manifest = pdf_evidence.parse(raw, source)
            if entry.get('source_url') != url or manifest.get('pdf_url') != url:
                raise EditionError('edition_mismatch')
            if not cached:
                self._save(url, raw)
            return entry, manifest, hashlib.sha256(raw).hexdigest()

    def _render_page(self, pdf_url, digest, page):
        with self._pdf_slot():
            raw, cached = self._raw(pdf_url)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise EditionError('edition_mismatch')
            png = pdf_evidence.render_page(raw, page)
            if not cached:
                self._save(pdf_url, raw)
            return png

    def _load_html(self, source, time_window):
        try:
            return self._html(source), 'ready'
        except (OSError, ValueError) as error:
            return (None, None), failure_code(error)

    def _load_pdf(self, source, pdf_url, time_window):
        try:
            return self._pdf(source, pdf_url), 'ready'
        except (OSError, ValueError) as error:
            reason = failure_code(error)
            if reason == 'busy':
                raise
            return (None, None, None), reason

    def originals(self, source, pdf_url, *, with_panels=False):
        """Return the preferred readable text and failure codes by channel."""
        window = int(monotonic()) // 300
        with self._lock(source):
            (html, html_panels), html_status = self._try_html(source, window)
            if _has_readable_body(html):
                channels = {'html_state':html['fulltext_status'], 'pdf_state':'not_needed'}
                if with_panels and html_panels:
                    channels['parsed_panels'] = {**html_panels,'state':'ready'}
                return html, channels
            html_state = 'no_readable_html' if html_status == 'ready' else html_status
            try:
                (entry, pdf_panels, _), pdf_state = self._try_pdf(source, pdf_url, window)
            except (OSError, ValueError) as error:
                entry, pdf_panels, pdf_state = None, None, failure_code(error)
            channels = {'html_state':html_state, 'pdf_state':'partial' if entry else pdf_state}
            if with_panels and pdf_panels:
                channels['parsed_panels'] = pdf_panels
            return entry, channels

    def visual_manifest(self, source, pdf_url):
        window = int(monotonic()) // 300
        with self._lock(source):
            (entry, html), html_status = self._try_html(source, window)
            complete_panels = html and not any(map(_needs_pdf, html['panels']))
            if complete_panels and source == hive_reader.PAPER:
                # This pinned gallery's nine indices are saved reading anchors.
                return {**html, 'state':'ready'}
            if (complete_panels and not html['truncated'] and (entry is None or entry['scope'] == 'full')
                    and not self._cached_original(pdf_url)):
                return {**html, 'state':'ready', 'pdf_state':'not_checked'}
            html_state = 'no_visuals' if html_status == 'ready' else html_status
            if html:
                html_state = 'ready' if complete_panels and not html['truncated'] else 'incomplete'
            (_, manifest, _), reason = self._try_pdf(source, pdf_url, window)
            fallback = manifest['panels'] if manifest else []
            if html and (fallback or any(not _needs_pdf(panel) for panel in html['panels'])):
                return {**html,'state':'ready','html_state':html_state,'pdf_url':pdf_url,
                        'pdf_state':(manifest.get('reason') or 'ready') if manifest else reason,
                        'truncated':html['truncated'] or bool(manifest and manifest['truncated']),
                        'panels':_merge_visuals(html['panels'], fallback)}
            if manifest is not None:
                return {**manifest, 'html_state':html_state}
            return {'state':'unavailable', 'reason':'pdf_' + reason, 'source':source,
                    'pdf_url':pdf_url, 'html_state':html_state, 'panels':[]}

    def render_page(self, source, pdf_url, page, manifest):
        if not any(panel.get('representation') == 'pdf_page' and panel.get('page_number') == page
                   for panel in manifest['panels']):
            raise EditionError('page_not_in_manifest')
        with self._lock(source):
            _, _, digest = self._pdf(source, pdf_url)
            return self._page(pdf_url, digest, page)

    def original_pdf(self, source, pdf_url):
        with self._lock(source):
            _, _, digest = self._pdf(source, pdf_url)
            raw, _ = self._raw(pdf_url)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise EditionError('edition_mismatch')
            return raw

    def _latest(self, key):
        paper_id = key.removeprefix('arxiv:')
        try:
            atom = self.fetch(arxiv_metadata_url(paper_id))
            matches = [entry for entry in parse_arxiv_feed(atom, require_explicit_version=True)
                       if entry['key'] == key and _LABEL.fullmatch(entry['label'])]
            if len(matches) == 1:
                return matches[0]['label'], matches[0]
        except (OSError, ValueError):
            pass
        try:
            return parse_arxiv_abs_version(self.fetch(arxiv_abs_url(paper_id)), paper_id), {}
        except (OSError, ValueError):
            raise EditionError('edition_unresolved') from None

    def pin_abstract(self, pick):
        """Prefer the publisher's verified abstract over an undated index edition."""
        if pick['version']['label'] != HF_FEED_LABEL:
            return pick
        try:
            _, metadata = self._latest(pick['item']['key'])
        except EditionError:
            return pick
        if not metadata:
            return pick
        metadata['published_at'] = metadata.get('published_at') or pick['version']['published_at']
        _, version_id, _ = self.store.upsert_parsed_entry(metadata)
        return {**pick, 'item': {**pick['item'], 'title': metadata['title']},
                'version': {**metadata, 'id': version_id}}

    def acquire(self, item, version, *, latest=False):
        previous = version['id']
        label = version['label']
        metadata = {}
        if label == HF_FEED_LABEL or latest:
            try:
                label, metadata = self._latest(item['key'])
            except EditionError as error:
                self._record_failure(item['id'])
                return previous, {'state':'unavailable', 'reason':error.code}
        source = visuals.paper_url(item['key'], label)
        pdf_url = pdf_evidence.pdf_source(item['key'], label)
        if not source or not pdf_url:
            return previous, {'state':'unavailable', 'reason':'edition_unresolved'}
        entry, channels = self.originals(source, pdf_url, with_panels=True)
        panels = channels.pop('parsed_panels',None)
        if entry is not None and entry['key'] == item['key'] and entry['label'] == label:
            entry = {**entry, 'published_at': metadata.get('published_at') or version.get('published_at') or '',
                     'author': entry['author'] or metadata.get('author') or version.get('author') or ''}
            # Some PDFs lack title metadata. Keep the known publication title.
            if entry['title'] == f'arXiv {item["key"].split(":", 1)[1]}{label}':
                entry['title'] = metadata.get('title') or item['title']
                entry['content_hash'] = compute_content_hash(entry['title'], entry['scope'], label, entry['blocks'])
            with self._lock('store:' + item['key']):
                _, saved, _ = self.store.upsert_parsed_entry(entry)
            if panels and panels.get('state') == 'ready':
                self.remember_panels(saved,panels)
            return saved, {'state':entry['fulltext_status'], **channels}

        # A verified Atom abstract is still useful; never replace existing full text with it.
        if metadata:
            with self._lock('store:' + item['key']):
                _, saved, _ = self.store.upsert_parsed_entry(metadata)
        else:
            saved = previous
        if channels['pdf_state'] != 'busy':
            self._record_failure(item['id'])
        return saved, {'state':'unavailable', 'reason':'originals_unavailable', **channels}

    def _record_failure(self, item_id):
        with self.store._connect() as conn:
            conn.execute("UPDATE items SET fulltext_status='failed' WHERE id=? "
                         "AND fulltext_status NOT IN ('ready','partial')", (item_id,))

    def claim_prefetch(self, day):
        with self.store._connect() as conn:
            return bool(conn.execute('''INSERT INTO meta(key,value) VALUES ('article-edition-prefetch-day',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value<excluded.value
                ''', (day,)).rowcount)
