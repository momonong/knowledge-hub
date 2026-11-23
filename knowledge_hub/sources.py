"""Bounded HTTPS source fetching, URL validation, and content parsers."""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

RELEASE_REPOS = {
    "pytorch": "pytorch/pytorch",
    "vllm": "vllm-project/vllm",
    "sglang": "sgl-project/sglang",
    "tensorrt-llm": "NVIDIA/TensorRT-LLM",
    "transformers": "huggingface/transformers",
}
RELEASE_APIS = {
    name: f"https://api.github.com/repos/{repo}/releases/latest"
    for name, repo in RELEASE_REPOS.items()
}
PYTORCH_RELEASE_API = RELEASE_APIS["pytorch"]
HF_PAPERS_API = "https://huggingface.co/api/daily_papers"
_HF_DAILY_QUERY = urlencode({"sort": "publishedAt", "limit": 50, "p": 0})
HF_DAILY_LATEST_URL = f"{HF_PAPERS_API}?{_HF_DAILY_QUERY}"
HF_FEED_LABEL = 'hf-index'
PUBLIC_PROJECTS_URL = 'https://api.github.com/users/momonong/repos?type=owner&sort=pushed&direction=desc&per_page=30'
ARXIV_QUERY_BASE = "https://export.arxiv.org/api/query"
ARXIV_CATEGORIES = ("cs.CL", "cs.LG", "cs.AI", "cs.CV", "cs.RO", "cs.DC", "cs.PF", "q-fin.TR")
ARXIV_FEED_LIMIT = 10
ARXIV_CATEGORY_URLS = {
    category: ARXIV_QUERY_BASE + "?" + urlencode({
        "search_query": f"cat:{category}",
        "max_results": str(ARXIV_FEED_LIMIT),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    for category in ARXIV_CATEGORIES
}
ARXIV_QUERY_URL = ARXIV_CATEGORY_URLS["cs.LG"]
ARXIV_RSS_URLS = {category: f"https://rss.arxiv.org/rss/{category}" for category in ARXIV_CATEGORIES}
PYTHON_314_DOC_URL = "https://docs.python.org/3.14/whatsnew/3.14.html"

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_PDF_RESPONSE_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
ARXIV_MIN_INTERVAL = 3.0

_ARXIV_ID_PART = r"(?:[0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})"
ARXIV_HTML_PATH_RE = re.compile(rf"^/html/({_ARXIV_ID_PART})v([1-9]\d*)$")
ARXIV_PDF_PATH_RE = re.compile(rf"^/pdf/{_ARXIV_ID_PART}v[1-9][0-9]{{0,8}}$")
ARXIV_ABS_PATH_RE = re.compile(rf"^/abs/{_ARXIV_ID_PART}$")
ARXIV_ASSET_PATH_RE = re.compile(rf"^/html/{_ARXIV_ID_PART}v[1-9]\d*/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]{{0,95}}/)*[A-Za-z0-9_-][A-Za-z0-9_.-]{{0,120}}\.(?:png|jpe?g|webp|svg)$")
ARXIV_ABS_URL_RE = re.compile(
    rf"^https?://(?:export\.)?arxiv\.org/abs/({_ARXIV_ID_PART})(?:v([1-9][0-9]{{0,8}}))?$"
)
SAFE_ANCHOR_RE = re.compile(r"^[A-Za-z0-9_.\-:]+$")
XML_DECL_BAN_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
SCRIPT_STYLE_RE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")

_arxiv_rate_lock = threading.Lock()
_last_arxiv_request_mono: float | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hf_iso_date(day: str) -> date:
    if not isinstance(day, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", day):
        raise ValueError("Invalid Hugging Face daily date")
    return date.fromisoformat(day)


def _hf_dated_papers_url(published_day: date) -> str:
    return f"{HF_PAPERS_API}?date={published_day.isoformat()}&{_HF_DAILY_QUERY}"


def hf_daily_papers_url(day: str) -> str:
    """Use the prior weekday so a Taipei morning run avoids an unpublished edition."""
    published_day = _hf_iso_date(day) - timedelta(days=1)
    while published_day.weekday() >= 5:
        published_day -= timedelta(days=1)
    return _hf_dated_papers_url(published_day)


def hf_papers_url(month, sort):
    if not re.fullmatch(r'20[0-9]{2}-(?:0[1-9]|1[0-2])', month) or sort not in ('publishedAt','trending'):
        raise ValueError('Invalid Hugging Face feed selector')
    return HF_PAPERS_API + '?' + urlencode({'month':month,'sort':sort,'limit':50,'p':0})


def arxiv_metadata_url(paper_id):
    if not re.fullmatch(_ARXIV_ID_PART, paper_id):
        raise ValueError('Invalid arXiv identifier')
    return ARXIV_QUERY_BASE + '?' + urlencode({'id_list':paper_id, 'max_results':1})


def arxiv_abs_url(paper_id):
    if not re.fullmatch(_ARXIV_ID_PART, paper_id):
        raise ValueError('Invalid arXiv identifier')
    return 'https://arxiv.org/abs/' + paper_id


class _ArxivEditionMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.identifiers = []
        self.pages = []

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        fields = dict(attrs)
        content = fields.get('content') or ''
        if fields.get('name') == 'citation_arxiv_id':
            self.identifiers.append(content)
        if fields.get('property') == 'og:url':
            self.pages.append(content)


def parse_arxiv_abs_version(raw, paper_id):
    """Require two same-paper metadata fields and an explicit, consistent vN."""
    metadata = _ArxivEditionMetadata()
    metadata.feed(raw.decode('utf-8'))
    metadata.close()
    if len(metadata.identifiers) != 1 or len(metadata.pages) != 1:
        raise ValueError('Missing or ambiguous arXiv edition metadata')
    identifier = re.fullmatch(rf'{re.escape(paper_id)}(?:v([1-9][0-9]{{0,8}}))?',
                              metadata.identifiers[0])
    page = re.fullmatch(re.escape(arxiv_abs_url(paper_id)) + r'(?:v([1-9][0-9]{0,8}))?',
                        metadata.pages[0])
    revisions = {field[1] for field in (identifier, page) if field and field[1]}
    if not identifier or not page or len(revisions) != 1:
        raise ValueError('The arXiv edition is missing or does not match both metadata fields')
    return 'v' + revisions.pop()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "HTTP redirects are prohibited", headers, fp
        )


def _check_basic_https_url(url: str):
    if not isinstance(url, str) or not url:
        raise ValueError("URL must be a non-empty string")
    if url != url.strip() or any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        raise ValueError("URL contains whitespace or control characters")
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("Only https:// URLs are allowed")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("URL userinfo is prohibited")
    if parsed.fragment:
        raise ValueError("URL fragments are prohibited")
    return parsed


def validate_manual_url(url: str) -> tuple[str, str]:
    """Validate URL for manual addition (`POST /add`).

    Returns `(kind, canonical_url)` where `kind` is `'python'` or `'arxiv'`.
    Raises `ValueError` if not strictly in the allowlist.
    """
    parsed = _check_basic_https_url(url)
    if parsed.query:
        raise ValueError("Query parameters are not allowed for manual URLs")

    if url == PYTHON_314_DOC_URL:
        return ("python", PYTHON_314_DOC_URL)

    if parsed.netloc == "arxiv.org":
        match = ARXIV_HTML_PATH_RE.fullmatch(parsed.path)
        if match:
            return ("arxiv", url)

    raise ValueError("URL is not in the allowed manual source list")


def validate_fetch_url(url: str) -> str:
    """Validate any outgoing URL before fetching."""
    parsed = _check_basic_https_url(url)

    if url in RELEASE_APIS.values() or url in ARXIV_CATEGORY_URLS.values() or url in ARXIV_RSS_URLS.values():
        return url

    if url == PYTHON_314_DOC_URL and not parsed.query:
        return url

    if url in (PUBLIC_PROJECTS_URL, HF_DAILY_LATEST_URL):
        return url

    if (parsed.netloc, parsed.path) == ('huggingface.co','/api/daily_papers'):
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            expected = (_hf_dated_papers_url(_hf_iso_date(query['date'][0])) if 'date' in query
                        else hf_papers_url(query['month'][0], query['sort'][0]))
            if url == expected:
                return url
        except (KeyError, ValueError):
            pass

    if parsed.netloc == "arxiv.org" and not parsed.query:
        if (ARXIV_HTML_PATH_RE.fullmatch(parsed.path)
                or (len(url) <= 512 and ARXIV_ASSET_PATH_RE.fullmatch(parsed.path))):
            return url
        if url == 'https://arxiv.org' + parsed.path and (
                ARXIV_PDF_PATH_RE.fullmatch(parsed.path) or ARXIV_ABS_PATH_RE.fullmatch(parsed.path)):
            return url

    if (parsed.netloc, parsed.path) == ("export.arxiv.org", "/api/query"):
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.keys() == {'id_list','max_results'} and len(query['id_list']) == 1:
            if url == arxiv_metadata_url(query['id_list'][0]):
                return url

    raise ValueError(f"Destination URL is not permitted: {url}")


def fetch_response_limit(url: str) -> int:
    validated = urlsplit(validate_fetch_url(url))
    if validated.netloc == 'arxiv.org' and ARXIV_PDF_PATH_RE.fullmatch(validated.path):
        return MAX_PDF_RESPONSE_BYTES
    return MAX_RESPONSE_BYTES


def default_fetch(url: str) -> bytes:
    """Bound HTTPS fetches without redirects. Fixed arXiv PDFs have their own cap."""
    global _last_arxiv_request_mono
    validate_fetch_url(url)
    parsed = urlsplit(url)
    is_arxiv_api = parsed.netloc == "export.arxiv.org"

    if is_arxiv_api:
        with _arxiv_rate_lock:
            now = time.monotonic()
            if _last_arxiv_request_mono is not None:
                elapsed = now - _last_arxiv_request_mono
                if elapsed < ARXIV_MIN_INTERVAL:
                    time.sleep(ARXIV_MIN_INTERVAL - elapsed)
            try:
                return _perform_https_get(url)
            finally:
                _last_arxiv_request_mono = time.monotonic()
    return _perform_https_get(url)


def _perform_https_get(url: str) -> bytes:
    limit = fetch_response_limit(url)
    pdf = limit == MAX_PDF_RESPONSE_BYTES
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        _NoRedirectHandler(),
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KnowledgeHub/1.0 (Local Single-User Reader)",
            "Accept": ("application/pdf" if pdf else
                       "image/png, image/jpeg, image/webp, image/svg+xml"
                       if ARXIV_ASSET_PATH_RE.fullmatch(urlsplit(url).path)
                       else "application/json, application/atom+xml, application/rss+xml, text/html;q=0.9"),
        },
        method="GET",
    )
    with opener.open(req, timeout=DEFAULT_TIMEOUT) as resp:
        status = getattr(resp, "status", 200)
        if status != 200:
            raise OSError(f"Unexpected HTTP status {status}")
        content_length = resp.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    raise ValueError(f"Response Content-Length exceeds {limit // (1024*1024)} MiB limit")
            except ValueError as err:
                if "exceeds" in str(err):
                    raise
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError(f"Response body exceeds {limit // (1024*1024)} MiB limit")
            chunks.append(chunk)
        return b"".join(chunks)


def compute_content_hash(title: str, scope: str, label: str, blocks: list[dict]) -> str:
    payload = {
        "title": title,
        "scope": scope,
        "label": label,
        "blocks": [{"anchor": b["anchor"], "text": b["text"]} for b in blocks],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_inline(text: str) -> str:
    if not text:
        return ""
    cleaned = SCRIPT_STYLE_RE.sub(" ", text)
    cleaned = HTML_TAG_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_huggingface_papers(raw):
    """Accept abstracts from the public index and retain their unknown edition."""
    entries = json.loads(raw.decode('utf-8'))
    if not isinstance(entries,list) or len(entries) > 50:
        raise ValueError('Unexpected Hugging Face paper feed')
    results = []
    for row in entries:
        paper = row.get('paper') if isinstance(row, dict) else None
        if not isinstance(paper,dict):
            continue
        ident, title, abstract, date = (paper.get(k) for k in ('id','title','summary','publishedAt'))
        if not all(isinstance(x,str) for x in (ident,title,abstract,date)) or not re.fullmatch(_ARXIV_ID_PART,ident):
            continue
        try:
            published = datetime.fromisoformat(date.replace('Z','+00:00'))
            if published.tzinfo is None:
                continue
            published = published.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except (ValueError, OverflowError):
            continue
        title = _normalize_inline(title[:400])
        abstract = _normalize_inline(abstract[:12000])
        if not title or not abstract:
            continue
        source = 'https://huggingface.co/papers/' + ident
        blocks = [{'anchor':'abstract','text':abstract,'source_url':source}]
        results.append({'key':'arxiv:'+ident, 'source_name':'huggingface','title':title,'label':HF_FEED_LABEL,
                        'scope':'abstract','source_url':source,'published_at':published,'blocks':blocks,
                        'content_hash':compute_content_hash(title,'abstract',HF_FEED_LABEL,blocks)})
    if entries and not results:
        raise ValueError('Hugging Face feed contains no usable paper metadata')
    return results


def _slugify_local_anchor(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_\-]+", "-", text.strip().lower()).strip("-")
    if not slug:
        return fallback
    return slug[:64]


_RELEASE_PROJECT_NAMES = {
    "pytorch": "PyTorch",
    "vllm": "vLLM",
    "sglang": "SGLang",
    "tensorrt-llm": "TensorRT-LLM",
    "transformers": "Hugging Face Transformers",
}
_RELEASE_TAG = re.compile(r"v?[0-9]+\.[0-9]+\.([0-9]+)(?:\.post[0-9]+)?", re.I)
_RELEASE_IMPACT = re.compile(
    r"\b(?:add(?:s|ed)?|support(?:s|ed)?|enable(?:s|d)?|introduc\w*|improv\w*|optimi[sz]\w*|"
    r"accelerat\w*|reduc\w*|lower\w*|break\w*|deprecat\w*|fix\w*)\b", re.I,
)
_RELEASE_TECH = re.compile(
    r"\b(?:inference|throughput|latency|kv[ -]cache|quantiz\w*|kernel|cuda|rocm|tensorrt|"
    r"distributed|parallelism|speculative decoding|multimodal|compiler|backward compatibility)\b", re.I,
)


def _release_json(raw: bytes) -> dict:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Release payload must be a JSON object")
    return data


def parse_pytorch_release(raw: bytes) -> dict:
    """Parse the existing PyTorch release source."""
    return _parse_github_release(_release_json(raw), "pytorch")


def parse_framework_release(raw: bytes, project: str) -> dict | None:
    """Keep stable feature releases and patches with concrete engineering impact."""
    data = _release_json(raw)
    entry = _parse_github_release(data, project)
    tag = _RELEASE_TAG.fullmatch(entry["label"])
    if data.get("draft") or data.get("prerelease") or not tag:
        return None
    if entry["scope"] != "full":
        # Preserve the existing PyTorch catalogue entry and its unavailable scope.
        return entry if project == "pytorch" else None
    if tag[1] == "0" and ".post" not in entry["label"].lower():
        return entry
    if any(_RELEASE_IMPACT.search(block["text"]) and _RELEASE_TECH.search(block["text"])
           for block in entry["blocks"]):
        return entry
    return None


def _parse_github_release(data: dict, project: str) -> dict:
    tag_name = data.get("tag_name")
    html_url = data.get("html_url")
    if not isinstance(tag_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", tag_name):
        raise ValueError("Missing or invalid release tag")
    if html_url != f"https://github.com/{RELEASE_REPOS[project]}/releases/tag/{tag_name}":
        raise ValueError("Release URL does not match the source repository and tag")

    raw_name = data.get("name")
    title = _normalize_inline(raw_name) if isinstance(raw_name, str) else ""
    if not title or title.casefold() == tag_name.casefold():
        title = f"{_RELEASE_PROJECT_NAMES[project]} {tag_name}"

    author_obj = data.get("author")
    author = ""
    if isinstance(author_obj, dict) and isinstance(author_obj.get("login"), str):
        author = _normalize_inline(author_obj["login"])

    published_at = ""
    if isinstance(data.get("published_at"), str) and data["published_at"].strip():
        published_at = data["published_at"].strip()
    elif isinstance(data.get("updated_at"), str) and data["updated_at"].strip():
        published_at = data["updated_at"].strip()

    raw_body = data.get("body") if isinstance(data.get("body"), str) else ""
    body_no_scripts = SCRIPT_STYLE_RE.sub("", raw_body)

    blocks: list[dict] = []
    current_section_slug = "overview"
    section_para_count: dict[str, int] = {}
    used_anchors: set[str] = set()

    for raw_part in re.split(r"\n\s*\n", body_no_scripts):
        part = raw_part.strip()
        if not part:
            continue
        lines = [line.strip() for line in part.splitlines() if line.strip()]
        if not lines:
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", lines[0])
        if heading_match:
            heading_text = _normalize_inline(heading_match.group(2))
            if heading_text:
                base_slug = _slugify_local_anchor(
                    heading_text, f"section-{len(blocks) + 1}"
                )
                anchor = base_slug
                suffix = 2
                while anchor in used_anchors:
                    anchor = f"{base_slug}-{suffix}"
                    suffix += 1
                used_anchors.add(anchor)
                current_section_slug = anchor
                blocks.append(
                    {
                        "anchor": anchor,
                        "text": heading_text,
                        "source_url": html_url,
                    }
                )
            rest_text = _normalize_inline(" ".join(lines[1:]))
            if rest_text:
                idx = section_para_count.get(current_section_slug, 0) + 1
                section_para_count[current_section_slug] = idx
                p_anchor = f"{current_section_slug}.p{idx}"
                while p_anchor in used_anchors:
                    idx += 1
                    p_anchor = f"{current_section_slug}.p{idx}"
                used_anchors.add(p_anchor)
                blocks.append(
                    {
                        "anchor": p_anchor,
                        "text": rest_text,
                        "source_url": html_url,
                    }
                )
        else:
            para_text = _normalize_inline(" ".join(lines))
            if not para_text:
                continue
            idx = section_para_count.get(current_section_slug, 0) + 1
            section_para_count[current_section_slug] = idx
            p_anchor = f"{current_section_slug}.p{idx}"
            while p_anchor in used_anchors:
                idx += 1
                p_anchor = f"{current_section_slug}.p{idx}"
            used_anchors.add(p_anchor)
            blocks.append(
                {
                    "anchor": p_anchor,
                    "text": para_text,
                    "source_url": html_url,
                }
            )

    if blocks:
        scope = "full"
        fulltext_status = "ready"
    else:
        scope = "unavailable"
        fulltext_status = "unavailable"
        blocks = [
            {
                "anchor": "unavailable",
                "text": "此版本未提供可讀取的正式說明正文。",
                "source_url": html_url,
            }
        ]

    content_hash = compute_content_hash(title, scope, tag_name, blocks)
    return {
        "key": f"{project}:{tag_name}",
        "source_name": project,
        "title": title,
        "label": tag_name,
        "scope": scope,
        "fulltext_status": fulltext_status,
        "source_url": html_url,
        "author": author,
        "published_at": published_at,
        "blocks": blocks,
        "content_hash": content_hash,
    }


def _parse_arxiv_xml(raw: bytes) -> ET.Element:
    if XML_DECL_BAN_RE.search(raw):
        raise ValueError("XML DTD or ENTITY declarations are prohibited")
    try:
        return ET.fromstring(raw.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError) as err:
        raise ValueError("Invalid arXiv XML") from err


def _arxiv_abstract(base_id, version_num, title, summary, author, published_at):
    label = "v" + version_num
    source_url = f"https://arxiv.org/abs/{base_id}{label}"
    blocks = [{"anchor": "abstract", "text": summary, "source_url": source_url}]
    return {
        "key": f"arxiv:{base_id}",
        "arxiv_id": base_id,
        "version_num": version_num,
        "source_name": "arxiv",
        "title": title,
        "label": label,
        "scope": "abstract",
        "fulltext_status": "not_requested",
        "source_url": source_url,
        "author": author,
        "published_at": published_at,
        "blocks": blocks,
        "content_hash": compute_content_hash(title, "abstract", label, blocks),
    }


def parse_arxiv_feed(
    raw: bytes, *, require_explicit_version: bool = False, max_entries: int | None = None,
    allow_empty: bool = False,
) -> list[dict]:
    """Parse arXiv Atom XML feed, rejecting DTD/ENTITY declarations."""
    root = _parse_arxiv_xml(raw)
    atom_ns = "{http://www.w3.org/2005/Atom}"
    if root.tag not in (f"{atom_ns}feed", "feed"):
        raise ValueError("Unexpected Atom root element")

    entries = root.findall(f"{atom_ns}entry")
    if not entries:
        entries = root.findall("entry")
    if not entries and not allow_empty:
        raise ValueError("arXiv Atom feed contains no entries")

    if max_entries is not None and len(entries) > max_entries:
        raise ValueError("arXiv Atom feed exceeds its entry limit")

    results: list[dict] = []
    for entry in entries:
        id_elem = entry.find(f"{atom_ns}id")
        if id_elem is None:
            id_elem = entry.find("id")
        raw_id = (id_elem.text or "").strip() if id_elem is not None else ""
        id_match = ARXIV_ABS_URL_RE.fullmatch(raw_id)
        if not id_match:
            raise ValueError(f"Malformed arXiv entry id: {raw_id}")

        base_id = id_match.group(1)
        if require_explicit_version and id_match.group(2) is None:
            continue
        version_num = id_match.group(2) or "1"

        title_elem = entry.find(f"{atom_ns}title")
        if title_elem is None:
            title_elem = entry.find("title")
        title = _normalize_inline(title_elem.text if title_elem is not None else "")
        if not title:
            raise ValueError("Missing arXiv entry title")

        summary_elem = entry.find(f"{atom_ns}summary")
        if summary_elem is None:
            summary_elem = entry.find("summary")
        summary = _normalize_inline(summary_elem.text if summary_elem is not None else "")
        if not summary:
            raise ValueError("Missing arXiv entry summary")

        published_elem = entry.find(f"{atom_ns}published")
        if published_elem is None:
            published_elem = entry.find("published")
        updated_elem = entry.find(f"{atom_ns}updated")
        if updated_elem is None:
            updated_elem = entry.find("updated")

        published_at = ""
        if updated_elem is not None and (updated_elem.text or "").strip():
            published_at = updated_elem.text.strip()
        elif published_elem is not None and (published_elem.text or "").strip():
            published_at = published_elem.text.strip()

        author_names: list[str] = []
        author_elems = entry.findall(f"{atom_ns}author") or entry.findall("author")
        for author_el in author_elems:
            name_el = author_el.find(f"{atom_ns}name")
            if name_el is None:
                name_el = author_el.find("name")
            if name_el is not None and (name_el.text or "").strip():
                author_names.append(_normalize_inline(name_el.text))
        author = ", ".join(author_names)

        results.append(_arxiv_abstract(base_id, version_num, title, summary, author, published_at))
    return results


_RSS_ABSTRACT = re.compile(
    rf"^arXiv:({_ARXIV_ID_PART})v([1-9][0-9]{{0,8}})\s+Announce Type:\s+[a-z-]+\s+Abstract:\s*(.*)$",
    re.DOTALL,
)


def parse_arxiv_rss(raw: bytes) -> list[dict]:
    """Parse the official RSS 2.0 feed and require corroborated, explicit editions."""
    root = _parse_arxiv_xml(raw)
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ValueError("Unexpected arXiv RSS document")
    entries = channel.findall("item")
    results = []
    for item in entries:
        link = ARXIV_ABS_URL_RE.fullmatch(item.findtext("link", "").strip())
        description = _RSS_ABSTRACT.fullmatch(item.findtext("description", "").strip())
        if not link or not description or link[1] != description[1] or link[2] not in (None, description[2]):
            continue
        title = _normalize_inline(item.findtext("title", ""))
        summary = _normalize_inline(description[3])
        if not title or not summary:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
            if published.tzinfo is None:
                continue
            published_at = published.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError):
            continue
        author = _normalize_inline(item.findtext("{http://purl.org/dc/elements/1.1/}creator", ""))
        results.append(_arxiv_abstract(link[1], description[2], title, summary, author, published_at))
        if len(results) == ARXIV_FEED_LIMIT:
            break
    if entries and not results:
        raise ValueError("arXiv RSS contains no verified fixed editions")
    return results


class _StructuredHTMLParser(HTMLParser):
    IGNORED_TAGS = frozenset(
        {
            "script",
            "style",
            "nav",
            "header",
            "footer",
            "aside",
            "noscript",
            "svg",
            "annotation",
            "annotation-xml",
            "form",
            "button",
            "iframe",
            "template",
        }
    )
    VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )
    BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignore_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.h1_title: str = ""
        self.has_main_container = False
        self.has_ltx_error = False
        self.ancestors: list[tuple[str, str | None]] = []
        self.active_block_tag: str | None = None
        self.active_block_publisher_id: str | None = None
        self.active_block_parts: list[str] = []
        self.raw_blocks: list[tuple[str, str | None, str]] = []

    def _current_nearest_publisher_id(self) -> str | None:
        for _tag, el_id in reversed(self.ancestors):
            if el_id:
                return el_id
        return None

    def _flush_active_block(self):
        if self.active_block_tag is None:
            return
        text = re.sub(r"\s+", " ", "".join(self.active_block_parts)).strip()
        tag = self.active_block_tag
        publisher_id = self.active_block_publisher_id
        self.active_block_tag = None
        self.active_block_publisher_id = None
        self.active_block_parts = []
        if not text:
            return
        if tag == "h1" and not self.h1_title:
            self.h1_title = text
            return
        self.raw_blocks.append((tag, publisher_id, text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag_l = tag.lower()
        if tag_l in self.IGNORED_TAGS:
            self.ignore_depth += 1
            return
        if self.ignore_depth > 0:
            return

        if tag_l == "title":
            self.in_title = True
            return

        attr_map = dict(attrs)
        raw_id = (attr_map.get("id") or "").strip()
        # Only preserve genuine publisher HTML IDs; never slugify invalid IDs as publisher IDs
        publisher_id = raw_id if (raw_id and SAFE_ANCHOR_RE.fullmatch(raw_id)) else None

        classes = set((attr_map.get("class") or "").split())
        if tag_l in ("article", "main") or "ltx_document" in classes:
            self.has_main_container = True
        if "ltx_ERROR" in classes or "ltx_incomplete" in classes:
            self.has_ltx_error = True

        if tag_l not in self.VOID_TAGS:
            self.ancestors.append((tag_l, publisher_id))

        if tag_l in self.BLOCK_TAGS:
            if self.active_block_tag is not None:
                self._flush_active_block()
            self.active_block_tag = tag_l
            self.active_block_publisher_id = (
                publisher_id or self._current_nearest_publisher_id()
            )
            self.active_block_parts = []

    def handle_endtag(self, tag: str):
        tag_l = tag.lower()
        if tag_l in self.IGNORED_TAGS:
            if self.ignore_depth > 0:
                self.ignore_depth -= 1
            return
        if self.ignore_depth > 0:
            return

        if tag_l == "title":
            self.in_title = False
            return

        if tag_l == self.active_block_tag:
            self._flush_active_block()

        for idx in range(len(self.ancestors) - 1, -1, -1):
            if self.ancestors[idx][0] == tag_l:
                del self.ancestors[idx:]
                break

    def handle_data(self, data: str):
        if self.ignore_depth > 0:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.active_block_tag is not None:
            self.active_block_parts.append(data)


def _extract_html_blocks(raw: bytes, source_url: str) -> tuple[str, str, list[dict]]:
    try:
        html_text = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError("HTML document is not valid UTF-8") from err

    parser = _StructuredHTMLParser()
    parser.feed(html_text)
    parser.close()
    parser._flush_active_block()

    doc_title = parser.h1_title or re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    if not doc_title:
        raise ValueError("HTML document is missing a title")

    if not parser.raw_blocks:
        raise ValueError("HTML document contains no readable content blocks")

    blocks: list[dict] = []
    used_anchors: set[str] = set()
    anchor_counts: dict[str, int] = {}

    for idx, (_tag, publisher_id, text) in enumerate(parser.raw_blocks, start=1):
        base_anchor = publisher_id if publisher_id else f"p{idx}"
        if base_anchor not in used_anchors:
            anchor = base_anchor
        else:
            count = anchor_counts.get(base_anchor, 0) + 1
            anchor_counts[base_anchor] = count
            anchor = f"{base_anchor}.p{count}"
            while anchor in used_anchors:
                count += 1
                anchor_counts[base_anchor] = count
                anchor = f"{base_anchor}.p{count}"
        used_anchors.add(anchor)
        block_source_url = (
            f"{source_url}#{publisher_id}" if publisher_id else source_url
        )
        blocks.append(
            {
                "anchor": anchor,
                "text": text,
                "source_url": block_source_url,
            }
        )

    if parser.has_main_container and not parser.has_ltx_error:
        scope = "full"
    else:
        scope = "partial"

    return doc_title, scope, blocks


def parse_arxiv_html(raw: bytes, source_url: str) -> dict:
    """Parse versioned arXiv LaTeXML HTML (`https://arxiv.org/html/<id>v<N>`)."""
    parsed = _check_basic_https_url(source_url)
    match = ARXIV_HTML_PATH_RE.fullmatch(parsed.path)
    if parsed.netloc != "arxiv.org" or not match:
        raise ValueError(f"Invalid arXiv HTML URL: {source_url}")

    base_id = match.group(1)
    version_num = match.group(2)
    label = f"v{version_num}"
    title, scope, blocks = _extract_html_blocks(raw, source_url)
    fulltext_status = "ready" if scope == "full" else "partial"
    content_hash = compute_content_hash(title, scope, label, blocks)
    return {
        "key": f"arxiv:{base_id}",
        "arxiv_id": base_id,
        "version_num": version_num,
        "source_name": "arxiv",
        "title": title,
        "label": label,
        "scope": scope,
        "fulltext_status": fulltext_status,
        "source_url": source_url,
        "author": "",
        "published_at": "",
        "blocks": blocks,
        "content_hash": content_hash,
    }


def parse_python_whatsnew_html(raw: bytes, source_url: str = PYTHON_314_DOC_URL) -> dict:
    """Parse Python 3.14 What's New documentation HTML."""
    if source_url != PYTHON_314_DOC_URL:
        raise ValueError("Unsupported Python documentation URL")
    title, scope, blocks = _extract_html_blocks(raw, source_url)
    label = "3.14"
    fulltext_status = "ready" if scope == "full" else "partial"
    content_hash = compute_content_hash(title, scope, label, blocks)
    return {
        "key": "python:whatsnew-3.14",
        "source_name": "python",
        "title": title,
        "label": label,
        "scope": scope,
        "fulltext_status": fulltext_status,
        "source_url": source_url,
        "author": "Python Documentation Authors",
        "published_at": "",
        "blocks": blocks,
        "content_hash": content_hash,
    }
