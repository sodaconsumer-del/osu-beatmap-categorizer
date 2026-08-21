#!/usr/bin/env python3
"""
osu! Burst/Stream/Jump Classifier - GUI
-----------------------------------------
A user-friendly front end for classify_maps.py. Works with:
  - osu!stable: point it at your osu! install folder (the one osu!.db and
    Songs/ both live in - not Songs/ itself). Reads osu!.db directly, which
    skips the directory walk entirely and brings ranked status, star ratings
    and beatmap ids along with it.
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
            text="stable: your osu! install folder (the one with osu!.db and Songs/ in it, not "
                 "Songs/ itself) - reads osu!.db directly for a fast scan, or falls back to walking "
                 "Songs/ if it can't.\n"
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
        # Driven off cm.CATEGORIES rather than a second copy of the list -
        # adding a category (Hybrid was the last one) should not need a second
        # edit here that is easy to forget.
        for cat in cm.CATEGORIES:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(row_cat, text=cat, variable=var).pack(side="left", padx=(0, 16))
            self.category_vars[cat] = var
        ttk.Label(frame_cat,
                  text="Uncheck what you don't want - e.g. an aim-only player could keep just Jumps checked.",
                  style="Muted.TLabel").pack(anchor="w", padx=10, pady=(0, 2))

        row_cj = ttk.Frame(frame_cat)
        row_cj.pack(fill="x", padx=10, pady=(4, 2))
        self.combine_jumps_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_cj, text='Also add one combined "Jumps" collection',
                         variable=self.combine_jumps_var).pack(side="left")
        ttk.Label(frame_cat,
                  text='Writes an extra "Jumps" collection holding every jump map, on top of the two '
                       "above rather than instead of them - so a map with jumps and bursts shows up in "
                       '"Jumps with bursts" AND in "Jumps". Nothing is reclassified and report.csv still '
                       "records the specific category; this only changes how the collections are grouped.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", padx=10, pady=(0, 8))

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
        # --- Detection sensitivity + advanced thresholds ---
        # Nineteen bare numbers with names like "mean_diam_ratio_max" told a
        # user nothing about what they would do. The common case is now one
        # click; every individual knob is still here, just folded away behind
        # a disclosure and given a plain-language name with a one-line "what
        # does this number actually mean" underneath.
        frame_adv = ttk.LabelFrame(body, text="7. Detection sensitivity")
        frame_adv.pack(fill="x", **pad)

        self.sensitivity_var = tk.StringVar(value=cm.DEFAULT_SENSITIVITY)
        row_sens = ttk.Frame(frame_adv)
        row_sens.pack(fill="x", padx=10, pady=(8, 0))
        for name, blurb in [
            ("Stricter", "only clear-cut bursts and streams"),
            ("Balanced", "recommended - matches osu!'s own tags"),
            ("Looser", "also catches short or borderline patterns"),
        ]:
            ttk.Radiobutton(row_sens, text="%s  (%s)" % (name, blurb),
                            variable=self.sensitivity_var, value=name,
                            command=self._apply_sensitivity).pack(anchor="w")
        self.custom_sens_label = ttk.Label(
            frame_adv, text="", style="Muted.TLabel", wraplength=680, justify="left")
        self.custom_sens_label.pack(anchor="w", padx=10, pady=(2, 0))

        ttk.Label(frame_adv,
                  text="Balanced is the measured default. Stricter and Looser are deliberate trades "
                       "for wanting fewer or more maps flagged - they are not better guesses, and "
                       "they only move four of the settings below.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(
            anchor="w", padx=10, pady=(2, 6))

        self.adv_open = False
        self.adv_toggle = ttk.Button(
            frame_adv, text="\u25b6  Advanced: fine-tune individual thresholds",
            command=self._toggle_advanced)
        self.adv_toggle.pack(anchor="w", padx=10, pady=(0, 6))

        # Packed and unpacked by _toggle_advanced - built now either way so
        # the param vars exist before a run can read them.
        self.adv_body = ttk.Frame(frame_adv)

        self.param_vars = {}
        # (key, label, what-the-number-means). Grouped so related settings read
        # together instead of arriving in whatever order they were added.
        sections = [
            ('Speed and rhythm - what counts as "fast"', [
                ("max_gap_ms", "Fastest gap that still counts as one run",
                 "milliseconds between notes. 140 is roughly a 107 BPM stream; lower means only "
                 "faster tapping counts."),
                ("gap_consistency_tol", "How much a run may change speed before it splits",
                 "0.18 = each gap may drift 18% from the run's own average. A real stream does not "
                 "change tapping speed halfway through."),
                ("burst_beat_fraction_max", "Slowest snap that still counts as a burst",
                 "as a fraction of one beat per note. 0.4 accepts 1/4 and 1/3 snap and rejects 1/2, "
                 "which is what stops a fast map's ordinary tapping from reading as bursts."),
                ("burst_max_gap_ms", "Slowest tapping that can still be a burst",
                 "milliseconds per note. 105 is about a 143 BPM stream. A slow song's honest 1/4 "
                 "(120ms at 125 BPM) is a real 1/4 and still not a burst - it isn't fast enough. "
                 "Streams are not affected by this."),
                ("max_plausible_bpm", "Above this BPM, read the tempo as doubled",
                 "300. Some songs are written at double their real tempo (360 for a 180 BPM song), "
                 "which makes real 1/4 bursts look like ordinary 1/2 tapping. Taken from the timing "
                 "points rather than the notes so every difficulty in a mapset agrees."),
                ("doubled_half_share_min", "When 1/2 is this much of a map, read it as doubled",
                 "0.25 = 25% of note gaps. The mirror of the halved check: some songs are written at "
                 "double their real tempo (360 for a 180 BPM song), which makes genuine 1/4 bursts "
                 "look like ordinary 1/2 tapping. The giveaway is that the notated 1/4 is missing "
                 "entirely - it would be a real 1/8."),
                ("halved_quarter_share_min", "When 1/4 is this much of a map, read it as halved",
                 "0.15 = 15% of note gaps. Some songs are written at half their real tempo (130 for a "
                 "260 BPM song), which makes ordinary 1/2 tapping look like 1/4. The notes give it "
                 "away: a real map uses 1/4 for bursts only, so a 1/4 layer this large - and too slow "
                 "to be a burst - is really the 1/2 backbone."),
            ]),
            ("Run length - burst vs stream", [
                ("burst_min", "Shortest run that counts as a burst",
                 "notes. 3 means a triple counts."),
                ("burst_max", "Longest run still called a burst",
                 "notes. Anything above this is a stream."),
                ("stream_min", "Shortest run that counts as a stream", "notes."),
            ]),
            ("Spacing - burst/stream vs jump", [
                ("tight_diam_ratio", 'How close is "stacked"',
                 "hit-circle diameters. Notes this close are overlapping or nearly so."),
                ("spaced_diam_ratio", 'How far apart is still "a stream"',
                 "hit-circle diameters. Wider than this and a transition is jump-spaced."),
                ("run_wide_fraction_max", "How much of a run may be jump-spaced",
                 "0.4 = up to 40% of its notes, before the whole run is called a jump pattern."),
                ("mean_diam_ratio_max", "Average spacing limit across a whole run",
                 "hit-circle diameters. Catches jump patterns that dodge the check above by chance."),
                ("jump_velocity_ratio", "How fast the cursor must travel to count as a jump",
                 "hit-circle diameters per 100ms, required on top of the spacing test above."),
            ]),
            ("How much of a map a pattern must cover to own it", [
                ("jump_pct_threshold", "Share of a map that must be jumps",
                 "% of note-to-note transitions."),
                ("stream_pct_threshold", "Share of a map that must be streams",
                 "% of notes. Stops one short run in a long jump map from claiming the whole map."),
                ("jump_min_transitions", "Fewest notes before the jump share means anything",
                 'a 30-note map being "20% jumps" is noise, not a finding.'),
                ("jump_gap_cap_ms", "Gap that counts as a break rather than gameplay",
                 "milliseconds. Breaks are left out of the percentages entirely."),
            ]),
            ("Sections - where in the map a pattern lives", [
                ("section_ms", "Length of one section",
                 "milliseconds. About two bars at 200 BPM. Sections are how the Hybrid category "
                 "tells 'jumps then streams' apart from 'both mixed evenly throughout' - coverage "
                 "alone averages those to the same numbers."),
                ("section_dominance", "How much of a section a pattern must hold to own it",
                 "0.5 = half its notes. A section is owned by at most one pattern."),
                ("hybrid_section_min", "Sections each side needs for \"Hybrid\"",
                 "0.15 = streams must own 15% of the map's sections and jumps another 15%, before "
                 "the map is called a mix of the two rather than one or the other."),
                ("hybrid_balance_min", "How balanced that mix must be",
                 "0.5 = the smaller side must own at least half as many sections as the larger. "
                 "Without it, a map with 61% jump sections and 19% stream sections counts as a "
                 "\"mix\" when it is plainly a jump map with a stream section in it."),
                ("section_min_transitions", "Fewest notes for a section to count at all",
                 "stops a map's sparse tail, or a couple of notes either side of a break, "
                 "registering as full sections and skewing the proportions."),
            ]),
            ("Cut streams - a stream with a skipped beat", [
                ("cut_max_multiple", "Biggest skipped-beat gap still inside one stream",
                 "as a multiple of the run's own note gap. 3 allows up to two missing notes."),
                ("cut_max_dist_ratio", "How far a skipped beat may travel",
                 "hit-circle diameters. Beyond this it is a real jump between two runs, not a skip."),
            ]),
        ]

        for title, fields in sections:
            ttk.Label(self.adv_body, text=title).pack(anchor="w", padx=10, pady=(8, 2))
            for key, label, meaning in fields:
                cell = ttk.Frame(self.adv_body)
                cell.pack(fill="x", padx=22, pady=(2, 0))
                ttk.Label(cell, text=label + ":").pack(side="left")
                var = tk.StringVar(value=str(cm.DEFAULT_PARAMS[key]))
                var.trace_add("write", self._on_param_edited)
                ttk.Entry(cell, textvariable=var, width=8).pack(side="left", padx=(6, 0))
                self.param_vars[key] = var
                ttk.Label(self.adv_body, text=meaning, style="Muted.TLabel",
                          wraplength=620, justify="left").pack(anchor="w", padx=34, pady=(0, 2))

        ttk.Button(self.adv_body, text="Reset to defaults",
                   command=self._reset_defaults).pack(anchor="e", padx=10, pady=(8, 8))

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
        path = filedialog.askdirectory(title="Select your osu! install folder (stable) or data folder (lazer)")
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

    def _toggle_advanced(self):
        """
        Show/hide the per-threshold panel. Collapsed by default - it is the
        part most users never need, and having all nineteen numbers on screen
        at once was what made the settings look impenetrable.
        """
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv_body.pack(fill="x")
            self.adv_toggle.config(text="▼  Advanced: fine-tune individual thresholds")
        else:
            self.adv_body.pack_forget()
            self.adv_toggle.config(text="▶  Advanced: fine-tune individual thresholds")

    def _apply_sensitivity(self):
        """
        Push the chosen preset into the individual threshold fields, so the
        Advanced panel always shows what is actually going to run rather than
        stale numbers the preset has since overridden. The run path reads
        those fields, so this is the only place the preset takes effect -
        there is no second source of truth to drift.
        """
        params = cm.params_for_sensitivity(self.sensitivity_var.get())
        # Setting the vars fires their write traces; without this guard each
        # one would re-run the Custom check mid-update and briefly see a
        # half-applied preset.
        self._suspend_param_watch = True
        try:
            for key, var in self.param_vars.items():
                var.set(str(params[key]))
        finally:
            self._suspend_param_watch = False
        self._refresh_sensitivity_note()

    def _on_param_edited(self, *_):
        """
        A hand-edited threshold means no preset describes the settings any
        more. Say so, rather than leaving a radio button selected that is now
        a lie about what will run.
        """
        if getattr(self, "_suspend_param_watch", False):
            return
        self._refresh_sensitivity_note()

    def _current_params_or_none(self):
        """
        Parsed threshold values, or None if any field isn't a number yet -
        which is normal mid-typing, when a field is briefly empty or "1.".
        Only the Custom indicator uses this; the run path does its own
        parsing and reports bad input properly rather than ignoring it.
        """
        out = {}
        for key, var in self.param_vars.items():
            try:
                out[key] = int(var.get()) if key in cm.INT_PARAMS else float(var.get())
            except ValueError:
                return None
        return out

    def _refresh_sensitivity_note(self):
        params = self._current_params_or_none()
        if params is None:
            return
        match = cm.sensitivity_of(params)
        if match is None:
            self.custom_sens_label.config(
                text="Custom - the thresholds below no longer match any preset. Pick a preset "
                     "above to overwrite them, or use Reset to defaults.")
        else:
            self.custom_sens_label.config(text="")
            # Editing a value back to a preset's numbers should re-select that
            # preset, not leave "Custom" showing something untrue.
            if self.sensitivity_var.get() != match:
                self.sensitivity_var.set(match)

    def _reset_defaults(self):
        self.sensitivity_var.set(cm.DEFAULT_SENSITIVITY)
        self._apply_sensitivity()

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
        self._stop_progress_bar()
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
                  include_categories, ranked_mode, min_star, max_star,
                  self.combine_jumps_var.get()),
            daemon=True,
        )
        self.worker_thread.start()

    def _stop_progress_bar(self):
        """
        Resets the bar to a clean determinate state, regardless of what mode
        it was in. Bug: previously only _start_run did this - the done/
        cancelled/error handlers never did, so a cancel or crash landing
        during the walk's indeterminate (pulsing) phase left the bar
        animating forever under a "Cancelled"/error message until the next
        run started and reset it. .stop() on an already-stopped bar is a
        harmless no-op, so this is safe to call unconditionally.
        """
        self.progress.stop()
        self.progress.config(mode="determinate")
        self._indeterminate = False

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
                include_categories, ranked_mode, min_star, max_star, combine_jumps):
        def progress_cb(done, total):
            self.msg_queue.put(("progress", done, total))

        def log_cb(msg):
            self.msg_queue.put(("log", msg))

        try:
            result = cm.run_pipeline(
                folder, output=output, csv_path=csv_path, write_db=output is not None,
                params=params, progress_cb=progress_cb, log_cb=log_cb, cancel_event=cancel_event,
                pause_event=pause_event, include_categories=include_categories, ranked_mode=ranked_mode,
                min_star=min_star, max_star=max_star, combine_jumps=combine_jumps,
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
                            self._stop_progress_bar()
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
                    self._stop_progress_bar()
                    self.progress_label.config(text="")
                    self.running = False
                elif kind == "cancelled":
                    self._log("\nCancelled - no CSV or collection.db was written for this run.")
                    self.run_button.config(state="normal")
                    self.pause_button.config(state="disabled", text="Pause")
                    self.cancel_button.config(state="disabled")
                    self._stop_progress_bar()
                    self.progress_label.config(text="Cancelled")
                    self.running = False
                elif kind == "error":
                    self._log("\nERROR:\n" + item[1])
                    messagebox.showerror("Error", "Something went wrong - see the log for details.")
                    self.run_button.config(state="normal")
                    self.pause_button.config(state="disabled", text="Pause")
                    self.cancel_button.config(state="disabled")
                    self._stop_progress_bar()
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
