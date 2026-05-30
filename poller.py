"""
poller.py — Background polling engine for near-real-time chapter detection.

Strategy:
  • Default poll cycle: every 5 minutes (catches updates within ~5 min of release)
  • All titles are checked in PARALLEL using a thread pool (max 5 concurrent).
  • A separate "fast-check" path is exposed so the GUI can trigger instant checks.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from scraper import scrape, get_session
from tracker import MangaTracker
from notifier import notify_new_chapter, notify_error

logger = logging.getLogger(__name__)

# Default poll interval in minutes — short enough for near-real-time detection.
DEFAULT_POLL_MINUTES = 5

# Max parallel scrape workers — keep modest to avoid rate-limiting.
MAX_WORKERS = 5


class PollingEngine:
    """
    Runs a background thread that checks each tracked manga on a fixed interval.
    All titles are scraped in parallel (ThreadPoolExecutor) for speed.
    """

    def __init__(
        self,
        tracker: MangaTracker,
        interval_minutes: int = DEFAULT_POLL_MINUTES,
        on_new_chapter: Callable | None = None,
        on_status_update: Callable | None = None,
    ):
        self.tracker = tracker
        self.interval_minutes = max(1, interval_minutes)
        self.on_new_chapter = on_new_chapter
        self.on_status_update = on_status_update

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tracker_lock = threading.Lock()   # guards tracker writes only
        self._next_check_at: float = 0.0

    # ── control ───────────────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="manga-poller"
        )
        self._thread.start()
        logger.info("Polling engine started")

    def stop(self):
        self._stop_event.set()
        logger.info("Polling engine stopping…")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def force_check_now(self):
        """Trigger an immediate check in a new thread (non-blocking)."""
        t = threading.Thread(
            target=self._check_all, daemon=True, name="manga-force-check"
        )
        t.start()

    def set_interval(self, minutes: int):
        self.interval_minutes = max(1, minutes)
        self._next_check_at = time.time() + self.interval_minutes * 60

    def seconds_until_next_check(self) -> float:
        remaining = self._next_check_at - time.time()
        return max(0.0, remaining)

    # ── core loop ─────────────────────────────────────────────────────────────

    def _run(self):
        """Main polling loop. Wakes up in 1-second ticks."""
        self._check_all()
        self._next_check_at = time.time() + self.interval_minutes * 60

        while not self._stop_event.is_set():
            if time.time() >= self._next_check_at:
                self._check_all()
                self._next_check_at = time.time() + self.interval_minutes * 60
            self._stop_event.wait(1)

    def _check_all(self):
        """Scrape all tracked titles in PARALLEL using a thread pool."""
        with self._tracker_lock:
            entries = self.tracker.get_all()

        if not entries:
            self._emit_status("No titles being tracked.")
            return

        n = len(entries)
        self._emit_status(f"Checking {n} title(s) in parallel…")
        completed = 0

        # Each worker gets its own session to avoid socket contention
        def _worker(url: str):
            session = get_session()
            return url, self._fetch_one(url, session)

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, n)) as pool:
            futures = {pool.submit(_worker, e.url): e.url for e in entries}
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                completed += 1
                self._emit_status(f"Checked {completed}/{n}…")
                # Results are applied inside _fetch_one (thread-safe via lock)

        mins = self.interval_minutes
        self._emit_status(
            f"All checked. Next scan in {mins} min "
            f"({int(self.seconds_until_next_check())} s)."
        )

    def _fetch_one(self, url: str, session):
        """Scrape a single URL and update tracker. Thread-safe."""
        with self._tracker_lock:
            entry = self.tracker.get(url)
        if entry is None:
            return

        display = entry.display_name

        try:
            info = scrape(url, session)

            if info.latest_chapter is None:
                logger.warning(f"No chapter found for {url}")
                with self._tracker_lock:
                    self.tracker.record_error(url)
                return

            with self._tracker_lock:
                is_new = self.tracker.update_chapter(
                    url,
                    info.latest_chapter,
                    info.title,
                    info.cover_url,
                )

            if is_new:
                logger.info(
                    f"NEW chapter for {info.title}: "
                    f"Ch.{info.latest_chapter.number} — {info.latest_chapter.title}"
                )
                notify_new_chapter(
                    info.title,
                    f"Chapter {info.latest_chapter.number:.0f}: {info.latest_chapter.title}",
                    info.latest_chapter.url,
                )
                if self.on_new_chapter:
                    self.on_new_chapter(
                        url,
                        info.title,
                        info.latest_chapter.number,
                        info.latest_chapter.title,
                        info.latest_chapter.url,
                    )
            else:
                logger.debug(
                    f"No new chapter for {info.title} "
                    f"(latest stored: {entry.last_chapter_num})"
                )

        except ConnectionError as e:
            logger.error(f"Connection error for {url}: {e}")
            with self._tracker_lock:
                self.tracker.record_error(url)
                entry = self.tracker.get(url)
            if entry and entry.error_count >= 3:
                notify_error(display, str(e))

        except Exception as e:
            logger.exception(f"Unexpected error checking {url}")
            with self._tracker_lock:
                self.tracker.record_error(url)

    def _emit_status(self, message: str):
        logger.debug(f"[Status] {message}")
        if self.on_status_update:
            try:
                self.on_status_update(message)
            except Exception:
                pass
