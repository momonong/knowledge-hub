"""SQLite storage, immutable versioning, note/relation binding, and search."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .sources import now_iso
from . import qa, lessons, connections, study, reading, daily, local_study, followup

VALID_STATUSES = frozenset({"unread", "later", "read"})
VALID_SCOPES = frozenset({"full", "abstract", "partial", "unavailable"})
VALID_SOURCE_STATES = frozenset({"unattempted", "updated", "unchanged", "failed"})
SCOPE_PRIORITY = {"full": 4, "partial": 3, "abstract": 2, "unavailable": 1}
_ARXIV_VERSION_RE = re.compile(r"^v([1-9]\d*)$")


def arxiv_version_number(label: str) -> int | None:
    match = _ARXIV_VERSION_RE.fullmatch(str(label or "").strip())
    return int(match.group(1)) if match else None


class KnowledgeStore:
    """Bounded SQLite storage for Knowledge Hub phase 1."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if self.db_path.parent:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    name TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    last_fetched_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    source_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unread',
                    fulltext_status TEXT NOT NULL DEFAULT 'not_requested',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS item_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    blocks_json TEXT NOT NULL,
                    UNIQUE(item_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version_id INTEGER NOT NULL REFERENCES item_versions(id) ON DELETE CASCADE,
                    anchor TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version_id INTEGER NOT NULL REFERENCES item_versions(id) ON DELETE CASCADE,
                    anchor TEXT NOT NULL,
                    concept TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL
                );
                """
            )
            if 'last_success_at' not in {r[1] for r in conn.execute('PRAGMA table_info(sources)')}:
                conn.execute('ALTER TABLE sources ADD COLUMN last_success_at TEXT')
                # Only a known successful final attempt can be migrated. A
                # historical failure does not reveal the preceding success.
                conn.execute("UPDATE sources SET last_success_at=last_fetched_at WHERE state IN ('updated','unchanged')")
            qa.initialize(conn)
            lessons.initialize(conn)
            connections.initialize(conn)
            study.initialize(conn)
            reading.initialize(conn)
            local_study.initialize(conn)
            daily.initialize(conn)
            followup.initialize(conn)
            from . import captures
            captures.initialize(conn)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'csrf_token'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('csrf_token', ?)",
                    (secrets.token_urlsafe(24),),
                )
            row_int = conn.execute(
                "SELECT value FROM meta WHERE key = 'interests_json'"
            ).fetchone()
            if row_int is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('interests_json', '[]')"
                )
            for src_name in ("pytorch", "arxiv"):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sources (name, state, last_fetched_at, last_error)
                    VALUES (?, 'unattempted', NULL, NULL)
                    """,
                    (src_name,),
                )

    def get_csrf_token(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'csrf_token'"
            ).fetchone()
            if row is None:
                token = secrets.token_urlsafe(24)
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('csrf_token', ?)",
                    (token,),
                )
                return token
            return str(row["value"])

    def get_interests(self) -> list[str]:
        with self._connect() as conn:
            return self._read_interests(conn)

    @staticmethod
    def _read_interests(conn) -> list[str]:
        row = conn.execute("SELECT value FROM meta WHERE key = 'interests_json'").fetchone()
        if row is None:
            return []
        try:
            data = json.loads(row["value"])
            if isinstance(data, list):
                return [str(x) for x in data if isinstance(x, str) and x.strip()]
        except json.JSONDecodeError:
            pass
        return []

    def set_interests(self, raw_terms: str) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for part in raw_terms.replace("，", ",").replace(";", ",").split(","):
            cleaned = part.strip()
            if not cleaned:
                continue
            if len(cleaned) > 100:
                raise ValueError("Interest term is too long")
            key_cf = cleaned.casefold()
            if key_cf not in seen:
                seen.add(key_cf)
                terms.append(cleaned)
        if len(terms) > 50:
            raise ValueError("Too many interest terms")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('interests_json', ?)",
                (json.dumps(terms, ensure_ascii=False),),
            )
        return terms

    def update_source_state(
        self, name: str, state: str, error: str | None = None
    ) -> None:
        if state not in VALID_SOURCE_STATES:
            raise ValueError(f"Invalid source state: {state}")
        ts = now_iso()
        with self._connect() as conn:
            if state == "failed":
                conn.execute("INSERT INTO source_failures(name,attempted_at,error) VALUES(?,?,?)",
                             (name, ts, error or "來源讀取失敗"))
            conn.execute(
                """
                INSERT INTO sources (name, state, last_fetched_at, last_error, last_success_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    state = excluded.state,
                    last_fetched_at = excluded.last_fetched_at,
                    last_error = excluded.last_error,
                    last_success_at = CASE WHEN excluded.state IN ('updated','unchanged')
                                      THEN excluded.last_success_at ELSE sources.last_success_at END
                """,
                (name, state, ts, error, ts if state in ('updated','unchanged') else None),
            )

    def get_sources(self) -> dict[str, dict]:
        with self._connect() as conn:
            return self._read_sources(conn)

    @staticmethod
    def _read_sources(conn) -> dict[str, dict]:
        rows = conn.execute(
            "SELECT name, state, last_fetched_at, last_error, last_success_at FROM sources ORDER BY name"
        )
        return {
            str(row["name"]): {
                "state": str(row["state"]),
                "last_fetched_at": row["last_fetched_at"],
                "last_success_at": row["last_success_at"],
                "last_error": row["last_error"],
            } for row in rows
        }

    def upsert_parsed_entry(self, entry: dict) -> tuple[int, str, bool]:
        """Insert item and/or immutable version if content_hash is new.

        Returns `(item_id, version_id_str, changed)`.
        """
        key = str(entry["key"]).strip()
        source_name = str(entry["source_name"]).strip()
        title = str(entry["title"]).strip()
        label = str(entry["label"]).strip()
        scope = str(entry["scope"]).strip()
        fulltext_status = str(entry.get("fulltext_status") or "not_requested")
        source_url = str(entry["source_url"]).strip()
        author = str(entry.get("author") or "").strip()
        published_at = str(entry.get("published_at") or "").strip()
        blocks = list(entry["blocks"])
        content_hash = str(entry["content_hash"]).strip()

        if not key or not title or scope not in VALID_SCOPES or not blocks:
            raise ValueError("Invalid entry payload for store")

        now = now_iso()
        blocks_json = json.dumps(blocks, ensure_ascii=False)

        with self._connect() as conn:
            item_row = conn.execute(
                "SELECT id, title, fulltext_status FROM items WHERE key = ?",
                (key,),
            ).fetchone()

            if item_row is None:
                cur = conn.execute(
                    """
                    INSERT INTO items (key, source_name, title, status, fulltext_status, created_at, updated_at)
                    VALUES (?, ?, ?, 'unread', ?, ?, ?)
                    """,
                    (key, source_name, title, fulltext_status, now, now),
                )
                item_id = int(cur.lastrowid)
                v_cur = conn.execute(
                    """
                    INSERT INTO item_versions (
                        item_id, label, scope, source_url, author, published_at, fetched_at, content_hash, blocks_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        label,
                        scope,
                        source_url,
                        author,
                        published_at,
                        now,
                        content_hash,
                        blocks_json,
                    ),
                )
                conn.execute('INSERT INTO version_titles(version_id,title) VALUES (?,?)',
                             (v_cur.lastrowid, title))
                return item_id, str(v_cur.lastrowid), True

            item_id = int(item_row["id"])
            existing_v = conn.execute(
                "SELECT id FROM item_versions WHERE item_id = ? AND content_hash = ?",
                (item_id, content_hash),
            ).fetchone()
            if existing_v is not None:
                if scope == "full" and item_row["fulltext_status"] != "ready":
                    conn.execute(
                        "UPDATE items SET fulltext_status = 'ready', updated_at = ? WHERE id = ?",
                        (now, item_id),
                    )
                elif scope == "partial" and item_row["fulltext_status"] not in ("ready", "partial"):
                    conn.execute(
                        "UPDATE items SET fulltext_status = 'partial', updated_at = ? WHERE id = ?",
                        (now, item_id),
                    )
                return item_id, str(existing_v["id"]), False

            existing_labels = [
                str(r["label"])
                for r in conn.execute(
                    "SELECT label FROM item_versions WHERE item_id = ?",
                    (item_id,),
                ).fetchall()
            ]

            if not author or not published_at:
                prev_meta = conn.execute(
                    """
                    SELECT author, published_at FROM item_versions
                    WHERE item_id = ?
                    ORDER BY CASE WHEN label = ? THEN 0 ELSE 1 END, id DESC
                    LIMIT 1
                    """,
                    (item_id, label),
                ).fetchone()
                if prev_meta is not None:
                    if not author:
                        author = str(prev_meta["author"] or "")
                    if not published_at:
                        published_at = str(prev_meta["published_at"] or "")

            v_cur = conn.execute(
                """
                INSERT INTO item_versions (
                    item_id, label, scope, source_url, author, published_at, fetched_at, content_hash, blocks_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    label,
                    scope,
                    source_url,
                    author,
                    published_at,
                    now,
                    content_hash,
                    blocks_json,
                ),
            )
            new_ft_status = str(item_row["fulltext_status"])
            if scope == "full":
                new_ft_status = "ready"
            elif scope == "partial":
                new_ft_status = "partial"

            new_ver_num = arxiv_version_number(label)
            existing_ver_nums = [
                n for lbl in existing_labels if (n := arxiv_version_number(lbl)) is not None
            ]
            if new_ver_num is not None and existing_ver_nums and new_ver_num < max(existing_ver_nums):
                updated_title = str(item_row["title"])
            else:
                updated_title = title

            conn.execute(
                """
                UPDATE items
                SET title = ?, fulltext_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (updated_title, new_ft_status, now, item_id),
            )
            conn.execute('INSERT INTO version_titles(version_id,title) VALUES (?,?)',
                         (v_cur.lastrowid, title))
            return item_id, str(v_cur.lastrowid), True

    def set_item_fulltext_status(self, item_id: int, fulltext_status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE items SET fulltext_status = ?, updated_at = ? WHERE id = ?",
                (fulltext_status, now_iso(), item_id),
            )

    def set_item_status(self, item_id: int, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid item status: {status}")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), item_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Item {item_id} not found")

    def add_note(
        self, item_id: int, version_id: str | int, anchor: str, text: str
    ) -> dict:
        cleaned_text = text.strip()
        cleaned_anchor = anchor.strip()
        if not cleaned_text or len(cleaned_text) > 10000:
            raise ValueError("Note text must be 1..10000 characters")
        if not cleaned_anchor or len(cleaned_anchor) > 200:
            raise ValueError("Invalid block anchor")

        with self._connect() as conn:
            v_row = self._resolve_version_row(conn, item_id, version_id)
            blocks = json.loads(v_row["blocks_json"])
            valid_anchors = {b["anchor"] for b in blocks}
            if cleaned_anchor not in valid_anchors:
                raise ValueError("Anchor does not exist in the specified version")

            now = now_iso()
            cur = conn.execute(
                """
                INSERT INTO notes (item_id, version_id, anchor, text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item_id, int(v_row["id"]), cleaned_anchor, cleaned_text, now),
            )
            return {
                "id": int(cur.lastrowid),
                "version_id": str(v_row["id"]),
                "anchor": cleaned_anchor,
                "text": cleaned_text,
                "created_at": now,
            }

    def add_relation(
        self,
        item_id: int,
        version_id: str | int,
        anchor: str,
        concept: str,
        kind: str,
    ) -> dict:
        cleaned_concept = concept.strip()
        cleaned_kind = kind.strip()
        cleaned_anchor = anchor.strip()
        if not cleaned_concept or len(cleaned_concept) > 200:
            raise ValueError("Concept must be 1..200 characters")
        if not cleaned_kind or len(cleaned_kind) > 100:
            raise ValueError("Relation kind must be 1..100 characters")
        if not cleaned_anchor or len(cleaned_anchor) > 200:
            raise ValueError("Invalid block anchor")

        with self._connect() as conn:
            v_row = self._resolve_version_row(conn, item_id, version_id)
            blocks = json.loads(v_row["blocks_json"])
            valid_anchors = {b["anchor"] for b in blocks}
            if cleaned_anchor not in valid_anchors:
                raise ValueError("Anchor does not exist in the specified version")

            now = now_iso()
            cur = conn.execute(
                """
                INSERT INTO relations (item_id, version_id, anchor, concept, kind, origin, created_at)
                VALUES (?, ?, ?, ?, ?, 'manual', ?)
                """,
                (
                    item_id,
                    int(v_row["id"]),
                    cleaned_anchor,
                    cleaned_concept,
                    cleaned_kind,
                    now,
                ),
            )
            return {
                "id": int(cur.lastrowid),
                "version_id": str(v_row["id"]),
                "anchor": cleaned_anchor,
                "concept": cleaned_concept,
                "kind": cleaned_kind,
                "origin": "manual",
                "created_at": now,
            }

    def _resolve_version_row(
        self, conn: sqlite3.Connection, item_id: int, version_ref: str | int
    ) -> sqlite3.Row:
        ref_str = str(version_ref).strip()
        if not ref_str:
            raise ValueError("Missing version_id")
        if ref_str.isdigit():
            row = conn.execute(
                "SELECT * FROM item_versions WHERE id = ? AND item_id = ?",
                (int(ref_str), item_id),
            ).fetchone()
            if row is not None:
                return row
        row = conn.execute(
            """
            SELECT * FROM item_versions
            WHERE item_id = ? AND label = ?
            ORDER BY CASE scope
                WHEN 'full' THEN 4
                WHEN 'partial' THEN 3
                WHEN 'abstract' THEN 2
                ELSE 1
            END DESC, id DESC
            LIMIT 1
            """,
            (item_id, ref_str),
        ).fetchone()
        if row is not None:
            return row
        raise ValueError("Specified version does not belong to this item")

    @staticmethod
    def select_default_version(versions: list[dict]) -> dict:
        if not versions:
            raise ValueError("Item has no versions")
        arxiv_nums = [
            num for v in versions if (num := arxiv_version_number(v["label"])) is not None
        ]
        if arxiv_nums:
            target_label = f"v{max(arxiv_nums)}"
        else:
            target_label = versions[-1]["label"]

        same_label = [v for v in versions if v["label"] == target_label]
        same_label.sort(
            key=lambda v: (SCOPE_PRIORITY.get(v["scope"], 0), int(v["id"])),
            reverse=True,
        )
        return same_label[0]

    @classmethod
    def select_version(cls, versions: list[dict], requested: str = "") -> dict | None:
        if not versions:
            return None
        req = str(requested or "").strip()
        if not req:
            return cls.select_default_version(versions)
        exact_id = next((v for v in versions if v["id"] == req), None)
        if exact_id is not None:
            return exact_id
        same_label = [v for v in versions if v["label"] == req]
        if same_label:
            same_label.sort(
                key=lambda v: (SCOPE_PRIORITY.get(v["scope"], 0), int(v["id"])),
                reverse=True,
            )
            return same_label[0]
        return None

    def get_item(self, item_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            return self._Hydrate_item(conn, row)

    def get_all_items(self, *, focal_id: int | None = None) -> list[dict]:
        with self._connect() as conn:
            return self._read_items(conn, focal_id=focal_id)

    def _read_items(self, conn, *, focal_id=None) -> list[dict]:
        if focal_id is None:
            rows = conn.execute("SELECT * FROM items ORDER BY updated_at DESC, id DESC").fetchall()
        else:
            # Feed discoveries become background only after the reader has
            # engaged with them. Keep the article being opened regardless.
            rows = conn.execute('''SELECT * FROM items i WHERE i.source_name<>'huggingface'
                OR i.id=? OR i.status='read' OR EXISTS (SELECT 1 FROM reading_sessions r
                    WHERE r.item_id=i.id AND r.revision>0)
                ORDER BY i.updated_at DESC,i.id DESC''',(focal_id,)).fetchall()
        return [self._Hydrate_item(conn, r) for r in rows]

    def _Hydrate_item(self, conn: sqlite3.Connection, item_row: sqlite3.Row) -> dict:
        item_id = int(item_row["id"])
        v_rows = conn.execute(
            "SELECT * FROM item_versions WHERE item_id = ? ORDER BY id ASC",
            (item_id,),
        ).fetchall()
        versions: list[dict] = []
        for vr in v_rows:
            versions.append(
                {
                    "id": str(vr["id"]),
                    "label": str(vr["label"]),
                    "scope": str(vr["scope"]),
                    "source_url": str(vr["source_url"]),
                    "author": str(vr["author"]),
                    "published_at": str(vr["published_at"]),
                    "fetched_at": str(vr["fetched_at"]),
                    "content_hash": str(vr["content_hash"]),
                    "blocks": json.loads(vr["blocks_json"]),
                }
            )

        n_rows = conn.execute(
            """SELECT n.*,c.source_url AS capture_url FROM notes n
               LEFT JOIN capture_records c ON c.note_id=n.id
               WHERE n.item_id=? ORDER BY n.id""",
            (item_id,),
        ).fetchall()
        notes = [
            {
                "id": int(nr["id"]),
                "version_id": str(nr["version_id"]),
                "anchor": str(nr["anchor"]),
                "text": str(nr["text"]),
                "created_at": str(nr["created_at"]),
                **({'source_url': nr['capture_url']} if nr['capture_url'] else {}),
            }
            for nr in n_rows
        ]

        r_rows = conn.execute(
            "SELECT * FROM relations WHERE item_id = ? ORDER BY id ASC",
            (item_id,),
        ).fetchall()
        relations = [
            {
                "id": int(rr["id"]),
                "version_id": str(rr["version_id"]),
                "anchor": str(rr["anchor"]),
                "concept": str(rr["concept"]),
                "kind": str(rr["kind"]),
                "origin": str(rr["origin"]),
                "created_at": str(rr["created_at"]),
            }
            for rr in r_rows
        ]

        return {
            "id": item_id,
            "key": str(item_row["key"]),
            "source_name": str(item_row["source_name"]),
            "title": str(item_row["title"]),
            "status": str(item_row["status"]),
            "fulltext_status": str(item_row["fulltext_status"]),
            "created_at": str(item_row["created_at"]),
            "updated_at": str(item_row["updated_at"]),
            "versions": versions,
            "notes": notes,
            "relations": relations,
            "questions": qa.records(conn, item_id),
            "connections": connections.records(conn, item_id),
        }

    def get_todays_picks(self, limit: int = 8) -> list[dict]:
        """Rank items by explicit interest match + recency, returning up to `limit` picks."""
        interests = self.get_interests()
        items = self.get_all_items()
        picks: list[dict] = []

        for item in items:
            if item["source_name"] == "library":
                continue
            if not item["versions"]:
                continue
            active_v = self.select_default_version(item["versions"])
            blocks = active_v["blocks"]
            matched_terms: list[str] = []
            best_block = blocks[0] if blocks else {"anchor": "top", "text": "", "source_url": active_v["source_url"]}
            found_interest_block = False

            title_cf = item["title"].casefold()
            key_cf = item["key"].casefold()
            concepts_cf = " ".join(r["concept"] for r in item["relations"]).casefold()

            for term in interests:
                t_cf = term.casefold()
                term_hit = False
                if t_cf in title_cf or t_cf in key_cf or t_cf in concepts_cf:
                    term_hit = True
                for blk in blocks:
                    if t_cf in blk["text"].casefold():
                        term_hit = True
                        if not found_interest_block:
                            best_block = blk
                            found_interest_block = True
                        break
                if term_hit:
                    matched_terms.append(term)

            if not found_interest_block and blocks:
                best_block = next(
                    (b for b in blocks if len(b["text"]) > 20),
                    blocks[0],
                )

            if matched_terms:
                reason = f"符合興趣關鍵字：{', '.join(matched_terms)}"
            elif interests:
                reason = "近期更新內容（依發布與同步時間排序）"
            else:
                reason = "最新同步文章（可於下方設定興趣關鍵字篩選）"

            excerpt_text = best_block["text"]
            if len(excerpt_text) > 180:
                excerpt_text = excerpt_text[:177] + "..."

            sort_time = active_v["published_at"] or active_v["fetched_at"] or item["updated_at"]
            picks.append(
                {
                    "item": item,
                    "version": active_v,
                    "matched_terms": matched_terms,
                    "match_count": len(matched_terms),
                    "reason": reason,
                    "excerpt": excerpt_text,
                    "anchor": best_block["anchor"],
                    "block_source_url": best_block.get("source_url") or active_v["source_url"],
                    "sort_time": sort_time,
                }
            )

        picks.sort(
            key=lambda p: (p["match_count"], p["sort_time"], p["item"]["id"]),
            reverse=True,
        )
        return picks[:limit]

    def search(self, query: str) -> list[dict]:
        """Substring search across titles, version blocks, concepts, and notes."""
        q = query.strip()
        if not q:
            return []
        q_cf = q.casefold()
        items = self.get_all_items()
        results: list[dict] = []

        for item in items:
            hits: list[dict] = []
            version_map = {v["id"]: v for v in item["versions"]}
            default_v = self.select_default_version(item["versions"]) if item["versions"] else None

            if q_cf in item["title"].casefold() and default_v is not None:
                first_anchor = default_v["blocks"][0]["anchor"] if default_v["blocks"] else ""
                hits.append(
                    {
                        "kind": "標題",
                        "version_id": default_v["id"],
                        "version_label": default_v["label"],
                        "anchor": first_anchor,
                        "snippet": item["title"],
                    }
                )

            for note in item["notes"]:
                if q_cf in note["text"].casefold():
                    v_obj = version_map.get(note["version_id"])
                    v_label = v_obj["label"] if v_obj else note["version_id"]
                    hits.append(
                        {
                            "kind": "筆記",
                            "version_id": note["version_id"],
                            "version_label": v_label,
                            "anchor": note["anchor"],
                            "snippet": note["text"],
                        }
                    )

            for rel in item["relations"]:
                combined = f"{rel['concept']} ({rel['kind']})"
                if q_cf in rel["concept"].casefold() or q_cf in rel["kind"].casefold():
                    v_obj = version_map.get(rel["version_id"])
                    v_label = v_obj["label"] if v_obj else rel["version_id"]
                    hits.append(
                        {
                            "kind": "概念關係",
                            "version_id": rel["version_id"],
                            "version_label": v_label,
                            "anchor": rel["anchor"],
                            "snippet": combined,
                        }
                    )

            for ver in item["versions"]:
                for blk in ver["blocks"]:
                    if q_cf in blk["text"].casefold():
                        hits.append(
                            {
                                "kind": "段落內容",
                                "version_id": ver["id"],
                                "version_label": ver["label"],
                                "anchor": blk["anchor"],
                                "snippet": blk["text"],
                            }
                        )

            for question in item['questions']:
                texts = [('問題', question['question'])]
                answer = question['answer']
                if question['status'] == 'completed' and answer is not None:
                    texts.extend(('回答', c['text']) for c in answer['claims'])
                    texts.append(('回答限制', answer['limitations']))
                for kind, text in texts:
                    if q_cf in text.casefold():
                        ver = version_map[question['version_id']]
                        hits.append({'kind': kind, 'version_id': ver['id'],
                                     'version_label': ver['label'],
                                     'anchor': f"question-{question['id']}", 'snippet': text})

            for saved in item['connections']:
                if saved['state'] != 'completed' or not saved['answer']:
                    continue
                texts = [c['text'] for c in saved['answer']['claims']] + [saved['answer']['limitations']]
                for text in texts:
                    if q_cf in text.casefold():
                        ver = version_map[saved['version_id']]
                        hits.append({'kind': '串連解說', 'version_id': ver['id'],
                            'version_label': ver['label'], 'anchor': f"connection-{saved['id']}", 'snippet': text})

            with self._connect() as conn:
                for saved in conn.execute('SELECT * FROM study_progress WHERE item_id=?', (item['id'],)):
                    text = saved['understanding'] + '\n' + saved['questions']
                    if q_cf in text.casefold():
                        ver = version_map[str(saved['version_id'])]
                        hits.append({'kind': '我的學習紀錄', 'version_id': ver['id'],
                            'version_label': ver['label'], 'anchor': f"progress-{saved['id']}", 'snippet': text})
                for raw in conn.execute("SELECT * FROM study_records WHERE item_id=? AND state='completed'", (item['id'],)):
                    saved = study.record(raw)
                    text = saved['prompt'] + '\n' + '\n'.join(s['text'] for s in saved['answer']['sections'])
                    if q_cf in text.casefold():
                        ver = version_map[saved['version_id']]
                        hits.append({'kind': '教學與追問', 'version_id': ver['id'],
                            'version_label': ver['label'], 'anchor': f"study-{saved['id']}", 'snippet': text})

            if hits:
                results.append({"item": item, "hits": hits})

        return results

    def create_lesson(self, item_id, version_id, title, learning_goal, blueprint_md,
                      origin, parent_id='') -> int:
        with self._connect() as conn:
            return lessons.create(conn, item_id, version_id, title, learning_goal,
                                  blueprint_md, origin, parent_id)

    def get_lesson(self, lesson_id) -> dict | None:
        lesson_id = lessons.identifier(lesson_id)
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM lessons WHERE id=?', (lesson_id,)).fetchone()
            return lessons.record(row) if row is not None else None

    def get_lessons(self, limit=None) -> list[dict]:
        with self._connect() as conn:
            return self._read_lessons(conn, limit)

    @staticmethod
    def _read_lessons(conn, limit=None) -> list[dict]:
        sql = 'SELECT * FROM lessons ORDER BY id DESC'
        rows = conn.execute(sql if limit is None else sql + ' LIMIT ?',
                            () if limit is None else (limit,))
        return [lessons.record(row) for row in rows]

    def update_lesson_result(self, lesson_id, notebook_url, review_notes) -> None:
        lesson_id = lessons.identifier(lesson_id)
        lessons.validate_result(notebook_url, review_notes)
        with self._connect() as conn:
            cur = conn.execute('UPDATE lessons SET notebook_url=?,review_notes=? WHERE id=?',
                               (notebook_url, review_notes, lesson_id))
            if not cur.rowcount:
                raise KeyError('找不到學習包。')

    def export_data(self) -> dict:
        """Build the portable JSON export from a single SQLite read snapshot."""
        with self._connect() as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            return self._export_snapshot(conn)

    def _export_snapshot(self, conn) -> dict:
        export_items: list[dict] = []
        for item in self._read_items(conn):
            export_versions = [
                {
                    "id": v["id"],
                    "label": v["label"],
                    "scope": v["scope"],
                    "source_url": v["source_url"],
                    "author": v["author"],
                    "published_at": v["published_at"],
                    "fetched_at": v["fetched_at"],
                    "blocks": v["blocks"],
                }
                for v in item["versions"]
            ]
            export_items.append(
                {
                    "id": item["id"],
                    "key": item["key"],
                    "title": item["title"],
                    "status": item["status"],
                    "fulltext_status": item["fulltext_status"],
                    "versions": export_versions,
                    "notes": item["notes"],
                    "relations": item["relations"],
                    "questions": item["questions"],
                    "connections": item["connections"],
                }
            )
        from . import captures
        return {
            "schema_version": 1,
            "library": self._export_library_metadata(conn),
            "interests": self._read_interests(conn),
            "sources": self._read_sources(conn),
            "items": export_items,
            "lessons": self._read_lessons(conn),
            "study": {
                'records': [study.record(r) for r in conn.execute('SELECT * FROM study_records ORDER BY id')],
                'progress': [dict(r) for r in conn.execute('SELECT * FROM study_progress ORDER BY id')],
            },
            "source_turns": [local_study.record(r) for r in conn.execute('SELECT * FROM source_turns ORDER BY id')],
            "followups": [followup._record(r) for r in conn.execute('SELECT * FROM followup_turns ORDER BY id')],
            "captures": [captures._row_record(r) for r in conn.execute('SELECT * FROM capture_records ORDER BY id')],
            "reading_sessions": [dict(row) for row in conn.execute(
                'SELECT * FROM reading_sessions ORDER BY item_id,version_id')],
        }

    @staticmethod
    def _export_library_metadata(conn) -> dict:
        from .library import KnowledgeLibrary

        # Library setup may create tables and opens its own connection. Read
        # the registered metadata directly within the export snapshot instead.
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('library_records','library_documents')")}
        categories = [('domains','domain'),('sources','source'),('concepts','concept'),
                      ('relations','relation'),('starter','starter')]
        result = {name: KnowledgeLibrary._records(conn, category) if 'library_records' in tables else []
                  for name,category in categories}
        result['schema_version'] = 1
        result['starter'].sort(key=lambda entry: entry['order'])
        result['metadata'] = {
            row['id']:json.loads(row['payload']) for row in conn.execute(
                "SELECT id,payload FROM library_records WHERE category='metadata' ORDER BY id")
        } if 'library_records' in tables else {}
        result['documents'] = []
        if {'library_records','library_documents'} <= tables:
            for row in conn.execute("""SELECT d.* FROM library_documents d JOIN library_records r
                                     ON r.category='source' AND r.id=d.source_id
                                     ORDER BY d.source_id,d.version_id,d.blob_sha256"""):
                document = dict(row)
                document.update(json.loads(document.pop('mapping_json')))
                result['documents'].append(document)
        return result
