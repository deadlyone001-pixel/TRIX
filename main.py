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

logger = logging.getLogger("manga_notifier.gui")


# ─────────────────────────────────────────────────────────────────────────────
#  Colour palette & style constants
# ─────────────────────────────────────────────────────────────────────────────

DARK_BG      = "#0f1117"
DARK_CARD    = "#1a1d27"
DARK_CARD2   = "#22263a"
ACCENT       = "#7c6af7"
ACCENT_HOVER = "#9b8fff"
TEXT_PRIMARY = "#e8eaf6"
TEXT_MUTED   = "#8a8da8"
TEXT_LINK    = "#7eb8ff"
SUCCESS      = "#4caf84"
WARNING      = "#f5a623"
ERROR_COL    = "#e05c5c"
BORDER       = "#2c3050"

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

    def __init__(self, master, entry, icons, on_remove, on_open_url, on_edit_title, **kwargs):
        super().__init__(master, bg=DARK_CARD2, **kwargs)
        self.entry = entry
        self.icons = icons
        self._build(on_remove, on_open_url, on_edit_title)

    def _build(self, on_remove, on_open_url, on_edit_title):
        self.configure(padx=12, pady=10, relief="flat", bd=0)

        # ── main row ──
        main_row = tk.Frame(self, bg=DARK_CARD2)
        main_row.pack(fill="x", expand=True)

        # ── left: status dot + title ──
        left = tk.Frame(main_row, bg=DARK_CARD2)
        left.pack(side="left", fill="x", expand=True)

        dot_color = SUCCESS if self.entry.last_chapter_num > 0 else TEXT_MUTED
        dot = tk.Label(left, text="●", fg=dot_color, bg=DARK_CARD2, font=("Segoe UI", 8))
        dot.pack(side="left", padx=(0, 8))

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

        ch_lbl = tk.Label(right, text=ch_text, fg=TEXT_MUTED,
                          bg=DARK_CARD2, font=FONT_SMALL)
        ch_lbl.pack(side="left", padx=(0, 12))

        # NEW badge and age
        if getattr(self.entry, "is_new", False):
            self.new_badge = tk.Label(right, text="NEW", fg="#ffffff", bg=SUCCESS,
                                 font=("Segoe UI", 8, "bold"), padx=4, pady=1)
            self.new_badge.pack(side="left", padx=(0, 8))
            
            age_lbl = tk.Label(right, text=self.entry.chapter_age_str, fg=SUCCESS,
                               bg=DARK_CARD2, font=FONT_SMALL)
            age_lbl.pack(side="left", padx=(0, 8))
            
            self._glow_state = 0
            self._animate_glow()

        # Error badge
        if self.entry.error_count > 0:
            err = tk.Label(right, text=f"⚠ {self.entry.error_count} err",
                           fg=WARNING, bg=DARK_CARD2, font=FONT_SMALL)
            err.pack(side="left", padx=(0, 8))

        # Open URL button
        open_btn = tk.Label(right, text="↗", fg=TEXT_LINK, bg=DARK_CARD2,
                            font=("Segoe UI", 13, "bold"), cursor="hand2")
        open_btn.pack(side="left", padx=(0, 8))
        open_btn.bind("<Button-1>", lambda e: on_open_url(self.entry.url))

        # Remove button
        rem_btn = tk.Label(right, text="✕", fg=ERROR_COL, bg=DARK_CARD2,
                           font=("Segoe UI", 11, "bold"), cursor="hand2")
        rem_btn.pack(side="left")
        rem_btn.bind("<Button-1>", lambda e: on_remove(self.entry.url))

        # ── thread history ──
        if getattr(self.entry, "history", []):
            hist_frame = tk.Frame(self, bg=DARK_CARD2)
            hist_frame.pack(fill="x", expand=True, pady=(10, 0), padx=(12, 0))
            
            for item in self.entry.history:
                row = tk.Frame(hist_frame, bg=DARK_CARD2)
                row.pack(fill="x", pady=1)
                
                # thread branch icon
                branch = tk.Label(row, text=" └─ ", fg=BORDER, bg=DARK_CARD2, font=FONT_MONO)
                branch.pack(side="left")
                
                # time
                t_str = time.strftime("%b %d, %H:%M", time.localtime(item.get("time", time.time())))
                t_lbl = tk.Label(row, text=t_str, fg=TEXT_MUTED, bg=DARK_CARD2, font=FONT_SMALL)
                t_lbl.pack(side="left", padx=(0, 10))
                
                # title
                ch_hist_lbl = tk.Label(row, text=f"Ch. {item.get('num', 0):.0f} — {item.get('title', '')}", fg=TEXT_MUTED, bg=DARK_CARD2, font=FONT_SMALL)
                ch_hist_lbl.pack(side="left")

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
            )
            card.pack(fill="x", pady=(0, 4))

            if i < len(entries) - 1:
                sep = tk.Frame(self._list_frame, bg=BORDER, height=1)
                sep.pack(fill="x", pady=(0, 4))

    def _open_url(self, url: str):
        webbrowser.open(url)
        
    def _show_about(self):
        AboutDialog(self)

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
        try:
            from PIL import Image, ImageTk
            import sys
            
            if getattr(sys, 'frozen', False):
                assets_dir = Path(sys._MEIPASS) / "data" / "assets"
            else:
                assets_dir = Path(__file__).parent / "data" / "assets"
                
            app_icon_path = assets_dir / "app_icon.png"
            img = Image.open(app_icon_path)
            img = img.resize((72, 72), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            tk.Label(self, image=self._photo, bg=DARK_CARD).pack(pady=(20, 0))
        except Exception:
            pass

        tk.Label(self, text="Manga Notifier", fg=TEXT_PRIMARY, bg=DARK_CARD, font=("Segoe UI", 16, "bold")).pack(pady=(10, 5))
        tk.Label(self, text="Your premium manga tracking companion.", fg=TEXT_MUTED, bg=DARK_CARD, font=FONT_BODY).pack()

        frame = tk.Frame(self, bg=DARK_CARD2, padx=16, pady=12)
        frame.pack(fill="x", padx=24, pady=16)

        tk.Label(frame, text="DEVELOPER", fg=TEXT_MUTED, bg=DARK_CARD2, font=("Segoe UI", 8, "bold")).pack(anchor="w")
        tk.Label(frame, text="Ayaan4uThere", fg=TEXT_PRIMARY, bg=DARK_CARD2, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 10))

        tk.Label(frame, text="FEEDBACK & SUPPORT", fg=TEXT_MUTED, bg=DARK_CARD2, font=("Segoe UI", 8, "bold")).pack(anchor="w")
        tk.Label(frame, text="schoolboy3216@gmail.com", fg=ACCENT, bg=DARK_CARD2, font=("Segoe UI", 10)).pack(anchor="w")

        self.bind("<Escape>", lambda e: self.destroy())


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = MangaNotifierApp()
    app.mainloop()
