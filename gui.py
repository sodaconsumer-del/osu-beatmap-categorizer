#!/usr/bin/env python3
"""
osu! Burst/Stream/Jump Classifier - GUI
-----------------------------------------
A user-friendly front end for classify_maps.py. Works with:
  - osu!stable: point it at your osu! install folder or its Songs/ folder.
    The beatmap list is read from osu!.db when it's there, which skips the
    directory walk entirely and brings ranked status, star ratings and
    beatmap ids along with it.
  - osu!lazer: point it at your osu! data folder (the one with
    client.realm). No export step needed.
  - Anything else: a plain folder of .osu files, or .osz archives from
    BeatmapExporter, are read directly - no extraction required.

Pure standard library (tkinter) - nothing to pip install.
"""

import json
import os
import sys
import threading
import queue
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import classify_maps as cm


APP_VERSION = "1.0.0"

# Where the theme preference is remembered. Deliberately in the user's home
# directory rather than next to the executable: people drop this in Program
# Files or run it straight out of a read-only extracted zip, and a settings
# write that throws on startup would be a miserable first impression.
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".osu-beatmap-categorizer.json")


# Two palettes. Dark is the default because this is a tool for osu! players,
# who are overwhelmingly running a dark game on a dark desktop, and a
# full-white window at 2am is genuinely unpleasant.
THEMES = {
    "dark": {
        "bg": "#1e1f22",         # window and frame background
        "surface": "#2b2d31",    # entries, text area
        "fg": "#e4e6eb",         # primary text
        "muted": "#9aa0a6",      # hint text
        "accent": "#5865f2",     # selection / focus
        "border": "#3f4147",
        "disabled": "#6b6f76",
        "trough": "#111214",     # progress bar background
    },
    "light": {
        "bg": "#f5f5f5",
        "surface": "#ffffff",
        "fg": "#1a1a1a",
        "muted": "#666666",
        "accent": "#3b5bdb",
        "border": "#c8c8c8",
        "disabled": "#a0a0a0",
        "trough": "#e0e0e0",
    },
}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass  # a preference failing to save is not worth interrupting anyone over


class ClassifierGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        # Version in the title so bug reports say which build they're from
        # without anyone having to ask.
        self.title(f"osu-beatmap-categorizer {APP_VERSION}")
        self.geometry("1200x800")
        # Everything above the run controls scrolls now, so the window can go
        # much smaller than the content without hiding anything. This used to
        # be 900x650 purely because the layout clipped below that.
        self.minsize(560, 420)

        self.config_data = load_config()
        self.theme_name = self.config_data.get("theme", "dark")
        if self.theme_name not in THEMES:
            self.theme_name = "dark"
        self.style = ttk.Style(self)
        self._themed_widgets = []

        self.msg_queue = queue.Queue()
        self.worker_thread = None
        self.running = False
        self.cancel_event = None
        self.pause_event = None
        # Last "N/M files" text shown, so resuming can restore it instead of
        # leaving the label stuck on "Paused" until the next progress tick.
        self._last_progress_text = ""
        # True while the progress bar is pulsing for a phase whose total
        # isn't known yet (the directory walk).
        self._indeterminate = False

        self._closing = False
        self._poll_job = None

        self._build_widgets()
        self._apply_theme()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.after(100, self._poll_queue)

    def _on_close(self):
        """
        Shuts the poll loop down before tearing the window down, so Tk doesn't
        fire a queued `after` callback at a widget that no longer exists.
        Also stops a running scan rather than leaving the worker thread
        churning through a library after the window has gone.
        """
        if self.running:
            if not messagebox.askokcancel(
                    "Quit", "A classification is still running. Quit anyway?\n\n"
                            "Nothing will be written for this run."):
                return
            if self.cancel_event is not None:
                self.cancel_event.set()
            if self.pause_event is not None:
                self.pause_event.set()  # release a pause so the worker can see the cancel
        self._closing = True
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
        self.destroy()

    # ------------------------------------------------------------------
    # Theming
    # ------------------------------------------------------------------
    def _apply_theme(self):
        """
        Restyles every widget for the current palette.

        ttk's native Windows theme ignores most colour options - it draws
        through the OS, so setting a background on a native button does
        nothing. 'clam' is the most colour-configurable built-in theme, so we
        switch to it and style it by hand. That keeps this to the standard
        library: no pip install, nothing to bundle.
        """
        c = THEMES[self.theme_name]
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass  # extremely old Tk - fall back to whatever's active

        self.configure(bg=c["bg"])
        s = self.style
        s.configure(".", background=c["bg"], foreground=c["fg"],
                    fieldbackground=c["surface"], bordercolor=c["border"],
                    lightcolor=c["bg"], darkcolor=c["bg"], troughcolor=c["trough"],
                    focuscolor=c["accent"], insertcolor=c["fg"])
        s.configure("TFrame", background=c["bg"])
        s.configure("TLabel", background=c["bg"], foreground=c["fg"])
        s.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        s.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
        s.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        # Checkbox and radio indicators are drawn by a dedicated element with
        # its own options - indicatorbackground/indicatorforeground, NOT the
        # indicatorcolor you might expect. Getting this wrong is silent: the
        # option is ignored and the indicators stay stock white, which on a
        # dark background looks like a rendering bug.
        s.configure("TCheckbutton", background=c["bg"], foreground=c["fg"],
                    indicatorbackground=c["surface"], indicatorforeground=c["fg"],
                    upperbordercolor=c["border"], lowerbordercolor=c["border"])
        s.configure("TRadiobutton", background=c["bg"], foreground=c["fg"],
                    indicatorbackground=c["surface"], indicatorforeground=c["fg"],
                    upperbordercolor=c["border"], lowerbordercolor=c["border"])
        s.configure("TEntry", fieldbackground=c["surface"], foreground=c["fg"],
                    insertcolor=c["fg"], bordercolor=c["border"])
        s.configure("TButton", background=c["surface"], foreground=c["fg"],
                    bordercolor=c["border"], focuscolor=c["bg"])
        s.configure("TProgressbar", background=c["accent"], troughcolor=c["trough"],
                    bordercolor=c["border"], lightcolor=c["accent"], darkcolor=c["accent"])
        s.configure("TScrollbar", background=c["surface"], troughcolor=c["bg"],
                    bordercolor=c["border"], arrowcolor=c["fg"],
                    lightcolor=c["surface"], darkcolor=c["surface"])
        s.map("TScrollbar", background=[("active", c["accent"])])

        # ttk state maps: without these, hover and disabled states revert to
        # clam's stock grey and the dark theme flickers light on mouseover.
        s.map("TButton",
              background=[("active", c["accent"]), ("disabled", c["bg"])],
              foreground=[("disabled", c["disabled"])])
        s.map("TCheckbutton",
              background=[("active", c["bg"])],
              foreground=[("disabled", c["disabled"])],
              indicatorbackground=[("selected", c["accent"]),
                                    ("disabled", c["bg"]),
                                    ("!selected", c["surface"])],
              indicatorforeground=[("selected", c["fg"])])
        s.map("TRadiobutton",
              background=[("active", c["bg"])],
              foreground=[("disabled", c["disabled"])],
              indicatorbackground=[("selected", c["accent"]),
                                    ("disabled", c["bg"]),
                                    ("!selected", c["surface"])],
              indicatorforeground=[("selected", c["fg"])])
        # Entries are drawn by Entry.field, whose only colour knobs are
        # fieldbackground/bordercolor/lightcolor - a plain `background` on
        # TEntry does nothing, which is why an unstyled entry stays white.
        s.map("TEntry",
              fieldbackground=[("readonly", c["bg"]), ("disabled", c["bg"])],
              lightcolor=[("focus", c["accent"])],
              bordercolor=[("focus", c["accent"])])

        # The log is a plain tk.Text, not a ttk widget, so it needs colouring
        # directly - ttk styles don't reach it.
        self.log_text.configure(bg=c["surface"], fg=c["fg"], insertbackground=c["fg"],
                                selectbackground=c["accent"], selectforeground=c["fg"],
                                highlightbackground=c["border"], highlightcolor=c["border"])
        # The scroll viewport is a plain tk.Canvas, so like the log it needs
        # colouring directly or it shows through as a white slab behind the
        # dark sections.
        self.canvas.configure(bg=c["bg"])
        self.theme_button.config(
            text="Light mode" if self.theme_name == "dark" else "Dark mode")

    def _toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self._apply_theme()
        self.config_data["theme"] = self.theme_name
        save_config(self.config_data)

    def _rewrap(self, width):
        """
        Rewrap the explanatory labels to the current window width.

        They used to carry a hardcoded wraplength=680, which meant they broke
        mid-sentence in a narrow window and left a wide empty margin in a
        maximised one. tkinter has no automatic wrapping for ttk labels, so
        the width has to be pushed in on every resize.
        """
        target = max(width - 60, 240)

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    if int(child.cget("wraplength") or 0):
                        child.configure(wraplength=target)
                except (tk.TclError, ValueError):
                    pass  # not a label, or has no wraplength option
                walk(child)

        walk(self)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_widgets(self):
        pad = {"padx": 10, "pady": 6}

        # The seven option sections are taller than a non-maximised window, so
        # they live in a scrollable canvas. Previously they were packed
        # straight onto the root window, which silently clipped everything
        # below the fold - with no scrollbar there was no way to reach the
        # thresholds or the Run button except by maximising.
        #
        # The run controls and log are deliberately OUTSIDE the scroll area,
        # pinned to the bottom, so Run and the progress bar are reachable at
        # any window size without hunting for them.
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        body = ttk.Frame(self.canvas)
        body_id = self.canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_resize(_event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def _on_canvas_resize(event):
            # Keep the inner frame exactly as wide as the viewport so the
            # sections stretch instead of leaving a dead strip on the right.
            self.canvas.itemconfigure(body_id, width=event.width)
            self._rewrap(event.width)

        body.bind("<Configure>", _on_body_resize)
        self.canvas.bind("<Configure>", _on_canvas_resize)

        # Mouse wheel. Bound on the toplevel rather than the canvas so it
        # works wherever the pointer is, and a no-op when everything already
        # fits (otherwise the view jitters against its own scroll limits).
        #
        # bind_all fires for every widget regardless of what's under the
        # cursor - Tk runs a widget's own bindings AND "all" bindings, it
        # doesn't pick one. So without the guard below, scrolling inside the
        # log (which has its own native Text scrolling) ALSO scrolled the
        # outer canvas at the same time: the log moved one way and the whole
        # window shifted under it in the same gesture. Skip entirely when the
        # pointer is over the log so only its own scrollbar/wheel handling
        # applies.
        def _on_wheel(event):
            if event.widget is self.log_text:
                return
            first, last = self.canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return
            self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

        self.bind_all("<MouseWheel>", _on_wheel)

        # --- Input folder ---
        frame_in = ttk.LabelFrame(body, text="1. Beatmap folder")
        frame_in.pack(fill="x", **pad)

        # The entry and its buttons need their own row. Packing them side="left"
        # directly into the LabelFrame left the hint below to fill whatever
        # space was left over - which pack puts to the RIGHT of them, not
        # underneath - so the hint got squeezed into a narrow column and
        # clipped its own text.
        row_in = ttk.Frame(frame_in)
        row_in.pack(fill="x")

        self.folder_var = tk.StringVar()
        ttk.Entry(row_in, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=(10, 5), pady=8)
        ttk.Button(row_in, text="Browse...", command=self._pick_folder).pack(side="left", padx=(0, 5), pady=8)

        lazer_default = cm.default_lazer_data_dir()
        if lazer_default and os.path.isdir(lazer_default):
            ttk.Button(row_in, text="Use lazer data folder", command=self._pick_lazer_default).pack(
                side="left", padx=(0, 10), pady=8)

        hint = ttk.Label(
            frame_in,
            text="stable: your Songs folder.\n"
                 "lazer: point at your osu! data folder (e.g. %appdata%\\osu on Windows, containing "
                 "client.realm and files/) - uses a fast direct-read path if available, or falls back to "
                 "scanning files/ directly otherwise. No export needed either way.\n"
                 "You can also point it at a BeatmapExporter export folder if you'd rather work from that; "
                 ".osz files are read directly either way.",
            style="Muted.TLabel", justify="left", wraplength=680,
        )
        hint.pack(fill="x", padx=10, pady=(0, 8))

        # --- Output ---
        frame_out = ttk.LabelFrame(body, text="2. Export folder")
        frame_out.pack(fill="x", **pad)

        row0 = ttk.Frame(frame_out)
        row0.pack(fill="x", padx=10, pady=(8, 4))
        self.export_dir_var = tk.StringVar(value=os.path.join(os.getcwd(), "osu-categorizer-export"))
        ttk.Entry(row0, textvariable=self.export_dir_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row0, text="Choose...", command=self._pick_export_dir).pack(side="left")

        ttk.Label(frame_out, text="collection.db and report.csv will be written into this folder.",
                  style="Muted.TLabel").pack(anchor="w", padx=10)

        row1 = ttk.Frame(frame_out)
        row1.pack(fill="x", padx=10, pady=(6, 8))
        self.write_db_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="Write collection.db", variable=self.write_db_var).pack(side="left", padx=(0, 20))
        self.write_csv_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="Write CSV report (recommended - audit before trusting the collection.db)",
                         variable=self.write_csv_var).pack(side="left")

        # --- Category selection ---
        frame_cat = ttk.LabelFrame(body, text="3. Categories to include in collection.db")
        frame_cat.pack(fill="x", **pad)
        row_cat = ttk.Frame(frame_cat)
        row_cat.pack(fill="x", padx=10, pady=8)
        self.category_vars = {}
        for cat in ["Streams", "Bursts", "Jumps with bursts", "Jumps (no bursts)", "Misc"]:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(row_cat, text=cat, variable=var).pack(side="left", padx=(0, 16))
            self.category_vars[cat] = var
        ttk.Label(frame_cat,
                  text="Uncheck what you don't want - e.g. an aim-only player could keep just Jumps checked.",
                  style="Muted.TLabel").pack(anchor="w", padx=10, pady=(0, 8))

        # --- Ranked status handling ---
        frame_ranked = ttk.LabelFrame(body, text="4. Ranked status")
        frame_ranked.pack(fill="x", **pad)
        self.ranked_mode_var = tk.StringVar(value="all_together")
        row_ranked = ttk.Frame(frame_ranked)
        row_ranked.pack(fill="x", padx=10, pady=8)
        ranked_options = [
            ("Keep ranked & unranked together", "all_together"),
            ("Ranked only", "ranked_only"),
            ("Unranked only", "unranked_only"),
            ("Split into separate collections", "split"),
        ]
        for label, value in ranked_options:
            ttk.Radiobutton(row_ranked, text=label, variable=self.ranked_mode_var, value=value).pack(
                side="left", padx=(0, 14))
        ttk.Label(frame_ranked,
                  text="Ranked status comes from the game's own database - osu!.db on stable, "
                       "client.realm on lazer. Pointing at a bare folder of .osu/.osz files instead "
                       "(a BeatmapExporter export, or lazer's files/ folder on its own) has no "
                       "database to read, so ranked status is unknown there.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", padx=10, pady=(0, 8))

        # --- Star rating filter ---
        frame_star = ttk.LabelFrame(body, text="5. Star rating filter")
        frame_star.pack(fill="x", **pad)
        row_star = ttk.Frame(frame_star)
        row_star.pack(fill="x", padx=10, pady=8)
        ttk.Label(row_star, text="Min Star:").pack(side="left")
        self.min_star_var = tk.StringVar(value="")
        ttk.Entry(row_star, textvariable=self.min_star_var, width=8).pack(side="left", padx=(4, 20))
        ttk.Label(row_star, text="Max Star:").pack(side="left")
        self.max_star_var = tk.StringVar(value="")
        ttk.Entry(row_star, textvariable=self.max_star_var, width=8).pack(side="left", padx=(4, 4))
        ttk.Label(frame_star,
                  text="Leave blank for no limit. Decimals OK (e.g. Max Star: 6.5). Star ratings come from "
                       "the same database as ranked status, so a filter here matches nothing when "
                       "scanning a bare folder of files.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", padx=10, pady=(0, 8))

        # --- Mods ---
        frame_mods = ttk.LabelFrame(body, text="6. Classify as if these mods were active")
        frame_mods.pack(fill="x", **pad)
        row_mods = ttk.Frame(frame_mods)
        row_mods.pack(fill="x", padx=10, pady=8)
        self.mod_vars = {}
        for acronym, label in [("DT", "Double Time (1.5x speed)"),
                                ("HR", "Hard Rock (CS x1.3)"),
                                ("HT", "Half Time (0.75x speed)"),
                                ("EZ", "Easy (CS / 2)")]:
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(row_mods, text=label, variable=var).pack(side="left", padx=(0, 16))
            self.mod_vars[acronym] = var
        ttk.Label(frame_mods,
                  text="Leave all unchecked for NM (no mods), which is the baseline. Only speed and "
                       "circle size change what a pattern is - DT makes slower rhythms fast enough to "
                       "count as streams, HR shrinks circles so the same spacing reads as wider. "
                       "(HR's vertical flip doesn't matter here: flipping every object preserves the "
                       "distance between them.)",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", padx=10, pady=(0, 8))

        # --- Advanced thresholds (collapsible-ish via a simple frame) ---
        frame_adv = ttk.LabelFrame(body, text="7. Thresholds (defaults match osu!'s official tag definitions)")
        frame_adv.pack(fill="x", **pad)

        self.param_vars = {}
        params_grid = ttk.Frame(frame_adv)
        params_grid.pack(fill="x", padx=10, pady=8)

        fields = [
            ("max_gap_ms", "Max ms between notes", cm.DEFAULT_PARAMS["max_gap_ms"]),
            ("gap_consistency_tol", "Gap consistency tol.", cm.DEFAULT_PARAMS["gap_consistency_tol"]),
            ("burst_min", "Burst min notes", cm.DEFAULT_PARAMS["burst_min"]),
            ("burst_max", "Burst max notes", cm.DEFAULT_PARAMS["burst_max"]),
            ("stream_min", "Stream min notes", cm.DEFAULT_PARAMS["stream_min"]),
            ("cut_max_multiple", "Max cut gap multiple", cm.DEFAULT_PARAMS["cut_max_multiple"]),
            ("tight_diam_ratio", "Tight spacing ratio", cm.DEFAULT_PARAMS["tight_diam_ratio"]),
            ("spaced_diam_ratio", "Spaced stream ratio", cm.DEFAULT_PARAMS["spaced_diam_ratio"]),
            ("jump_velocity_ratio", "Jump vel. (diam/100ms)", cm.DEFAULT_PARAMS["jump_velocity_ratio"]),
            ("jump_pct_threshold", "Jump %% threshold", cm.DEFAULT_PARAMS["jump_pct_threshold"]),
            ("stream_pct_threshold", "Stream %% threshold", cm.DEFAULT_PARAMS["stream_pct_threshold"]),
            ("jump_min_transitions", "Min notes for jump calc", cm.DEFAULT_PARAMS["jump_min_transitions"]),
            ("jump_gap_cap_ms", "Break cutoff (ms)", cm.DEFAULT_PARAMS["jump_gap_cap_ms"]),
            ("run_wide_fraction_max", "Max wide fraction in run", cm.DEFAULT_PARAMS["run_wide_fraction_max"]),
            ("mean_diam_ratio_max", "Max avg spacing ratio", cm.DEFAULT_PARAMS["mean_diam_ratio_max"]),
        ]
        for i, (key, label, default) in enumerate(fields):
            r, c = divmod(i, 2)
            cell = ttk.Frame(params_grid)
            cell.grid(row=r, column=c, sticky="w", padx=6, pady=3)
            ttk.Label(cell, text=label + ":", width=22).pack(side="left")
            var = tk.StringVar(value=str(default))
            ttk.Entry(cell, textvariable=var, width=8).pack(side="left")
            self.param_vars[key] = var

        ttk.Button(frame_adv, text="Reset to defaults", command=self._reset_defaults).pack(anchor="e", padx=10, pady=(0, 8))

        # --- Run controls ---
        # Pinned below the scroll area: Run, pause/cancel, progress and the
        # log must never be the thing that scrolled off the bottom.
        bottom = ttk.Frame(self)
        bottom.pack(fill="both", side="bottom")

        frame_run = ttk.Frame(bottom)
        frame_run.pack(fill="x", **pad)
        self.run_button = ttk.Button(frame_run, text="Run classification", command=self._start_run)
        self.run_button.pack(side="left")
        self.pause_button = ttk.Button(frame_run, text="Pause", command=self._toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=(6, 0))
        self.cancel_button = ttk.Button(frame_run, text="Cancel", command=self._cancel_run, state="disabled")
        self.cancel_button.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(frame_run, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.progress_label = ttk.Label(frame_run, text="")
        self.progress_label.pack(side="left")
        self.theme_button = ttk.Button(frame_run, text="Light mode", command=self._toggle_theme)
        self.theme_button.pack(side="left", padx=(10, 0))

        # --- Log ---
        frame_log = ttk.LabelFrame(bottom, text="Log")
        frame_log.pack(fill="both", expand=True, **pad)
        log_row = ttk.Frame(frame_log)
        log_row.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text = tk.Text(log_row, height=12, wrap="word", state="disabled")
        log_vsb = ttk.Scrollbar(log_row, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_vsb.set)
        log_vsb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def _pick_folder(self):
        path = filedialog.askdirectory(title="Select your Songs folder (or your lazer osu! data folder)")
        if path:
            self.folder_var.set(path)

    def _pick_lazer_default(self):
        path = cm.default_lazer_data_dir()
        if path and os.path.isdir(path):
            self.folder_var.set(path)
        else:
            messagebox.showinfo("Not found", "Couldn't find the default lazer data folder - use Browse instead.")

    def _pick_export_dir(self):
        path = filedialog.askdirectory(title="Select export folder for collection.db and report.csv")
        if path:
            self.export_dir_var.set(path)

    def _reset_defaults(self):
        for key, var in self.param_vars.items():
            var.set(str(cm.DEFAULT_PARAMS[key]))

    def _log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ------------------------------------------------------------------
    # Run pipeline in a background thread so the GUI stays responsive
    # ------------------------------------------------------------------
    def _start_run(self):
        if self.running:
            return

        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Invalid folder", "Please choose a valid beatmap folder first.")
            return

        try:
            params = {key: (int(var.get()) if key in cm.INT_PARAMS else float(var.get()))
                      for key, var in self.param_vars.items()}
        except ValueError:
            messagebox.showerror("Invalid threshold", "Threshold fields must be numbers.")
            return

        mods = [acronym for acronym, var in self.mod_vars.items() if var.get()] or None

        export_dir = self.export_dir_var.get().strip()
        if not export_dir:
            messagebox.showerror("Invalid export folder", "Please choose an export folder.")
            return
        try:
            os.makedirs(export_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Invalid export folder", f"Couldn't create that folder:\n{e}")
            return

        output = os.path.join(export_dir, "collection.db") if self.write_db_var.get() else None
        csv_path = os.path.join(export_dir, "report.csv") if self.write_csv_var.get() else None

        include_categories = [cat for cat, var in self.category_vars.items() if var.get()]
        if self.write_db_var.get() and not include_categories:
            messagebox.showerror("No categories selected",
                                  "Check at least one category to include in collection.db, "
                                  "or uncheck 'Write collection.db' if you only want the CSV.")
            return
        ranked_mode = self.ranked_mode_var.get()

        min_star_str = self.min_star_var.get().strip()
        max_star_str = self.max_star_var.get().strip()
        try:
            min_star = float(min_star_str) if min_star_str else None
            max_star = float(max_star_str) if max_star_str else None
        except ValueError:
            messagebox.showerror("Invalid star rating", "Min Star / Max Star must be numbers (decimals OK).")
            return
        if min_star is not None and max_star is not None and min_star > max_star:
            messagebox.showerror("Invalid star rating range", "Min Star can't be greater than Max Star.")
            return

        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self.progress.stop()
        self.progress.config(mode="determinate")
        self._indeterminate = False
        self.progress["value"] = 0
        self._last_progress_text = ""
        self.progress_label.config(text="Starting...")
        self.run_button.config(state="disabled")
        self.pause_button.config(state="normal", text="Pause")
        self.cancel_button.config(state="normal")
        self.running = True
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()  # start unpaused

        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(folder, output, csv_path, params, self.cancel_event, self.pause_event,
                  include_categories, ranked_mode, min_star, max_star, mods),
            daemon=True,
        )
        self.worker_thread.start()

    def _cancel_run(self):
        if self.running and self.cancel_event is not None:
            self.cancel_event.set()
            if self.pause_event is not None:
                self.pause_event.set()  # release a pause so cancel takes effect immediately
            self.cancel_button.config(state="disabled")
            self.pause_button.config(state="disabled")
            self.progress_label.config(text="Cancelling...")

    def _toggle_pause(self):
        if not self.running or self.pause_event is None:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.config(text="Resume")
            self.progress_label.config(text="Paused")
            self._log("Paused - click Resume to continue. (A phase that's already in "
                      "progress, like the directory walk, finishes its current step first.)")
        else:
            self.pause_event.set()
            self.pause_button.config(text="Pause")
            # Restore the label immediately rather than leaving it reading
            # "Paused". Some phases (the directory walk, the realm-reader
            # subprocess, CSV writing) emit no progress callbacks at all, so
            # without this the UI can sit on a stale "Paused" with a frozen
            # progress bar for minutes after resuming - indistinguishable
            # from resume simply not working.
            self.progress_label.config(text=self._last_progress_text or "Working...")
            self._log("Resumed.")

    def _worker(self, folder, output, csv_path, params, cancel_event, pause_event,
                include_categories, ranked_mode, min_star, max_star, mods):
        def progress_cb(done, total):
            self.msg_queue.put(("progress", done, total))

        def log_cb(msg):
            self.msg_queue.put(("log", msg))

        try:
            result = cm.run_pipeline(
                folder, output=output, csv_path=csv_path, write_db=output is not None,
                params=params, progress_cb=progress_cb, log_cb=log_cb, cancel_event=cancel_event,
                pause_event=pause_event, include_categories=include_categories, ranked_mode=ranked_mode,
                min_star=min_star, max_star=max_star, mods=mods,
            )
            self.msg_queue.put(("done", result))
        except cm.ScanCancelled:
            self.msg_queue.put(("cancelled", None))
        except Exception:
            self.msg_queue.put(("error", traceback.format_exc()))

    def _poll_queue(self):
        if self._closing:
            return
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total = item
                    if total is None:
                        # Indeterminate phase: the directory walk, where the
                        # total isn't known yet. Pulse the bar and count
                        # folders so it's visibly alive - a stuck bar during
                        # a three-minute walk reads as a crash.
                        if not self._indeterminate:
                            self._indeterminate = True
                            self.progress.config(mode="indeterminate")
                            self.progress.start(15)
                        self._last_progress_text = (f"searching {done} folders..."
                                                     if done else "searching folders...")
                        if self.pause_event is None or self.pause_event.is_set():
                            self.progress_label.config(text=self._last_progress_text)
                    elif total:
                        if self._indeterminate:
                            self._indeterminate = False
                            self.progress.stop()
                            self.progress.config(mode="determinate")
                        self.progress["maximum"] = total
                        self.progress["value"] = done
                        self._last_progress_text = f"{done}/{total} files"
                        # Don't clobber the "Paused" label with a stale
                        # progress tick that was already queued when the
                        # pause took effect.
                        if self.pause_event is None or self.pause_event.is_set():
                            self.progress_label.config(text=self._last_progress_text)
                elif kind == "log":
                    self._log(item[1])
                elif kind == "done":
                    self._log("\nDone.")
                    if self.write_db_var.get():
                        self._log("Note: each diff is classified by its DOMINANT pattern - a stream map with a jump section is still a stream map. Jumps vs. Bursts is decided by which one actually covers more of the map, not just whether a burst run exists at all.")
                        self._log("Back up your existing collection.db before replacing it, or merge with a tool like Piotrekol's CollectionManager.")
                    self.run_button.config(state="normal")
                    self.pause_button.config(state="disabled", text="Pause")
                    self.cancel_button.config(state="disabled")
                    self.progress_label.config(text="")
                    self.running = False
                elif kind == "cancelled":
                    self._log("\nCancelled - no CSV or collection.db was written for this run.")
                    self.run_button.config(state="normal")
                    self.pause_button.config(state="disabled", text="Pause")
                    self.cancel_button.config(state="disabled")
                    self.progress_label.config(text="Cancelled")
                    self.running = False
                elif kind == "error":
                    self._log("\nERROR:\n" + item[1])
                    messagebox.showerror("Error", "Something went wrong - see the log for details.")
                    self.run_button.config(state="normal")
                    self.pause_button.config(state="disabled", text="Pause")
                    self.cancel_button.config(state="disabled")
                    self.running = False
        except queue.Empty:
            pass
        if not self._closing:
            self._poll_job = self.after(100, self._poll_queue)


def main():
    app = ClassifierGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
