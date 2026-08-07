#!/usr/bin/env python3
"""
osu! Burst/Stream/Jump Classifier - GUI
-----------------------------------------
A user-friendly front end for classify_maps.py. Works with:
  - osu!stable: point it at your Songs/ folder directly
  - osu!lazer: export your library with BeatmapExporter first
    (https://github.com/kabiiQ/BeatmapExporter), then point this at
    whatever folder you exported to. .osz files are read natively -
    no need to extract them.

Pure standard library (tkinter) - nothing to pip install.
"""

import os
import sys
import threading
import queue
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import classify_maps as cm


class ClassifierGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("osu-beatmap-categorizer")
        self.geometry("1200x800")
        self.minsize(900, 650)

        self.msg_queue = queue.Queue()
        self.worker_thread = None
        self.running = False
        self.cancel_event = None

        self._build_widgets()
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_widgets(self):
        pad = {"padx": 10, "pady": 6}

        # --- Input folder ---
        frame_in = ttk.LabelFrame(self, text="1. Beatmap folder")
        frame_in.pack(fill="x", **pad)

        self.folder_var = tk.StringVar()
        ttk.Entry(frame_in, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=(10, 5), pady=8)
        ttk.Button(frame_in, text="Browse...", command=self._pick_folder).pack(side="left", padx=(0, 5), pady=8)

        lazer_default = cm.default_lazer_data_dir()
        if lazer_default and os.path.isdir(lazer_default):
            ttk.Button(frame_in, text="Use lazer data folder", command=self._pick_lazer_default).pack(
                side="left", padx=(0, 10), pady=8)

        hint = ttk.Label(
            frame_in,
            text="stable: your Songs folder.\n"
                 "lazer: point at your osu! data folder (e.g. %appdata%\\osu on Windows, containing "
                 "client.realm and files/) - uses a fast direct-read path if available, or falls back to "
                 "scanning files/ directly otherwise. No export needed either way.\n"
                 "You can also point it at a BeatmapExporter export folder if you'd rather work from that; "
                 ".osz files are read directly either way.",
            foreground="#666666", justify="left", wraplength=680,
        )
        hint.pack(fill="x", padx=10, pady=(0, 8))

        # --- Output ---
        frame_out = ttk.LabelFrame(self, text="2. Export folder")
        frame_out.pack(fill="x", **pad)

        row0 = ttk.Frame(frame_out)
        row0.pack(fill="x", padx=10, pady=(8, 4))
        self.export_dir_var = tk.StringVar(value=os.path.join(os.getcwd(), "osu-categorizer-export"))
        ttk.Entry(row0, textvariable=self.export_dir_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row0, text="Choose...", command=self._pick_export_dir).pack(side="left")

        ttk.Label(frame_out, text="collection.db and report.csv will be written into this folder.",
                  foreground="#666666").pack(anchor="w", padx=10)

        row1 = ttk.Frame(frame_out)
        row1.pack(fill="x", padx=10, pady=(6, 8))
        self.write_db_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="Write collection.db", variable=self.write_db_var).pack(side="left", padx=(0, 20))
        self.write_csv_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="Write CSV report (recommended - audit before trusting the collection.db)",
                         variable=self.write_csv_var).pack(side="left")

        # --- Category selection ---
        frame_cat = ttk.LabelFrame(self, text="3. Categories to include in collection.db")
        frame_cat.pack(fill="x", **pad)
        row_cat = ttk.Frame(frame_cat)
        row_cat.pack(fill="x", padx=10, pady=8)
        self.category_vars = {}
        for cat in ["Streams", "Bursts", "Jumps", "Misc"]:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(row_cat, text=cat, variable=var).pack(side="left", padx=(0, 16))
            self.category_vars[cat] = var
        ttk.Label(frame_cat,
                  text="Uncheck what you don't want - e.g. an aim-only player could keep just Jumps checked.",
                  foreground="#666666").pack(anchor="w", padx=10, pady=(0, 8))

        # --- Ranked status handling ---
        frame_ranked = ttk.LabelFrame(self, text="4. Ranked status (osu!lazer fast path only)")
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
                  text="Ranked status is only known when scanning via osu!lazer's realm fast path. "
                       "Other scan methods (Songs folder, files/ folder, .osz) can't tell ranked from unranked.",
                  foreground="#666666", wraplength=680, justify="left").pack(anchor="w", padx=10, pady=(0, 8))

        # --- Advanced thresholds (collapsible-ish via a simple frame) ---
        frame_adv = ttk.LabelFrame(self, text="5. Thresholds (defaults match osu!'s official tag definitions)")
        frame_adv.pack(fill="x", **pad)

        self.param_vars = {}
        params_grid = ttk.Frame(frame_adv)
        params_grid.pack(fill="x", padx=10, pady=8)

        fields = [
            ("burst_min", "Burst min notes", cm.DEFAULT_PARAMS["burst_min"]),
            ("burst_max", "Burst max notes", cm.DEFAULT_PARAMS["burst_max"]),
            ("stream_min", "Stream min notes", cm.DEFAULT_PARAMS["stream_min"]),
            ("snap_ratio", "Snap speed ratio", cm.DEFAULT_PARAMS["snap_ratio"]),
            ("tight_diam_ratio", "Tight spacing ratio", cm.DEFAULT_PARAMS["tight_diam_ratio"]),
            ("jump_velocity_ratio", "Jump velocity ratio", cm.DEFAULT_PARAMS["jump_velocity_ratio"]),
            ("jump_pct_threshold", "Jump %% threshold", cm.DEFAULT_PARAMS["jump_pct_threshold"]),
            ("run_wide_fraction_max", "Max wide fraction in run", cm.DEFAULT_PARAMS["run_wide_fraction_max"]),
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
        frame_run = ttk.Frame(self)
        frame_run.pack(fill="x", **pad)
        self.run_button = ttk.Button(frame_run, text="Run classification", command=self._start_run)
        self.run_button.pack(side="left")
        self.cancel_button = ttk.Button(frame_run, text="Cancel", command=self._cancel_run, state="disabled")
        self.cancel_button.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(frame_run, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.progress_label = ttk.Label(frame_run, text="")
        self.progress_label.pack(side="left")

        # --- Log ---
        frame_log = ttk.LabelFrame(self, text="Log")
        frame_log.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(frame_log, height=12, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

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
            params = {key: (int(var.get()) if key in ("burst_min", "burst_max", "stream_min")
                             else float(var.get())) for key, var in self.param_vars.items()}
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

        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self.progress["value"] = 0
        self.progress_label.config(text="Starting...")
        self.run_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        self.running = True
        self.cancel_event = threading.Event()

        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(folder, output, csv_path, params, self.cancel_event, include_categories, ranked_mode),
            daemon=True,
        )
        self.worker_thread.start()

    def _cancel_run(self):
        if self.running and self.cancel_event is not None:
            self.cancel_event.set()
            self.cancel_button.config(state="disabled")
            self.progress_label.config(text="Cancelling...")

    def _worker(self, folder, output, csv_path, params, cancel_event, include_categories, ranked_mode):
        def progress_cb(done, total):
            self.msg_queue.put(("progress", done, total))

        def log_cb(msg):
            self.msg_queue.put(("log", msg))

        try:
            result = cm.run_pipeline(
                folder, output=output, csv_path=csv_path, write_db=output is not None,
                params=params, progress_cb=progress_cb, log_cb=log_cb, cancel_event=cancel_event,
                include_categories=include_categories, ranked_mode=ranked_mode,
            )
            self.msg_queue.put(("done", result))
        except cm.ScanCancelled:
            self.msg_queue.put(("cancelled", None))
        except Exception:
            self.msg_queue.put(("error", traceback.format_exc()))

    def _poll_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total = item
                    if total:
                        self.progress["maximum"] = total
                        self.progress["value"] = done
                        self.progress_label.config(text=f"{done}/{total} files")
                elif kind == "log":
                    self._log(item[1])
                elif kind == "done":
                    self._log("\nDone.")
                    if self.write_db_var.get():
                        self._log("Note: each diff is classified by its DOMINANT pattern (Streams > Bursts > Jumps > Misc) - a stream map with a jump section is still a stream map, not split across collections.")
                        self._log("Back up your existing collection.db before replacing it, or merge with a tool like Piotrekol's CollectionManager.")
                    self.run_button.config(state="normal")
                    self.cancel_button.config(state="disabled")
                    self.progress_label.config(text="")
                    self.running = False
                elif kind == "cancelled":
                    self._log("\nCancelled - no CSV or collection.db was written for this run.")
                    self.run_button.config(state="normal")
                    self.cancel_button.config(state="disabled")
                    self.progress_label.config(text="Cancelled")
                    self.running = False
                elif kind == "error":
                    self._log("\nERROR:\n" + item[1])
                    messagebox.showerror("Error", "Something went wrong - see the log for details.")
                    self.run_button.config(state="normal")
                    self.cancel_button.config(state="disabled")
                    self.running = False
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


def main():
    app = ClassifierGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
