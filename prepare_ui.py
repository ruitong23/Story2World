"""Desktop UI for preparing all files required by the novel simulation."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from app_files import (
    APP_DIR,
    PREPARATION_OUTPUTS,
    file_status,
    load_settings,
    save_settings,
)


class PreparationApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NavelMaker 2 - Simulation Preparation")
        self.root.geometry("1040x760")
        self.root.minsize(900, 650)

        self.events = queue.Queue()
        self.process = None
        self.stop_requested = False
        self.started_at = None
        self.last_percent = 0.0

        self.novel_path = tk.StringVar()
        self.percent = tk.DoubleVar(value=100.0)
        self.percent_entry = tk.StringVar(value="100")
        self.chunk_size = tk.StringVar(value="2000")
        self.overlap = tk.StringVar(value="300")
        saved = load_settings()
        self.base_url = tk.StringVar(
            value=os.getenv("NOVEL_LLM_BASE_URL", saved["llm_base_url"])
        )
        self.model = tk.StringVar(
            value=os.getenv("NOVEL_LLM_MODEL", saved["llm_model"])
        )
        self.api_key = tk.StringVar(
            value=os.getenv("NOVEL_LLM_API_KEY", saved["llm_api_key"])
        )
        self.stage_text = tk.StringVar(value="Ready")
        self.eta_text = tk.StringVar(value="ETA: --")
        self.progress_value = tk.DoubleVar(value=0.0)

        self._build_ui()
        self.refresh_file_checks()
        self.root.protocol("WM_DELETE_WINDOW", self.close_window)
        self.root.after(100, self._drain_events)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        source = ttk.LabelFrame(outer, text="Source and processing scope", padding=10)
        source.grid(row=0, column=0, sticky="ew")
        source.columnconfigure(1, weight=1)

        ttk.Label(source, text="Novel TXT file").grid(row=0, column=0, sticky="w")
        ttk.Entry(source, textvariable=self.novel_path).grid(
            row=0, column=1, padx=8, sticky="ew"
        )
        ttk.Button(source, text="Browse...", command=self.choose_novel).grid(
            row=0, column=2
        )

        ttk.Label(source, text="Story percentage").grid(
            row=1, column=0, pady=(10, 0), sticky="w"
        )
        slider = ttk.Scale(
            source,
            from_=1,
            to=100,
            variable=self.percent,
            command=self._slider_changed,
        )
        slider.grid(row=1, column=1, padx=8, pady=(10, 0), sticky="ew")
        percentage_entry = ttk.Entry(
            source, textvariable=self.percent_entry, width=8
        )
        percentage_entry.grid(row=1, column=2, pady=(10, 0), sticky="w")
        percentage_entry.bind("<Return>", self._entry_changed)
        percentage_entry.bind("<FocusOut>", self._entry_changed)

        sizing = ttk.Frame(source)
        sizing.grid(row=2, column=0, columnspan=3, pady=(10, 0), sticky="ew")
        ttk.Label(sizing, text="Chunk size").pack(side="left")
        ttk.Entry(sizing, textvariable=self.chunk_size, width=9).pack(
            side="left", padx=(6, 18)
        )
        ttk.Label(sizing, text="Overlap").pack(side="left")
        ttk.Entry(sizing, textvariable=self.overlap, width=9).pack(
            side="left", padx=6
        )
        ttk.Label(
            sizing,
            text="100% processes the full story. Smaller values process the opening portion.",
        ).pack(side="left", padx=16)

        llm = ttk.LabelFrame(outer, text="OpenAI-compatible local LLM", padding=10)
        llm.grid(row=1, column=0, pady=10, sticky="ew")
        llm.columnconfigure(1, weight=1)
        ttk.Label(llm, text="Base URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(llm, textvariable=self.base_url).grid(
            row=0, column=1, padx=8, sticky="ew"
        )
        ttk.Label(llm, text="Model").grid(row=1, column=0, pady=(8, 0), sticky="w")
        ttk.Entry(llm, textvariable=self.model).grid(
            row=1, column=1, padx=8, pady=(8, 0), sticky="ew"
        )
        ttk.Label(llm, text="API key").grid(row=2, column=0, pady=(8, 0), sticky="w")
        ttk.Entry(llm, textvariable=self.api_key, show="*").grid(
            row=2, column=1, padx=8, pady=(8, 0), sticky="ew"
        )
        ttk.Button(llm, text="Check server", command=self.check_server).grid(
            row=0, column=2, rowspan=3, padx=(8, 0)
        )

        status = ttk.LabelFrame(outer, text="Preparation progress", padding=10)
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Progressbar(
            status,
            variable=self.progress_value,
            maximum=100,
            mode="determinate",
        ).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(status, textvariable=self.stage_text).grid(
            row=1, column=0, pady=(6, 0), sticky="w"
        )
        ttk.Label(status, textvariable=self.eta_text).grid(
            row=1, column=1, pady=(6, 0), sticky="e"
        )

        lower = ttk.Panedwindow(outer, orient="horizontal")
        lower.grid(row=3, column=0, pady=(10, 0), sticky="nsew")

        checks_frame = ttk.LabelFrame(lower, text="Required output files", padding=8)
        log_frame = ttk.LabelFrame(lower, text="Preparation log", padding=8)
        lower.add(checks_frame, weight=2)
        lower.add(log_frame, weight=3)

        self.checks = ttk.Treeview(
            checks_frame,
            columns=("status", "description"),
            show="headings",
            height=14,
        )
        self.checks.heading("status", text="Status")
        self.checks.heading("description", text="File / purpose")
        self.checks.column("status", width=85, anchor="center")
        self.checks.column("description", width=340)
        self.checks.pack(fill="both", expand=True)

        self.log = tk.Text(log_frame, wrap="word", state="disabled", font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, pady=(10, 0), sticky="ew")
        self.start_button = ttk.Button(
            actions, text="Start preparation", command=self.start_preparation
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            actions,
            text="Stop preparation",
            command=self.stop_preparation,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Refresh file checks", command=self.refresh_file_checks).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="Open output folder", command=self.open_folder).pack(
            side="left"
        )

    def choose_novel(self):
        path = filedialog.askopenfilename(
            title="Select novel text file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.novel_path.set(path)

    def _slider_changed(self, value):
        self.percent_entry.set(str(round(float(value), 1)).rstrip("0").rstrip("."))

    def _entry_changed(self, _event=None):
        try:
            value = max(1.0, min(100.0, float(self.percent_entry.get())))
        except ValueError:
            value = self.percent.get()
        self.percent.set(value)
        self.percent_entry.set(str(round(value, 1)).rstrip("0").rstrip("."))

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_file_checks(self):
        self.checks.delete(*self.checks.get_children())
        for item in file_status(PREPARATION_OUTPUTS):
            status = "Made" if item["exists"] else "Needed"
            self.checks.insert(
                "",
                "end",
                values=(status, f"{item['name']} - {item['description']}"),
                tags=("made" if item["exists"] else "needed",),
            )
        self.checks.tag_configure("made", foreground="#137333")
        self.checks.tag_configure("needed", foreground="#b06000")

    def check_server(self):
        def worker():
            try:
                url = self.base_url.get().rstrip("/") + "/models"
                request = urllib.request.Request(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key.get()}"},
                )
                with urllib.request.urlopen(request, timeout=8) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                models = [item.get("id", "") for item in payload.get("data", [])]
                selected = self.model.get()
                detail = (
                    f"Server online. Selected model found: {selected}"
                    if selected in models
                    else f"Server online, but '{selected}' was not listed."
                )
                self.events.put(("dialog", ("LLM server", detail, "info")))
            except Exception as error:
                self.events.put(
                    ("dialog", ("LLM server", f"Server check failed:\n{error}", "error"))
                )

        threading.Thread(target=worker, daemon=True).start()

    def _validated_settings(self):
        novel = Path(self.novel_path.get().strip())
        if not novel.is_file():
            raise ValueError("Select an existing novel TXT file.")
        self._entry_changed()
        percent = float(self.percent.get())
        chunk_size = int(self.chunk_size.get())
        overlap = int(self.overlap.get())
        if chunk_size <= 0:
            raise ValueError("Chunk size must be positive.")
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError("Overlap must be at least 0 and smaller than chunk size.")
        if not self.base_url.get().strip() or not self.model.get().strip():
            raise ValueError("LLM base URL and model are required.")
        return novel.resolve(), percent, chunk_size, overlap

    def start_preparation(self):
        if self.process and self.process.poll() is None:
            return
        try:
            novel, percent, chunk_size, overlap = self._validated_settings()
        except Exception as error:
            messagebox.showerror("Invalid settings", str(error))
            return

        command = [
            sys.executable,
            "-u",
            str(APP_DIR / "pipeline_program.py"),
            "--novel",
            str(novel),
            "--percent",
            str(percent),
            "--chunk-size",
            str(chunk_size),
            "--overlap",
            str(overlap),
        ]
        env = os.environ.copy()
        env["NOVEL_LLM_BASE_URL"] = self.base_url.get().strip()
        env["NOVEL_LLM_MODEL"] = self.model.get().strip()
        env["NOVEL_LLM_API_KEY"] = self.api_key.get().strip() or "lm-studio"
        self.api_key.set(env["NOVEL_LLM_API_KEY"])
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        env.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
        save_settings(
            {
                "llm_base_url": env["NOVEL_LLM_BASE_URL"],
                "llm_model": env["NOVEL_LLM_MODEL"],
                "llm_api_key": env["NOVEL_LLM_API_KEY"],
            }
        )

        self.progress_value.set(0)
        self.stage_text.set("Starting preparation...")
        self.eta_text.set("ETA: calculating")
        self.started_at = time.monotonic()
        self.last_percent = 0.0
        self.stop_requested = False
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.append_log("=" * 70)
        self.append_log(f"Novel: {novel}")
        self.append_log(f"Scope: {percent:g}%")

        try:
            self.process = subprocess.Popen(
                command,
                cwd=APP_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as error:
            self.start_button.configure(state="normal")
            messagebox.showerror("Could not start", str(error))
            return
        threading.Thread(target=self._read_process, daemon=True).start()

    def stop_preparation(self):
        process = self.process
        if not process or process.poll() is not None:
            return
        if not messagebox.askyesno(
            "Stop preparation",
            "Stop now? Completed chunks are already checkpointed and will be reused on the next run.",
        ):
            return
        self.stop_requested = True
        self.stop_button.configure(state="disabled")
        self.stage_text.set("Stopping preparation...")
        self.eta_text.set("ETA: stopping")
        self.append_log("Stop requested. Preserving completed checkpoint data...")
        try:
            process.terminate()
        except Exception as error:
            self.append_log(f"Normal stop failed: {error}")
            try:
                process.kill()
            except Exception as kill_error:
                self.append_log(f"Forced stop failed: {kill_error}")

    def close_window(self):
        process = self.process
        if process and process.poll() is None:
            if not messagebox.askyesno(
                "Close preparation",
                "Preparation is still running. Stop it and close the window? Completed chunks will remain checkpointed.",
            ):
                return
            self.stop_requested = True
            try:
                process.terminate()
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        self.root.destroy()

    def _read_process(self):
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            if line.startswith("@@PROGRESS "):
                try:
                    self.events.put(("progress", json.loads(line[11:])))
                except json.JSONDecodeError:
                    self.events.put(("log", line))
            else:
                self.events.put(("log", line))
        return_code = self.process.wait()
        self.events.put(("finished", return_code))

    def _set_progress(self, payload):
        percent = float(payload.get("percent", 0))
        self.last_percent = percent
        self.progress_value.set(percent)
        self.stage_text.set(payload.get("label", "Working..."))
        if self.started_at and 0.5 <= percent < 100:
            elapsed = time.monotonic() - self.started_at
            remaining = elapsed * (100.0 - percent) / percent
            self.eta_text.set(f"ETA: {self._duration(remaining)}")
        elif percent >= 100:
            self.eta_text.set("ETA: complete")

    @staticmethod
    def _duration(seconds):
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    def _drain_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self.append_log(payload)
                elif event == "progress":
                    self._set_progress(payload)
                elif event == "finished":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.refresh_file_checks()
                    if self.stop_requested:
                        self.stage_text.set("Preparation stopped; checkpoint preserved")
                        self.eta_text.set("ETA: stopped")
                        self.append_log(
                            "Preparation stopped. Start again with any percentage to resume matching chunks."
                        )
                        self.stop_requested = False
                    elif payload == 0:
                        self.progress_value.set(100)
                        self.stage_text.set("Preparation complete")
                        self.eta_text.set("ETA: complete")
                        messagebox.showinfo(
                            "Preparation complete",
                            "All simulation preparation stages completed.",
                        )
                    else:
                        self.stage_text.set(f"Preparation failed (exit code {payload})")
                        self.eta_text.set("ETA: stopped")
                        messagebox.showerror(
                            "Preparation failed",
                            "The pipeline stopped. Review the preparation log for details.",
                        )
                elif event == "dialog":
                    title, text, kind = payload
                    if kind == "error":
                        messagebox.showerror(title, text)
                    else:
                        messagebox.showinfo(title, text)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def open_folder(self):
        try:
            os.startfile(APP_DIR)
        except Exception as error:
            messagebox.showerror("Open folder", str(error))


def main():
    root = tk.Tk()
    try:
        root.iconname("NavelMaker")
    except tk.TclError:
        pass
    PreparationApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
