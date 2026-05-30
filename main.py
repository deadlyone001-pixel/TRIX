"""
main.py — Manga Notifier — Main GUI entry point.
Modern dark-themed tkinter app for tracking manga chapter updates.
"""

import logging
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
    ASSETS_DIR = Path(sys._MEIPASS) / "data" / "assets"
else:
    BASE_DIR = Path(__file__).parent
    ASSETS_DIR = BASE_DIR / "data" / "assets"

LOG_DIR = BASE_DIR / "data"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "manga_notifier.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

from poller import PollingEngine
from tracker import MangaTracker
from themes import THEMES, THEME_NAMES, DEFAULT_THEME

logger = logging.getLogger("manga_notifier.gui")

# ── Live theme dict (acts like CSS variables) ─────────────────────────────────
T: dict = dict(THEMES[DEFAULT_THEME])  # mutable; reassigned on theme switch

PREFS_FILE = BASE_DIR / "data" / "prefs.json"

def _load_prefs() -> dict:
    try:
        import json
        return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_prefs(d: dict):
    import json
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


# Load saved theme
_prefs = _load_prefs()
_saved = _prefs.get("theme", DEFAULT_THEME)
if _saved in THEMES:
    T.update(THEMES[_saved])

# ─ Global color strings (updated by set_theme()) ──────────────────────────────
DARK_BG      = T["bg"]
DARK_CARD    = T["card"]
DARK_CARD2   = T["card2"]
ACCENT       = T["accent"]
ACCENT_HOVER = T["accent_hover"]
TEXT_PRIMARY = T["text"]
TEXT_MUTED   = T["muted"]
TEXT_LINK    = T["link"]
SUCCESS      = T["success"]
WARNING      = T["warning"]
ERROR_COL    = T["error"]
DANGER       = T["error"]   # alias used for hiatus indicator
BORDER       = T["border"]

def _sync_globals():
    """Copy T into the module-level color globals so widget constructors pick them up."""
    import main as _m
    for attr, key in [("DARK_BG","bg"),("DARK_CARD","card"),("DARK_CARD2","card2"),
                      ("ACCENT","accent"),("ACCENT_HOVER","accent_hover"),
                      ("TEXT_PRIMARY","text"),("TEXT_MUTED","muted"),("TEXT_LINK","link"),
                      ("SUCCESS","success"),("WARNING","warning"),
                      ("ERROR_COL","error"),("DANGER","error"),("BORDER","border")]:
        setattr(_m, attr, T[key])

FONT_TITLE   = ("Segoe UI", 18, "bold")
FONT_HEADING = ("Segoe UI", 11, "bold")
FONT_BODY    = ("Segoe UI", 10)
FONT_SMALL   = ("Segoe UI", 9)
FONT_MONO    = ("Consolas", 9)

DEFAULT_INTERVAL = 5  # minutes


# ─────────────────────────────────────────────────────────────────────────────
#  Custom Widgets
# ─────────────────────────────────────────────────────────────────────────────

class RoundedButton(tk.Frame):
    """A styled button using a Frame + Label, with hover color animation."""

    def __init__(self, master, text, command=None,
                 bg=ACCENT, fg=TEXT_PRIMARY, hover_bg=ACCENT_HOVER,
                 width=140, height=34, radius=17, font=FONT_BODY, **kwargs):
        # Use the master's bg for the outer frame to blend in
        try:
            outer_bg = master.cget("bg")
        except Exception:
            outer_bg = DARK_BG

        super().__init__(master, bg=outer_bg, **kwargs)

        self._bg = bg
        self._hover_bg = hover_bg
        self._fg = fg
        self._command = command

        # Inner label acts as the button face
        self._lbl = tk.Label(
            self, text=text, bg=bg, fg=fg, font=font,
            padx=14, pady=6, cursor="hand2",
            relief="flat", bd=0,
        )
        self._lbl.pack(fill="both", expand=True)

        self._lbl.bind("<Enter>", self._on_enter)
        self._lbl.bind("<Leave>", self._on_leave)
        self._lbl.bind("<Button-1>", self._click)
        self._lbl.bind("<ButtonRelease-1>", self._on_leave)

    def _on_enter(self, _e=None):
        self._lbl.configure(bg=self._hover_bg)

    def _on_leave(self, _e=None):
        self._lbl.configure(bg=self._bg)

    def _click(self, _e=None):
        if self._command:
            self._command()

    def config_text(self, text: str):
        self._lbl.configure(text=text)



class MangaCard(tk.Frame):
    """A single manga entry card in the list."""

    def __init__(self, master, entry, icons, on_remove, on_open_url, on_edit_title, on_status_change=None, **kwargs):
        super().__init__(master, bg=DARK_CARD2, **kwargs)
        self.entry = entry
        self.icons = icons
        self._on_status_change = on_status_change
        self._build(on_remove, on_open_url, on_edit_title)

    def _build(self, on_remove, on_open_url, on_edit_title):
        self.configure(padx=12, pady=10, relief="flat", bd=0)
        self._thread_expanded = False
        history = getattr(self.entry, "history", [])

        # ── main row ──
        main_row = tk.Frame(self, bg=DARK_CARD2)
        main_row.pack(fill="x", expand=True)

        # ── left: expand arrow + status dot + site icon + title ──
        left = tk.Frame(main_row, bg=DARK_CARD2)
        left.pack(side="left", fill="x", expand=True)

        # Expand/collapse arrow (Reddit-style thread toggle)
        if history:
            self._arrow_lbl = tk.Label(left, text="▶", fg=TEXT_MUTED, bg=DARK_CARD2,
                                       font=("Segoe UI", 8), cursor="hand2")
            self._arrow_lbl.pack(side="left", padx=(0, 6))
            self._arrow_lbl.bind("<Button-1>", self._toggle_thread)
        else:
            tk.Label(left, text=" ", bg=DARK_CARD2, width=2).pack(side="left")

        # Status dot — click to cycle: unknown → active → hiatus → unknown
        STATUS_CYCLE = ["unknown", "active", "hiatus"]
        STATUS_COLORS = {"unknown": TEXT_MUTED, "active": SUCCESS, "hiatus": DANGER}
        STATUS_TIPS   = {
            "unknown": "Status unknown  |  Click to set",
            "active":  "Active  |  Click to change",
            "hiatus":  "On hiatus  |  Click to change",
        }
        current_status = getattr(self.entry, "user_status", "unknown")
        dot_color = STATUS_COLORS.get(current_status, TEXT_MUTED)

        dot = tk.Label(left, text="●", fg=dot_color, bg=DARK_CARD2,
                       font=("Segoe UI", 9), cursor="hand2")
        dot.pack(side="left", padx=(0, 6))

        def _cycle_status(e=None):
            states = STATUS_CYCLE
            cur = getattr(self.entry, "user_status", "unknown")
            nxt = states[(states.index(cur) + 1) % len(states)] if cur in states else "active"
            self.entry.user_status = nxt
            dot.configure(fg=STATUS_COLORS[nxt])
            _update_tip(nxt)
            if self._on_status_change:
                self._on_status_change(self.entry.url, nxt)

        def _update_tip(status):
            nonlocal _current_tip
            _current_tip = STATUS_TIPS.get(status, "")

        _current_tip = STATUS_TIPS.get(current_status, "")

        def _show_tip(e):
            self._tip = tk.Toplevel(self)
            self._tip.wm_overrideredirect(True)
            self._tip.wm_geometry(f"+{e.x_root+12}+{e.y_root+4}")
            tk.Label(self._tip, text=_current_tip, bg="#2a2a2a", fg="#ffffff",
                     font=("Segoe UI", 8), padx=6, pady=3, relief="solid", bd=1).pack()

        def _hide_tip(e):
            if hasattr(self, "_tip") and self._tip:
                self._tip.destroy(); self._tip = None

        dot.bind("<Button-1>", _cycle_status)
        dot.bind("<Enter>", _show_tip)
        dot.bind("<Leave>", _hide_tip)

        domain = ""
        if "kuaikanmanhua.com" in self.entry.url: domain = "kuaikan"
        elif "bilibili.com" in self.entry.url: domain = "bilibili"
        elif "mangadex.org" in self.entry.url: domain = "mangadex"
        elif "ac.qq.com" in self.entry.url: domain = "ac_qq"

        if domain and domain in self.icons:
            icon_lbl = tk.Label(left, image=self.icons[domain], bg=DARK_CARD2)
            icon_lbl.pack(side="left", padx=(0, 8))

        title_txt = self.entry.display_name
        if not getattr(self.entry, "title_resolved", False):
            title_txt = "⏳ Resolving title..."
        elif len(title_txt) > 35:
            title_txt = title_txt[:32] + "…"
        title = tk.Label(left, text=title_txt, fg=TEXT_PRIMARY,
                         bg=DARK_CARD2, font=FONT_HEADING, anchor="w", cursor="hand2")
        title.pack(side="left")
        title.bind("<Button-3>", lambda e: on_edit_title(self.entry.url, self.entry.display_name))

        # ── right: chapter info + buttons ──
        right = tk.Frame(main_row, bg=DARK_CARD2)
        right.pack(side="right")

        if getattr(self.entry, "last_chapter_num", -1) > 0:
            ch_text = f"Ch. {self.entry.last_chapter_num:.0f} — {self.entry.last_chapter_title}"
            if len(ch_text) > 28:
                ch_text = ch_text[:25] + "…"
        else:
            ch_text = "Not checked yet"

        ch_lbl = tk.Label(right, text=ch_text, fg=TEXT_MUTED, bg=DARK_CARD2, font=FONT_SMALL)
        ch_lbl.pack(side="left", padx=(0, 12))

        if getattr(self.entry, "is_new", False):
            self.new_badge = tk.Label(right, text="NEW", fg="#ffffff", bg=SUCCESS,
                                 font=("Segoe UI", 8, "bold"), padx=4, pady=1)
            self.new_badge.pack(side="left", padx=(0, 8))
            age_lbl = tk.Label(right, text=self.entry.chapter_age_str, fg=SUCCESS,
                               bg=DARK_CARD2, font=FONT_SMALL)
            age_lbl.pack(side="left", padx=(0, 8))
            self._glow_state = 0
            self._animate_glow()

        if self.entry.error_count > 0:
            err = tk.Label(right, text=f"⚠ {self.entry.error_count} err",
                           fg=WARNING, bg=DARK_CARD2, font=FONT_SMALL)
            err.pack(side="left", padx=(0, 8))

        open_btn = tk.Label(right, text="↗", fg=TEXT_LINK, bg=DARK_CARD2,
                            font=("Segoe UI", 13, "bold"), cursor="hand2")
        open_btn.pack(side="left", padx=(0, 8))
        open_btn.bind("<Button-1>", lambda e: on_open_url(self.entry.url))

        rem_btn = tk.Label(right, text="✕", fg=ERROR_COL, bg=DARK_CARD2,
                           font=("Segoe UI", 11, "bold"), cursor="hand2")
        rem_btn.pack(side="left")
        rem_btn.bind("<Button-1>", lambda e: on_remove(self.entry.url))

        # ── thread history (collapsible) ──
        self._thread_frame = tk.Frame(self, bg=DARK_CARD2)
        # not packed yet — only shown on expand
        if history:
            self._build_thread(history, on_open_url)

    def _build_thread(self, history, on_open_url):
        """Populate the thread frame with Reddit-style chapter history rows."""
        # Header label for the thread
        hdr = tk.Frame(self._thread_frame, bg=DARK_CARD2)
        hdr.pack(fill="x", pady=(6, 2), padx=(16, 0))
        tk.Label(hdr, text="CHAPTER HISTORY", fg=TEXT_MUTED, bg=DARK_CARD2,
                 font=("Segoe UI", 7, "bold")).pack(side="left")

        indent_colors = [ACCENT, "#4caf84", "#f5a623", "#7eb8ff", "#e05c5c"]
        for idx, item in enumerate(history):
            bar_color = indent_colors[idx % len(indent_colors)]
            row = tk.Frame(self._thread_frame, bg=DARK_CARD2)
            row.pack(fill="x", pady=2, padx=(16, 8))

            # colored vertical bar (Reddit thread style)
            bar = tk.Frame(row, bg=bar_color, width=3)
            bar.pack(side="left", fill="y", padx=(0, 10))
            bar.pack_propagate(False)

            # content block
            content = tk.Frame(row, bg=DARK_CARD2)
            content.pack(side="left", fill="x", expand=True)

            t_str = time.strftime("%b %d, %H:%M", time.localtime(item.get("time", time.time())))
            top_row = tk.Frame(content, bg=DARK_CARD2)
            top_row.pack(fill="x")
            tk.Label(top_row, text=f"Ch. {item.get('num', 0):.0f}",
                     fg=TEXT_PRIMARY, bg=DARK_CARD2, font=("Segoe UI", 9, "bold")).pack(side="left")
            tk.Label(top_row, text=f"  ·  {t_str}",
                     fg=TEXT_MUTED, bg=DARK_CARD2, font=FONT_SMALL).pack(side="left")

            ch_title = item.get("title", "")
            if ch_title:
                tk.Label(content, text=ch_title, fg=TEXT_MUTED, bg=DARK_CARD2,
                         font=FONT_SMALL, anchor="w").pack(fill="x")

            ch_url = item.get("url", "")
            if ch_url:
                link = tk.Label(content, text="Open chapter ↗", fg=TEXT_LINK,
                                bg=DARK_CARD2, font=FONT_SMALL, cursor="hand2")
                link.pack(anchor="w")
                link.bind("<Button-1>", lambda e, u=ch_url: on_open_url(u))

    def _toggle_thread(self, _e=None):
        self._thread_expanded = not self._thread_expanded
        if self._thread_expanded:
            self._thread_frame.pack(fill="x", expand=True, pady=(4, 4))
            self._arrow_lbl.configure(text="▼", fg=ACCENT)
        else:
            self._thread_frame.pack_forget()
            self._arrow_lbl.configure(text="▶", fg=TEXT_MUTED)

    def _animate_glow(self):
        # Cycle colors to simulate a pulse/glow
        colors = ["#4caf50", "#55c95a", "#5fed66", "#55c95a"]
        if hasattr(self, "new_badge") and self.new_badge.winfo_exists():
            self._glow_state = (self._glow_state + 1) % len(colors)
            self.new_badge.configure(bg=colors[self._glow_state])
            self.after(350, self._animate_glow)


# ─────────────────────────────────────────────────────────────────────────────
#  Main Application Window
# ─────────────────────────────────────────────────────────────────────────────

class MangaNotifierApp(tk.Tk):

    def __init__(self):
        super().__init__()

        self.title("Manga Notifier")
        self.geometry("860x620")
        self.minsize(720, 500)
        self.configure(bg=DARK_BG)
        self.resizable(True, True)

        # Set taskbar icon if available
        icon_path = Path(__file__).parent / "assets" / "icon.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        # ── Core objects ──────────────────────────────────────────────────
        self.tracker = MangaTracker()
        self.engine = PollingEngine(
            self.tracker,
            interval_minutes=DEFAULT_INTERVAL,
            on_new_chapter=self._on_new_chapter_callback,
            on_status_update=self._on_status_callback,
        )
        
        self.site_icons = {}
        for site in ["kuaikan", "bilibili", "mangadex", "ac_qq"]:
            p = ASSETS_DIR / f"{site}.png"
            if p.exists():
                try:
                    self.site_icons[site] = tk.PhotoImage(file=str(p))
                except Exception:
                    pass

        self._interval_var = tk.IntVar(value=DEFAULT_INTERVAL)

        # ── Build UI ──────────────────────────────────────────────────────
        self._build_ui()

        # ── Start engine ──────────────────────────────────────────────────
        self.engine.start()
        self._refresh_list()

        # On close
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Set window icon
        app_icon_path = ASSETS_DIR / "app_icon.ico"
        if app_icon_path.exists():
            self.iconbitmap(str(app_icon_path))

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()
        main = tk.Frame(self, bg=DARK_BG)
        main.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self._build_manga_list(main)
        self._build_status_bar()

    def _build_header(self):
        hdr = tk.Frame(self, bg=DARK_CARD, pady=12, padx=20)
        hdr.pack(fill="x")

        # Logo + title
        logo_frame = tk.Frame(hdr, bg=DARK_CARD)
        logo_frame.pack(side="left")

        # Try to load app_icon.png as the logo
        self._header_logo = None
        try:
            from PIL import Image, ImageTk
            _icon_path = ASSETS_DIR / "app_icon.png"
            if _icon_path.exists():
                _img = Image.open(_icon_path).resize((48, 48), Image.LANCZOS)
                self._header_logo = ImageTk.PhotoImage(_img)
                tk.Label(logo_frame, image=self._header_logo, bg=DARK_CARD).pack(side="left", padx=(0, 10))
            else:
                tk.Label(logo_frame, text="📖", bg=DARK_CARD, font=("Segoe UI", 22)).pack(side="left", padx=(0, 10))
        except Exception:
            tk.Label(logo_frame, text="📖", bg=DARK_CARD, font=("Segoe UI", 22)).pack(side="left", padx=(0, 10))

        title_frame = tk.Frame(logo_frame, bg=DARK_CARD)
        title_frame.pack(side="left")
        tk.Label(title_frame, text="Manga Notifier", fg=TEXT_PRIMARY,
                 bg=DARK_CARD, font=FONT_TITLE).pack(anchor="w")
        tk.Label(title_frame, text="Get notified when new chapters drop",
                 fg=TEXT_MUTED, bg=DARK_CARD, font=FONT_SMALL).pack(anchor="w")

        # Controls on the right
        ctrl = tk.Frame(hdr, bg=DARK_CARD)
        ctrl.pack(side="right")

        # Interval control
        iv_frame = tk.Frame(ctrl, bg=DARK_CARD)
        iv_frame.pack(side="left", padx=(0, 16))
        tk.Label(iv_frame, text="Check every", fg=TEXT_MUTED,
                 bg=DARK_CARD, font=FONT_SMALL).pack(side="left", padx=(0, 4))
        iv_spin = tk.Spinbox(
            iv_frame, from_=1, to=1440, increment=1, width=4,
            textvariable=self._interval_var,
            bg=DARK_CARD2, fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY,
            buttonbackground=DARK_CARD2, relief="flat",
            font=FONT_BODY,
            command=self._on_interval_change,
        )
        iv_spin.pack(side="left")
        tk.Label(iv_frame, text="min", fg=TEXT_MUTED,
                 bg=DARK_CARD, font=FONT_SMALL).pack(side="left", padx=(4, 0))

        # Check now button
        self._check_btn = RoundedButton(ctrl, "Check Now", command=self._check_now,
                                        bg=DARK_CARD2, fg=TEXT_PRIMARY, hover_bg=BORDER,
                                        width=110, height=32)
        self._check_btn.pack(side="left", padx=(0, 8))

        # Add manga button
        add_btn = RoundedButton(ctrl, "+ Add Manga", command=self._add_manga,
                                bg=ACCENT, fg=TEXT_PRIMARY, hover_bg=ACCENT_HOVER,
                                width=120, height=32)
        add_btn.pack(side="left")

        info_btn = RoundedButton(ctrl, "i", command=self._show_about,
                                 bg=DARK_CARD2, fg=TEXT_MUTED, hover_bg=BORDER,
                                 width=32, height=32)
        info_btn.pack(side="left", padx=(8, 0))

        # Theme picker
        prefs = _load_prefs()
        self._theme_var = tk.StringVar(value=prefs.get("theme", DEFAULT_THEME))
        theme_menu = tk.OptionMenu(ctrl, self._theme_var, *THEME_NAMES,
                                   command=self._on_theme_change)
        theme_menu.configure(
            bg=DARK_CARD2, fg=TEXT_MUTED, activebackground=BORDER,
            activeforeground=TEXT_PRIMARY, highlightthickness=0,
            relief="flat", font=FONT_SMALL, borderwidth=0,
            indicatoron=False,
        )
        theme_menu["menu"].configure(
            bg=DARK_CARD, fg=TEXT_PRIMARY,
            activebackground=ACCENT, activeforeground=TEXT_PRIMARY,
            font=FONT_SMALL, borderwidth=0,
        )
        theme_menu.pack(side="left", padx=(6, 0))

    def _build_manga_list(self, parent):
        left_pane = tk.Frame(parent, bg=DARK_BG)
        left_pane.pack(fill="both", expand=True, pady=(12, 0))

        # Section heading
        heading_row = tk.Frame(left_pane, bg=DARK_BG)
        heading_row.pack(fill="x", pady=(0, 8))
        tk.Label(heading_row, text="TRACKED TITLES", fg=TEXT_MUTED,
                 bg=DARK_BG, font=("Segoe UI", 9, "bold")).pack(side="left")

        self._count_lbl = tk.Label(heading_row, text="0 titles", fg=TEXT_MUTED,
                                   bg=DARK_BG, font=FONT_SMALL)
        self._count_lbl.pack(side="right")

        # Scrollable card container
        container = tk.Frame(left_pane, bg=DARK_BG)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, bg=DARK_BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._list_frame = tk.Frame(canvas, bg=DARK_BG)
        self._canvas_window = canvas.create_window((0, 0), window=self._list_frame, anchor="nw")

        def _on_resize(event):
            canvas.itemconfig(self._canvas_window, width=event.width)

        def _on_frame_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        canvas.bind("<Configure>", _on_resize)
        self._list_frame.bind("<Configure>", _on_frame_configure)

        # Mousewheel scroll
        def _on_mousewheel(event):
            # Only scroll if the content is taller than the canvas view
            if self._list_frame.winfo_height() > canvas.winfo_height():
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._manga_canvas = canvas

    def _build_status_bar(self):
        # ── Legend bar (above status bar) ────────────────────────────────────
        legend = tk.Frame(self, bg=DARK_CARD, height=24)
        legend.pack(fill="x", side="bottom")
        legend.pack_propagate(False)

        tk.Label(legend, text="Dot status — click to set:",
                 fg=TEXT_MUTED, bg=DARK_CARD, font=("Segoe UI", 8),
                 padx=12).pack(side="left")
        for color, label in [(SUCCESS, "Active"), (DANGER, "Hiatus"), (TEXT_MUTED, "Unknown")]:
            tk.Label(legend, text="●", fg=color, bg=DARK_CARD, font=("Segoe UI", 9)
                     ).pack(side="left", padx=(8, 2))
            tk.Label(legend, text=label, fg=TEXT_MUTED, bg=DARK_CARD,
                     font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))

        # ── Status bar ────────────────────────────────────────────────────────
        bar = tk.Frame(self, bg=DARK_CARD, height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._status_var = tk.StringVar(value="Starting up…")
        tk.Label(bar, textvariable=self._status_var,
                 fg=TEXT_MUTED, bg=DARK_CARD, font=FONT_SMALL,
                 padx=16).pack(side="left", fill="y")

        self._indicator_var = tk.StringVar(value="● ACTIVE")
        self._indicator_lbl = tk.Label(bar, textvariable=self._indicator_var,
                                       fg=SUCCESS, bg=DARK_CARD, font=FONT_SMALL,
                                       padx=16)
        self._indicator_lbl.pack(side="right", fill="y")

        self._pulse_state = True
        self._animate_indicator()

    # ── Refresh UI ────────────────────────────────────────────────────────────

    def _refresh_list(self):
        """Rebuild the manga card list from current tracker data."""
        for widget in self._list_frame.winfo_children():
            widget.destroy()

        entries = self.tracker.get_all()
        self._count_lbl.config(text=f"{len(entries)} title{'s' if len(entries) != 1 else ''}")

        if not entries:
            tk.Label(
                self._list_frame,
                text="No manga tracked yet.\nClick  + Add Manga  to get started!",
                fg=TEXT_MUTED, bg=DARK_BG,
                font=("Segoe UI", 11),
                justify="center",
            ).pack(pady=60)
            return

        for i, entry in enumerate(entries):
            card = MangaCard(
                self._list_frame, entry, self.site_icons,
                on_remove=self._remove_manga,
                on_open_url=self._open_url,
                on_edit_title=self._edit_manga_title,
                on_status_change=self._set_manga_status,
            )
            card.pack(fill="x", pady=(0, 4))

            if i < len(entries) - 1:
                sep = tk.Frame(self._list_frame, bg=BORDER, height=1)
                sep.pack(fill="x", pady=(0, 4))

    def _open_url(self, url: str):
        webbrowser.open(url)

    def _set_manga_status(self, url: str, status: str):
        """Called when the user clicks a card's status dot to cycle its state."""
        self.tracker.set_user_status(url, status)
        
    def _show_about(self):
        AboutDialog(self)

    # ── Theme system ──────────────────────────────────────────────────────────

    def _on_theme_change(self, name: str):
        """Called when user picks a theme from the OptionMenu."""
        global T, DARK_BG, DARK_CARD, DARK_CARD2, ACCENT, ACCENT_HOVER
        global TEXT_PRIMARY, TEXT_MUTED, TEXT_LINK, SUCCESS, WARNING, ERROR_COL, BORDER
        T.update(THEMES[name])
        _sync_globals()
        # Update globals in this module's namespace too
        DARK_BG      = T["bg"]
        DARK_CARD    = T["card"]
        DARK_CARD2   = T["card2"]
        ACCENT       = T["accent"]
        ACCENT_HOVER = T["accent_hover"]
        TEXT_PRIMARY = T["text"]
        TEXT_MUTED   = T["muted"]
        TEXT_LINK    = T["link"]
        SUCCESS      = T["success"]
        WARNING      = T["warning"]
        ERROR_COL    = T["error"]
        BORDER       = T["border"]
        # Persist
        p = _load_prefs()
        p["theme"] = name
        _save_prefs(p)
        # Recolor root and rebuild
        self.configure(bg=DARK_BG)
        self._recolor_widget(self)
        self._refresh_list()

    def _recolor_widget(self, widget):
        """Recursively recolor all bg/fg-able widgets to current theme."""
        _bg_map = {
            DARK_BG: T["bg"], DARK_CARD: T["card"], DARK_CARD2: T["card2"],
            BORDER: T["border"],
        }
        try:
            cur_bg = widget.cget("bg")
            # Map old bg to new bg
            new_bg = None
            for old, new in _bg_map.items():
                if cur_bg == old or cur_bg in (old, T["bg"], T["card"], T["card2"]):
                    new_bg = new
                    break
            if new_bg is None:
                new_bg = T["bg"]
            widget.configure(bg=new_bg)
        except Exception:
            pass
        try:
            cur_fg = widget.cget("fg")
            if cur_fg in (TEXT_MUTED, T["muted"]):
                widget.configure(fg=T["muted"])
            elif cur_fg in (TEXT_PRIMARY, T["text"]):
                widget.configure(fg=T["text"])
            elif cur_fg in (TEXT_LINK, T["link"]):
                widget.configure(fg=T["link"])
            elif cur_fg in (ACCENT, T["accent"]):
                widget.configure(fg=T["accent"])
            elif cur_fg in (SUCCESS, T["success"]):
                widget.configure(fg=T["success"])
            elif cur_fg in (ERROR_COL, T["error"]):
                widget.configure(fg=T["error"])
        except Exception:
            pass
        for child in widget.winfo_children():
            self._recolor_widget(child)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _add_manga(self):
        dlg = AddMangaDialog(self)
        self.wait_window(dlg)
        if dlg.result_url:
            url = dlg.result_url.strip()
            name = dlg.result_name.strip()
            self.tracker.add(url, name)
            self._refresh_list()
            logger.info(f"Added: {name or url}")
            t = threading.Thread(
                target=self._check_one_deferred,
                args=(url,),
                daemon=True,
            )
            t.start()

    def _check_one_deferred(self, url: str):
        self.engine._check_one(url)
        self.after(0, self._refresh_list)

    def _remove_manga(self, url: str):
        entry = self.tracker.get(url)
        name = entry.display_name if entry else url
        if messagebox.askyesno("Remove", f"Stop tracking\n«{name}»?", parent=self):
            self.tracker.remove(url)
            self._refresh_list()
            logger.info(f"Removed: {name}")

    def _edit_manga_title(self, url: str, current_name: str):
        dlg = EditTitleDialog(self, current_name)
        self.wait_window(dlg)
        if dlg.result_name is not None:
            new_name = dlg.result_name.strip()
            entry = self.tracker.get(url)
            if entry:
                entry.display_name = new_name or url
                entry.has_custom_title = bool(new_name)
                entry.title_resolved = True
                self.tracker.save()
                self._refresh_list()
                logger.info(f"Edited title: {new_name}")

    def _check_now(self):
        logger.info("Manual check triggered…")
        self.engine.force_check_now()
        self.after(3000, self._refresh_list)
        self.after(8000, self._refresh_list)
        self.after(20000, self._refresh_list)

    def _on_interval_change(self):
        val = self._interval_var.get()
        if val >= 1:
            self.engine.set_interval(val)
            logger.info(f"Interval set to {val} min")

    # ── Callbacks from poller ─────────────────────────────────────────────────

    def _on_new_chapter_callback(self, url, manga_title, ch_num, ch_title, ch_url):
        msg = f"🆕 {manga_title} — Ch.{ch_num:.0f}: {ch_title}"
        logger.info(msg)
        self.after(0, self._refresh_list)

    def _on_status_callback(self, message: str):
        self.after(0, lambda: self._status_var.set(message))
        self.after(500, self._refresh_list)

    # ── Animations ────────────────────────────────────────────────────────────

    def _animate_indicator(self):
        if self.engine.is_running():
            self._pulse_state = not getattr(self, "_pulse_state", False)
            color = SUCCESS if self._pulse_state else DARK_CARD
            self._indicator_lbl.config(fg=color, text="● ACTIVE")
            
            # Auto-update countdown timer in status bar
            curr_status = self._status_var.get()
            if "Next scan in" in curr_status or "All checked" in curr_status:
                secs = int(self.engine.seconds_until_next_check())
                mins = self.engine.interval_minutes
                self._status_var.set(f"All checked. Next scan in {mins} min ({secs} s).")
        else:
            self._indicator_lbl.config(fg=ERROR_COL, text="● STOPPED")
        self.after(1000, self._animate_indicator)

    # ── Window close ──────────────────────────────────────────────────────────

    def _on_close(self):
        self.engine.stop()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Add Manga Dialog
# ─────────────────────────────────────────────────────────────────────────────

class AddMangaDialog(tk.Toplevel):
    """Modal dialog to add a new manga URL."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Add Manga")
        self.geometry("520x250")
        self.configure(bg=DARK_CARD)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result_url = ""
        self.result_name = ""

        self._build()
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w)//2}+{py + (ph - h)//2}")

    def _build(self):
        pad = {"padx": 24, "pady": 6}

        tk.Label(self, text="Add New Manga", fg=TEXT_PRIMARY,
                 bg=DARK_CARD, font=FONT_TITLE).pack(pady=(20, 4))

        tk.Label(self, text="Paste the title or chapter listing page URL",
                 fg=TEXT_MUTED, bg=DARK_CARD, font=FONT_SMALL).pack()

        # URL field
        tk.Label(self, text="URL *", fg=TEXT_MUTED,
                 bg=DARK_CARD, font=FONT_SMALL, anchor="w").pack(fill="x", **pad)

        self._url_var = tk.StringVar()
        url_entry = tk.Entry(self, textvariable=self._url_var,
                             bg=DARK_CARD2, fg=TEXT_PRIMARY,
                             insertbackground=TEXT_PRIMARY,
                             relief="flat", font=FONT_BODY,
                             width=50)
        url_entry.pack(fill="x", padx=24, ipady=6)
        url_entry.focus_set()

        # Name field
        tk.Label(self, text="Display Name (optional)", fg=TEXT_MUTED,
                 bg=DARK_CARD, font=FONT_SMALL, anchor="w").pack(fill="x", **pad)

        self._name_var = tk.StringVar()
        name_entry = tk.Entry(self, textvariable=self._name_var,
                              bg=DARK_CARD2, fg=TEXT_PRIMARY,
                              insertbackground=TEXT_PRIMARY,
                              relief="flat", font=FONT_BODY,
                              width=50)
        name_entry.pack(fill="x", padx=24, ipady=6)

        # Buttons
        btn_row = tk.Frame(self, bg=DARK_CARD)
        btn_row.pack(pady=(16, 0))

        cancel_btn = RoundedButton(btn_row, "Cancel", command=self.destroy,
                                   bg=DARK_CARD2, fg=TEXT_MUTED, hover_bg=BORDER,
                                   width=100, height=32)
        cancel_btn.pack(side="left", padx=(0, 12))

        add_btn = RoundedButton(btn_row, "Add", command=self._submit,
                                bg=ACCENT, fg=TEXT_PRIMARY, hover_bg=ACCENT_HOVER,
                                width=100, height=32)
        add_btn.pack(side="left")

        self.bind("<Return>", lambda e: self._submit())
        self.bind("<Escape>", lambda e: self.destroy())

    def _submit(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Please enter a URL.", parent=self)
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            messagebox.showerror("Invalid URL", "URL must start with http:// or https://", parent=self)
            return
        self.result_url = url
        self.result_name = self._name_var.get().strip()
        self.destroy()

class EditTitleDialog(tk.Toplevel):
    """Modal dialog to edit the display name of a manga."""

    def __init__(self, parent, current_name):
        super().__init__(parent)
        self.title("Edit Title")
        self.geometry("400x180")
        self.configure(bg=DARK_CARD)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result_name = None
        self.current_name = current_name
        self._build()
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w)//2}+{py + (ph - h)//2}")

    def _build(self):
        tk.Label(self, text="Edit Display Name", fg=TEXT_PRIMARY,
                 bg=DARK_CARD, font=FONT_HEADING).pack(pady=(20, 10))

        self._name_var = tk.StringVar(value=self.current_name)
        name_entry = tk.Entry(self, textvariable=self._name_var,
                              bg=DARK_CARD2, fg=TEXT_PRIMARY,
                              insertbackground=TEXT_PRIMARY,
                              relief="flat", font=FONT_BODY,
                              width=40)
        name_entry.pack(fill="x", padx=24, ipady=6)
        name_entry.focus_set()
        name_entry.select_range(0, tk.END)

        btn_row = tk.Frame(self, bg=DARK_CARD)
        btn_row.pack(pady=(16, 0))

        cancel_btn = RoundedButton(btn_row, "Cancel", command=self.destroy,
                                   bg=DARK_CARD2, fg=TEXT_MUTED, hover_bg=BORDER,
                                   width=90, height=30)
        cancel_btn.pack(side="left", padx=(0, 12))

        save_btn = RoundedButton(btn_row, "Save", command=self._submit,
                                 bg=ACCENT, fg=TEXT_PRIMARY, hover_bg=ACCENT_HOVER,
                                 width=90, height=30)
        save_btn.pack(side="left")

        self.bind("<Return>", lambda e: self._submit())
        self.bind("<Escape>", lambda e: self.destroy())

    def _submit(self):
        self.result_name = self._name_var.get()
        self.destroy()

class AboutDialog(tk.Toplevel):
    """Modal dialog displaying developer credits."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("About Manga Notifier")
        self.geometry("360x360")
        self.configure(bg=DARK_CARD)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._build()
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w)//2}+{py + (ph - h)//2}")

    def _build(self):
        # ── icon ──
        try:
            from PIL import Image, ImageTk
            _p = ASSETS_DIR / "app_icon.png"
            if _p.exists():
                _img = Image.open(_p).resize((80, 80), Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(_img)
                tk.Label(self, image=self._photo, bg=DARK_CARD).pack(pady=(20, 0))
        except Exception:
            tk.Label(self, text="📖", bg=DARK_CARD, font=("Segoe UI", 36)).pack(pady=(20, 0))

        tk.Label(self, text="Manga Notifier", fg=TEXT_PRIMARY, bg=DARK_CARD,
                 font=("Segoe UI", 16, "bold")).pack(pady=(8, 2))
        tk.Label(self, text="Your premium manga tracking companion.",
                 fg=TEXT_MUTED, bg=DARK_CARD, font=FONT_SMALL).pack()

        # ── version pill ──
        ver = tk.Label(self, text="v1.0.0", fg=ACCENT, bg=DARK_CARD2,
                       font=("Segoe UI", 8, "bold"), padx=8, pady=2)
        ver.pack(pady=(6, 0))

        # ── info card ──
        card = tk.Frame(self, bg=DARK_CARD2, padx=16, pady=14)
        card.pack(fill="x", padx=24, pady=14)

        tk.Label(card, text="DEVELOPER", fg=TEXT_MUTED, bg=DARK_CARD2,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        tk.Label(card, text="Ayaan4uThere", fg=TEXT_PRIMARY, bg=DARK_CARD2,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(2, 10))

        sep = tk.Frame(card, bg=BORDER, height=1)
        sep.pack(fill="x", pady=(0, 10))

        tk.Label(card, text="FEEDBACK & SUPPORT", fg=TEXT_MUTED, bg=DARK_CARD2,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        email_lbl = tk.Label(card, text="✉  schoolboy3216@gmail.com", fg=ACCENT,
                             bg=DARK_CARD2, font=("Segoe UI", 10), cursor="hand2")
        email_lbl.pack(anchor="w", pady=(2, 0))
        email_lbl.bind("<Button-1>", lambda e: webbrowser.open("mailto:schoolboy3216@gmail.com"))

        sep2 = tk.Frame(card, bg=BORDER, height=1)
        sep2.pack(fill="x", pady=10)

        tk.Label(card, text="SOURCE & ISSUES", fg=TEXT_MUTED, bg=DARK_CARD2,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        gh_lbl = tk.Label(card, text="⎋  Report a bug / Request a feature", fg=TEXT_LINK,
                          bg=DARK_CARD2, font=("Segoe UI", 10), cursor="hand2")
        gh_lbl.pack(anchor="w", pady=(2, 0))
        gh_lbl.bind("<Button-1>", lambda e: webbrowser.open("mailto:schoolboy3216@gmail.com?subject=MangaNotifier%20Feedback"))

        # ── close button ──
        close_btn = RoundedButton(self, "Close", command=self.destroy,
                                  bg=DARK_CARD2, fg=TEXT_MUTED, hover_bg=BORDER,
                                  width=100, height=30)
        close_btn.pack(pady=(0, 16))

        self.bind("<Escape>", lambda e: self.destroy())


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = MangaNotifierApp()
    app.mainloop()
