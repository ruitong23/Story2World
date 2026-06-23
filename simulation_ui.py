"""Standalone Tkinter interface for the Step 17 novel simulation runtime."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import messagebox, ttk

from app_files import (
    SIMULATION_REQUIRED_FILES,
    file_status,
    generated_db_path,
    load_settings,
)
from step17_runtime import load_step17_runtime


def make_llm_callable(base_url, model, api_key):
    def call_llm(system_prompt, user_prompt, temperature=0.2, max_tokens=4096):
        request = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=900) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"].strip()

    return call_llm


class SimulationApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NavelMaker 2 - Novel Simulation")
        self.root.geometry("1180x800")
        self.root.minsize(960, 680)
        self.events = queue.Queue()
        self.operation_started = None
        self.selected_character_id = None

        saved = load_settings()
        self.base_url = os.getenv("NOVEL_LLM_BASE_URL", saved["llm_base_url"])
        self.model = os.getenv("NOVEL_LLM_MODEL", saved["llm_model"])
        self.api_key = (
            os.getenv("NOVEL_LLM_API_KEY", saved["llm_api_key"]).strip()
            or "lm-studio"
        )
        llm = make_llm_callable(self.base_url, self.model, self.api_key)
        self.runtime = load_step17_runtime(
            world_path=generated_db_path("canonical", "world_db.json"),
            character_path=generated_db_path(
                "canonical", "canonical_character_db.json"
            ),
            agent_path=generated_db_path("agents", "agent_profiles.json"),
            state_path=generated_db_path("runtime", "simulation_state.json"),
            llm_callable=llm,
        )
        self.store = self.runtime["store"]
        self.orchestrator = self.runtime["orchestrator"]
        self.catalog = sorted(
            self.orchestrator.agent_catalog(),
            key=lambda item: item.get("canonical_name", "").casefold(),
        )
        self.catalog_by_id = {
            item["character_id"]: item for item in self.catalog
        }
        self.character_by_id = {
            item["character_id"]: item
            for item in self.runtime["character_db"].get("characters", [])
        }
        self.filtered_catalog = list(self.catalog)
        self.list_ids = []

        self.search_text = tk.StringVar()
        self.file_status_text = tk.StringVar(value="Required files: all checks passed")
        self.runtime_status_text = tk.StringVar()
        self.progress_value = tk.DoubleVar(value=0)
        self.progress_text = tk.StringVar(value="Ready")
        self.eta_text = tk.StringVar(value="ETA: --")
        self.story_progress_value = tk.DoubleVar(value=0)
        self.story_progress_text = tk.StringVar(value="Story progress: --")

        self._build_ui()
        self._refresh_agent_list()
        self._refresh_runtime_status()
        self._show_latest_story()
        self.root.after(100, self._drain_events)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(
            header,
            text="NavelMaker 2",
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left")
        ttk.Label(header, textvariable=self.file_status_text, foreground="#137333").pack(
            side="right"
        )

        left = ttk.LabelFrame(outer, text="Choose a character", padding=8)
        left.grid(row=1, column=0, sticky="nsw", padx=(0, 8))
        left.rowconfigure(1, weight=1)

        search = ttk.Entry(left, textvariable=self.search_text, width=32)
        search.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        search.bind("<KeyRelease>", lambda _event: self._apply_filter())

        self.agent_list = tk.Listbox(left, width=35, exportselection=False)
        agent_scroll = ttk.Scrollbar(left, command=self.agent_list.yview)
        self.agent_list.configure(yscrollcommand=agent_scroll.set)
        self.agent_list.grid(row=1, column=0, sticky="nsew")
        agent_scroll.grid(row=1, column=1, sticky="ns")
        self.agent_list.bind("<<ListboxSelect>>", self._select_agent)

        self.agent_detail = tk.Text(left, width=35, height=13, wrap="word", state="disabled")
        self.agent_detail.grid(row=2, column=0, columnspan=2, pady=(8, 6), sticky="ew")
        self.enter_button = ttk.Button(
            left, text="Enter world as this character", command=self.enter_world
        )
        self.enter_button.grid(row=3, column=0, columnspan=2, sticky="ew")

        right = ttk.Frame(outer)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        runtime_frame = ttk.LabelFrame(right, text="Runtime", padding=8)
        runtime_frame.grid(row=0, column=0, sticky="ew")
        runtime_frame.columnconfigure(0, weight=1)
        ttk.Label(runtime_frame, textvariable=self.runtime_status_text).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Progressbar(
            runtime_frame,
            variable=self.progress_value,
            maximum=100,
        ).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(runtime_frame, textvariable=self.progress_text).grid(
            row=2, column=0, sticky="w"
        )
        ttk.Label(runtime_frame, textvariable=self.eta_text).grid(
            row=2, column=1, sticky="e", padx=8
        )
        ttk.Button(runtime_frame, text="Reset world", command=self.reset_world).grid(
            row=1, column=2, rowspan=2, padx=(8, 0)
        )
        ttk.Progressbar(
            runtime_frame,
            variable=self.story_progress_value,
            maximum=100,
        ).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(runtime_frame, textvariable=self.story_progress_text).grid(
            row=4, column=0, columnspan=3, sticky="w"
        )

        notebook = ttk.Notebook(right)
        notebook.grid(row=1, column=0, pady=(8, 0), sticky="nsew")
        story_tab = ttk.Frame(notebook, padding=8)
        status_tab = ttk.Frame(notebook, padding=8)
        diagnostics_tab = ttk.Frame(notebook, padding=8)
        notebook.add(story_tab, text="Story")
        notebook.add(status_tab, text="Character status")
        notebook.add(diagnostics_tab, text="Diagnostics")

        story_tab.columnconfigure(0, weight=1)
        story_tab.rowconfigure(0, weight=1)
        self.story = tk.Text(
            story_tab,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 11),
            padx=12,
            pady=12,
        )
        story_scroll = ttk.Scrollbar(story_tab, command=self.story.yview)
        self.story.configure(yscrollcommand=story_scroll.set)
        self.story.grid(row=0, column=0, sticky="nsew")
        story_scroll.grid(row=0, column=1, sticky="ns")

        self.user_input = tk.Text(story_tab, height=4, wrap="word")
        self.user_input.grid(row=1, column=0, pady=(8, 0), sticky="ew")
        self.continue_button = ttk.Button(
            story_tab, text="Continue story", command=self.continue_story
        )
        self.continue_button.grid(row=1, column=1, padx=(8, 0), pady=(8, 0), sticky="ns")

        self.status_view = tk.Text(status_tab, wrap="word", state="disabled")
        self.status_view.pack(fill="both", expand=True)
        self.diagnostics = tk.Text(
            diagnostics_tab, wrap="none", state="disabled", font=("Consolas", 9)
        )
        self.diagnostics.pack(fill="both", expand=True)

    def _apply_filter(self):
        query = self.search_text.get().strip().casefold()
        self.filtered_catalog = [
            item
            for item in self.catalog
            if not query
            or query in item.get("canonical_name", "").casefold()
            or query
            in " ".join(item.get("aliases", [])).casefold()
        ]
        self._refresh_agent_list()

    def _refresh_agent_list(self):
        self.agent_list.delete(0, "end")
        self.list_ids = []
        for item in self.filtered_catalog:
            tier = item.get("tier", "reference").upper()
            self.agent_list.insert(
                "end", f"{item.get('canonical_name', '?')}  [{tier}]"
            )
            self.list_ids.append(item["character_id"])

    def _select_agent(self, _event=None):
        selection = self.agent_list.curselection()
        if not selection:
            return
        character_id = self.list_ids[selection[0]]
        self.selected_character_id = character_id
        catalog = self.catalog_by_id[character_id]
        character = self.character_by_id.get(character_id, {})
        detail = "\n".join(
            [
                catalog.get("canonical_name", ""),
                f"Tier: {catalog.get('tier', 'unknown')}",
                catalog.get("notice", ""),
                "",
                character.get("background_summary", ""),
                "",
                "Aliases: " + ", ".join(character.get("aliases", [])),
            ]
        ).strip()
        self._set_text(self.agent_detail, detail)
        self._show_character_status(character_id)

    def _show_character_status(self, character_id=None):
        character_id = character_id or self.selected_character_id
        if not character_id:
            return
        character = self.character_by_id.get(character_id, {})
        runtime_state = self.store.runtime.get("character_runtime", {}).get(
            character_id, {}
        )
        content = {
            "canonical_name": character.get("canonical_name"),
            "background": character.get("background_summary"),
            "personality": character.get("personality", []),
            "goals": character.get("goals", []),
            "constraints": character.get("constraints", []),
            "abilities": character.get("abilities", []),
            "story_progress": self._story_progress_snapshot(),
            "runtime": runtime_state,
        }
        self._set_text(
            self.status_view, json.dumps(content, ensure_ascii=False, indent=2)
        )

    def _progress_callback(self, value, label):
        self.events.put(("progress", {"percent": value, "label": label}))

    def _run_operation(self, label, function):
        self.operation_started = time.monotonic()
        self.progress_value.set(1)
        self.progress_text.set(label)
        self.eta_text.set("ETA: calculating")
        self.enter_button.configure(state="disabled")
        self.continue_button.configure(state="disabled")

        def worker():
            try:
                result = function()
                self.events.put(("operation_done", result))
            except Exception as error:
                self.events.put(("operation_error", error))

        threading.Thread(target=worker, daemon=True).start()

    def enter_world(self):
        if not self.selected_character_id:
            messagebox.showwarning("Choose a character", "Select a character first.")
            return
        self._run_operation(
            "Creating the opening scene...",
            lambda: self.orchestrator.start_character_experience(
                self.selected_character_id,
                progress_callback=self._progress_callback,
            ),
        )

    def continue_story(self):
        if not self.store.runtime.get("active_scene"):
            messagebox.showwarning(
                "No active scene", "Choose a character and enter the world first."
            )
            return
        text = self.user_input.get("1.0", "end").strip()
        if not text:
            text = "I pause and pay attention to what is happening around me."
        self._run_operation(
            "Running the next simulation turn...",
            lambda: self.orchestrator.run_turn(
                text, progress_callback=self._progress_callback
            ),
        )

    def reset_world(self):
        if not messagebox.askyesno(
            "Reset simulation", "Reset runtime/simulation_state.json to a new main branch?"
        ):
            return
        self.store.reset()
        self.selected_character_id = None
        self.story.configure(state="normal")
        self.story.delete("1.0", "end")
        self.story.configure(state="disabled")
        self.progress_value.set(0)
        self.progress_text.set("World reset")
        self.eta_text.set("ETA: --")
        self._refresh_runtime_status()

    def _story_events(self):
        return [
            event
            for event in self.store.branch.get("events", [])
            if event.get("narration")
            and event.get("event_type")
            in {"scene_opening_rendered", "immersive_scene_turn"}
        ]

    def _show_latest_story(self):
        events = self._story_events()
        text = events[-1].get("narration", "") if events else ""
        if text:
            self._set_text(self.story, text)

    def _refresh_runtime_status(self):
        snapshot = self.store.snapshot()
        scene = snapshot.get("active_scene") or {}
        focus_id = scene.get("focus_character_id")
        focus = self.character_by_id.get(focus_id, {}).get(
            "canonical_name", "No active character"
        )
        clock = snapshot.get("clock", {})
        minute = int(clock.get("minute_of_day", 480))
        self.runtime_status_text.set(
            f"{focus} | Day {clock.get('day', 1)} "
            f"{minute // 60:02d}:{minute % 60:02d} | "
            f"Revision {snapshot.get('revision', 0)} | Model {self.model}"
        )
        story_progress = self._story_progress_snapshot()
        self.story_progress_value.set(story_progress["canonical_percent"])
        self.story_progress_text.set(story_progress["label"])
        if focus_id:
            self._show_character_status(focus_id)

    def _prepared_source_progress(self):
        graph_path = generated_db_path("graph", "raw_graph_triples.json")
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "completed": 0,
                "available": 0,
                "percent": 0.0,
            }
        completed = int(graph.get("completed_chunk_count") or 0)
        available = int(
            graph.get("available_source_chunk_count")
            or graph.get("total_source_chunk_count")
            or 0
        )
        available_unknown = not bool(available)
        if not available:
            manifest = graph.get("chunk_manifest", [])
            expected = int(graph.get("expected_chunk_count") or len(manifest) or 0)
            available = expected
        percent = completed * 100 / available if available else 0.0
        return {
            "completed": completed,
            "available": available,
            "available_unknown": available_unknown,
            "percent": max(0.0, min(100.0, percent)),
        }

    def _story_progress_snapshot(self):
        runtime = self.store.runtime
        timeline = (
            runtime.get("canonical_timeline")
            or self.runtime["world_db"]
            .get("canonical_timeline_db", {})
            .get("timeline_nodes", [])
        )
        total_canonical_events = len(timeline)
        cursor = int(runtime.get("timeline_cursor", 0) or 0)
        cursor = max(0, min(cursor, total_canonical_events))
        reached = (
            min(total_canonical_events, cursor + 1)
            if total_canonical_events and runtime.get("active_scene")
            else cursor
        )
        runtime_event_count = len(self.store.branch.get("events", []))
        committed_runtime_events = len(
            runtime.get("runtime_event_db", {}).get("runtime_committed_events", [])
        )
        prepared = self._prepared_source_progress()
        canonical_percent = (
            reached * 100 / total_canonical_events
            if total_canonical_events
            else 0.0
        )
        if prepared.get("available_unknown"):
            prepared_label = (
                f"Prepared scope: {prepared['completed']} chunks "
                "(source total unavailable until the next preparation run)"
            )
        else:
            prepared_label = (
                f"Prepared source: {prepared['completed']}/{prepared['available']} "
                f"chunks ({prepared['percent']:.1f}%)"
            )
        label = (
            f"{prepared_label} | "
            f"Canonical position in prepared scope: {reached}/{total_canonical_events} "
            f"events ({canonical_percent:.1f}%) | "
            f"Runtime events: {runtime_event_count} "
            f"(sidecar commits {committed_runtime_events})"
        )
        return {
            "canonical_percent": max(0.0, min(100.0, canonical_percent)),
            "prepared_source_percent": prepared["percent"],
            "prepared_completed_chunks": prepared["completed"],
            "prepared_available_chunks": prepared["available"],
            "canonical_reached_events": reached,
            "canonical_total_events": total_canonical_events,
            "runtime_event_count": runtime_event_count,
            "sidecar_committed_event_count": committed_runtime_events,
            "label": label,
        }

    @staticmethod
    def _set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _set_progress(self, payload):
        percent = max(0.0, min(100.0, float(payload.get("percent", 0))))
        self.progress_value.set(percent)
        self.progress_text.set(payload.get("label", "Working..."))
        if self.operation_started and percent >= 2 and percent < 100:
            elapsed = time.monotonic() - self.operation_started
            remaining = elapsed * (100 - percent) / percent
            self.eta_text.set(f"ETA: {max(1, round(remaining))}s")
        elif percent >= 100:
            self.eta_text.set("ETA: complete")

    def _drain_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    self._set_progress(payload)
                elif event == "operation_done":
                    self.enter_button.configure(state="normal")
                    self.continue_button.configure(state="normal")
                    self.progress_value.set(100)
                    self.progress_text.set("Turn complete")
                    self.eta_text.set("ETA: complete")
                    self.user_input.delete("1.0", "end")
                    self._show_latest_story()
                    self._refresh_runtime_status()
                    pipeline = payload.get("pipeline", payload)
                    self._set_text(
                        self.diagnostics,
                        json.dumps(pipeline, ensure_ascii=False, indent=2),
                    )
                elif event == "operation_error":
                    self.enter_button.configure(state="normal")
                    self.continue_button.configure(state="normal")
                    self.progress_text.set("Operation failed")
                    self.eta_text.set("ETA: stopped")
                    messagebox.showerror("Simulation error", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)


def main():
    checks = file_status(SIMULATION_REQUIRED_FILES)
    missing = [item for item in checks if not item["exists"]]
    root = tk.Tk()
    root.withdraw()
    if missing:
        lines = "\n".join(f"- {item['name']}: {item['description']}" for item in missing)
        messagebox.showerror(
            "Simulation files missing",
            "Run 02_prepare_simulation.bat first.\n\nMissing files:\n" + lines,
            parent=root,
        )
        root.destroy()
        return
    root.deiconify()
    SimulationApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
