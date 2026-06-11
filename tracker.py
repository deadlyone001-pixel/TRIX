"""
tracker.py — Persistent tracking of manga series and their last-seen chapters.
"""

import json
import logging
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

from scraper import ChapterInfo

logger = logging.getLogger(__name__)

import sys

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

DATA_FILE = BASE_DIR / "data" / "tracked.json"

# Chapters updated within this window are shown as "NEW" in the UI
NEW_CHAPTER_WINDOW_HOURS = 8
@dataclass
class TrackedManga:
    url: str
    display_name: str           # user-chosen or auto-detected from scrape
    last_chapter_num: float     # -1 means never seen
    last_chapter_title: str
    last_chapter_url: str
    added_at: float = field(default_factory=time.time)
    last_checked: float = field(default_factory=lambda: 0.0)
    last_chapter_found_at: float = field(default_factory=lambda: 0.0)
    error_count: int = 0
    cover_url: str = ""
    title_resolved: bool = False
    has_custom_title: bool = False
    history: list = field(default_factory=list)
    user_status: str = "unknown"   # "unknown" | "active" | "hiatus" — set manually by user
    channel_id: int = 0
    ping_id: str = ""

    # ── derived helpers ───────────────────────────────────────────────────────

    @property
    def is_new(self) -> bool:
        """True if a new chapter was found within the last NEW_CHAPTER_WINDOW_HOURS."""
        if self.last_chapter_found_at <= 0:
            return False
        age_hours = (time.time() - self.last_chapter_found_at) / 3600
        return age_hours <= NEW_CHAPTER_WINDOW_HOURS

    @property
    def chapter_age_str(self) -> str:
        """Human-readable age of the latest chapter detection."""
        if self.last_chapter_found_at <= 0:
            return ""
        elapsed = time.time() - self.last_chapter_found_at
        if elapsed < 60:
            return "just now"
        if elapsed < 3600:
            return f"{int(elapsed // 60)}m ago"
        if elapsed < 86400:
            return f"{int(elapsed // 3600)}h ago"
        return f"{int(elapsed // 86400)}d ago"

    # ── serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TrackedManga":
        return cls(
            url=d["url"],
            display_name=d.get("display_name", "Unknown"),
            last_chapter_num=d.get("last_chapter_num", -1),
            last_chapter_title=d.get("last_chapter_title", ""),
            last_chapter_url=d.get("last_chapter_url", ""),
            added_at=d.get("added_at", time.time()),
            last_checked=d.get("last_checked", 0.0),
            last_chapter_found_at=d.get("last_chapter_found_at", 0.0),
            error_count=d.get("error_count", 0),
            cover_url=d.get("cover_url", ""),
            title_resolved=d.get("title_resolved", False),
            has_custom_title=d.get("has_custom_title", False),
            history=d.get("history", []),
            user_status=d.get("user_status", "unknown"),
            channel_id=d.get("channel_id", 0),
            ping_id=d.get("ping_id", ""),
        )


class MangaTracker:
    """Manages the list of tracked manga and persists state to disk."""

    def __init__(self, data_file: Path = DATA_FILE):
        self.data_file = data_file
        self._manga: dict[str, TrackedManga] = {}  # keyed by URL
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self):
        if self.data_file.exists():
            try:
                raw = json.loads(self.data_file.read_text(encoding="utf-8"))
                self._manga = {
                    item["url"]: TrackedManga.from_dict(item)
                    for item in raw
                }
                logger.info(f"Loaded {len(self._manga)} tracked titles from {self.data_file}")
            except Exception as e:
                logger.error(f"Failed to load tracking data: {e}")
                self._manga = {}

    def save(self):
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        data = [m.to_dict() for m in self._manga.values()]
        self.data_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def add(self, url: str, display_name: str = "", channel_id: int = 0, ping_id: str = "") -> TrackedManga:
        """Add a new manga to track. Returns the TrackedManga object."""
        if url in self._manga:
            self._manga[url].channel_id = channel_id
            self._manga[url].ping_id = ping_id
            self.save()
            return self._manga[url]
        entry = TrackedManga(
            url=url,
            display_name=display_name or url,
            last_chapter_num=-1,
            last_chapter_title="",
            last_chapter_url="",
            title_resolved=bool(display_name),  # user set a name → consider resolved
            has_custom_title=bool(display_name),
            channel_id=channel_id,
            ping_id=ping_id,
        )
        self._manga[url] = entry
        self.save()
        return entry

    def remove(self, url: str):
        if url in self._manga:
            del self._manga[url]
            self.save()

    def get_all(self) -> list[TrackedManga]:
        return list(self._manga.values())

    def get(self, url: str) -> Optional[TrackedManga]:
        return self._manga.get(url)

    def set_user_status(self, url: str, status: str):
        """Set the manual status ('unknown', 'active', 'hiatus') and save."""
        entry = self._manga.get(url)
        if entry:
            entry.user_status = status
            self.save()

    # ── update helpers ────────────────────────────────────────────────────────

    def update_chapter(
        self,
        url: str,
        chapter: ChapterInfo,
        manga_title: str,
        cover_url: str,
    ) -> bool:
        """
        Update the stored chapter for a manga.
        Returns True if this is a NEW chapter (number is strictly higher than stored).
        """
        entry = self._manga.get(url)
        if entry is None:
            return False

        is_new = chapter.number > entry.last_chapter_num

        if entry.last_chapter_num > 10000 and chapter.number < 10000:
            is_new = True

        if is_new:
            entry.last_chapter_num = chapter.number
            entry.last_chapter_title = chapter.title
            entry.last_chapter_url = chapter.url
            entry.last_chapter_found_at = time.time()
            entry.history.insert(0, {
                "num": chapter.number,
                "title": chapter.title,
                "url": chapter.url,
                "time": time.time(),
            })
            entry.history = entry.history[:5]

        # Always update metadata
        if manga_title and manga_title not in ("Unknown", ""):
            if not entry.has_custom_title:
                entry.display_name = manga_title
            entry.title_resolved = True
        if cover_url:
            entry.cover_url = cover_url
        entry.last_checked = time.time()
        entry.error_count = 0
        self.save()
        return is_new

    def record_error(self, url: str):
        entry = self._manga.get(url)
        if entry:
            entry.error_count += 1
            entry.last_checked = time.time()
            self.save()
