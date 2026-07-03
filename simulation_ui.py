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
    save_llm_profile,
    set_active_llm_profile,
    delete_llm_profile,
)
from step17_runtime import clean_text, load_step17_runtime
from llm_api import chat_completion, list_models, token_usage_summary


def make_llm_callable(base_url, model, api_key):
    def call_llm(
        system_prompt,
        user_prompt,
        temperature=0.2,
        max_tokens=4096,
        response_format=None,
    ):
        return chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            source="desktop_simulation",
            flow="step17_runtime",
            response_format=response_format,
            timeout=900,
        )

    return call_llm


class LLMProfileDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("LLM API Profile")
        self.resizable(False, False)
        self.result = None
        saved = load_settings()
        self.profiles = saved.get("llm_profiles", [])
        self.profile_name = tk.StringVar(value=saved["active_llm_profile"])
        self.base_url = tk.StringVar(value=saved["llm_base_url"])
        self.model = tk.StringVar(value=saved["llm_model"])
        self.api_key = tk.StringVar(value=saved["llm_api_key"])
        self.status_text = tk.StringVar(
            value="Select or edit the OpenAI-compatible API used by simulation."
        )

        outer = ttk.Frame(self, padding=14)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(1, weight=1)
        ttk.Label(outer, text="Saved profile").grid(row=0, column=0, sticky="w")
        self.profile_combo = ttk.Combobox(
            outer,
            textvariable=self.profile_name,
            values=[item["profile_name"] for item in self.profiles],
            width=42,
        )
        self.profile_combo.grid(row=0, column=1, sticky="ew", padx=8)
        self.profile_combo.bind("<<ComboboxSelected>>", self._profile_selected)
        ttk.Label(outer, text="Base URL").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(outer, textvariable=self.base_url, width=48).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=(8, 0)
        )
        ttk.Label(outer, text="Model").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.model_combo = ttk.Combobox(outer, textvariable=self.model)
        self.model_combo.grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=(8, 0)
        )
        ttk.Label(outer, text="API key").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(outer, textvariable=self.api_key, show="*").grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=8, pady=(8, 0)
        )
        ttk.Label(outer, textvariable=self.status_text, foreground="#5f6f6a").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Button(actions, text="Save profile", command=self._save_profile).pack(
            side="left"
        )
        ttk.Button(actions, text="Delete", command=self._delete_profile).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="Check server", command=self._check_server).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="Start simulation", command=self._accept).pack(
            side="right"
        )
        ttk.Button(actions, text="Cancel", command=self._cancel).pack(
            side="right", padx=(0, 8)
        )
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.grab_set()
        self.wait_visibility()
        self.focus_set()

    def _current_profile(self):
        name = self.profile_name.get().strip() or self.model.get().strip()
        base_url = self.base_url.get().strip()
        model = self.model.get().strip()
        api_key = self.api_key.get().strip() or "lm-studio"
        if not base_url or not model:
            raise ValueError("LLM base URL and model are required.")
        return {
            "profile_name": name or "Local LM Studio",
            "llm_base_url": base_url,
            "llm_model": model,
            "llm_api_key": api_key,
        }

    def _refresh(self, saved):
        self.profiles = saved.get("llm_profiles", [])
        self.profile_combo.configure(
            values=[item["profile_name"] for item in self.profiles]
        )
        self.profile_name.set(saved["active_llm_profile"])
        self.base_url.set(saved["llm_base_url"])
        self.model.set(saved["llm_model"])
        self.api_key.set(saved["llm_api_key"])

    def _profile_selected(self, _event=None):
        try:
            self._refresh(set_active_llm_profile(self.profile_name.get().strip()))
        except KeyError:
            pass

    def _save_profile(self):
        try:
            self._refresh(save_llm_profile(self._current_profile(), make_active=True))
            self.status_text.set("Profile saved.")
        except Exception as error:
            messagebox.showerror("LLM profile", str(error), parent=self)

    def _delete_profile(self):
        name = self.profile_name.get().strip()
        if not name:
            return
        if messagebox.askyesno("LLM profile", f"Delete profile '{name}'?", parent=self):
            self._refresh(delete_llm_profile(name))
            self.status_text.set("Profile deleted.")

    def _check_server(self):
        try:
            models = list_models(
                self.base_url.get().strip(),
                self.api_key.get().strip(),
            )
            self.model_combo.configure(values=models)
            selected = self.model.get().strip()
            if selected in models:
                self.status_text.set(f"Server online. Selected model found: {selected}")
            elif models:
                self.model.set(models[0])
                self.status_text.set(
                    f"Server online. Select a model from {len(models)} available models."
                )
            else:
                self.status_text.set("Server online, but no models were listed.")
        except Exception as error:
            self.status_text.set(f"Server check failed: {error}")

    def _accept(self):
        try:
            self.result = save_llm_profile(
                self._current_profile(),
                make_active=True,
            )
        except Exception as error:
            messagebox.showerror("LLM profile", str(error), parent=self)
            return
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class SimulationApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NavelMaker 2 - Novel Simulation")
        self.root.geometry("1280x820")
        self.root.minsize(1040, 700)
        self.events = queue.Queue()
        self.operation_started = None
        self.selected_character_id = None
        self.start_percent = tk.DoubleVar(value=0.0)

        dialog = LLMProfileDialog(root)
        root.wait_window(dialog)
        if dialog.result is None:
            root.destroy()
            return
        saved = dialog.result
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
        self.character_by_id = {
            item["character_id"]: item
            for item in self.runtime["character_db"].get("characters", [])
        }
        self.catalog = sorted(
            self.orchestrator.agent_catalog(),
            key=self._catalog_sort_key,
        )
        self.catalog_by_id = {
            item["character_id"]: item for item in self.catalog
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
        self._restore_active_character()
        self._refresh_runtime_status()
        self._show_latest_story(prefer_recovery=True)
        self.root.after(100, self._drain_events)

    def _catalog_sort_key(self, item):
        character = self.runtime["character_db"].get("character_by_id", {}).get(
            item.get("character_id"), {}
        )
        if not character:
            character = self.character_by_id.get(item.get("character_id"), {})
        tier_score = {"full": 3, "light": 2, "reference": 1}.get(
            item.get("tier"), 0
        )
        data_score = (
            tier_score * 1000
            + len(character.get("all_relations", [])) * 20
            + len(character.get("abilities", [])) * 15
            + (
                len(character.get("owned_items", []))
                + len(character.get("used_items", []))
            )
            * 10
            + len(character.get("evidence_refs", []))
            + len(character.get("source_chunk_ids", []))
        )
        return (-data_score, item.get("canonical_name", "").casefold())

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
        start_scope = ttk.Frame(left)
        start_scope.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        start_scope.columnconfigure(1, weight=1)
        ttk.Label(start_scope, text="Start %").grid(row=0, column=0, sticky="w")
        ttk.Scale(
            start_scope,
            from_=0,
            to=100,
            variable=self.start_percent,
        ).grid(row=0, column=1, padx=6, sticky="ew")
        ttk.Label(start_scope, text="0 = auto").grid(row=0, column=2, sticky="e")
        self.enter_button = ttk.Button(
            left, text="Enter world as this character", command=self.enter_world
        )
        self.enter_button.grid(row=4, column=0, columnspan=2, sticky="ew")

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
        self.save_button = ttk.Button(
            runtime_frame, text="Save", command=self.save_world
        )
        self.save_button.grid(row=1, column=2, padx=(8, 0))
        ttk.Button(runtime_frame, text="Reset world", command=self.reset_world).grid(
            row=2, column=2, padx=(8, 0)
        )
        ttk.Progressbar(
            runtime_frame,
            variable=self.story_progress_value,
            maximum=100,
        ).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(runtime_frame, textvariable=self.story_progress_text).grid(
            row=4, column=0, columnspan=3, sticky="w"
        )
        ttk.Button(
            runtime_frame,
            text="Preview DB anchor",
            command=self.preview_db_anchor,
        ).grid(row=5, column=2, padx=(8, 0), pady=(6, 0), sticky="e")
        ttk.Button(
            runtime_frame,
            text="Token usage",
            command=self.show_token_usage,
        ).grid(row=5, column=1, padx=(8, 0), pady=(6, 0), sticky="e")

        notebook = ttk.Notebook(right)
        notebook.grid(row=1, column=0, pady=(8, 0), sticky="nsew")
        story_tab = ttk.Frame(notebook, padding=8)
        world_tab = ttk.Frame(notebook, padding=8)
        status_tab = ttk.Frame(notebook, padding=8)
        diagnostics_tab = ttk.Frame(notebook, padding=8)
        notebook.add(story_tab, text="Story")
        notebook.add(world_tab, text="World admin")
        notebook.add(status_tab, text="Character status")
        notebook.add(diagnostics_tab, text="Diagnostics")

        story_tab.columnconfigure(0, weight=1)
        story_tab.columnconfigure(2, weight=0)
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
        agent_trace_frame = ttk.LabelFrame(
            story_tab,
            text="Agent trace",
            padding=6,
        )
        agent_trace_frame.grid(
            row=0,
            column=2,
            sticky="nsew",
            padx=(8, 0),
        )
        agent_trace_frame.rowconfigure(0, weight=1)
        agent_trace_frame.columnconfigure(0, weight=1)
        self.agent_trace = tk.Text(
            agent_trace_frame,
            width=36,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 9),
            padx=8,
            pady=8,
        )
        agent_trace_scroll = ttk.Scrollbar(
            agent_trace_frame,
            command=self.agent_trace.yview,
        )
        self.agent_trace.configure(yscrollcommand=agent_trace_scroll.set)
        self.agent_trace.grid(row=0, column=0, sticky="nsew")
        agent_trace_scroll.grid(row=0, column=1, sticky="ns")

        self.user_input = tk.Text(story_tab, height=4, wrap="word")
        self.user_input.grid(row=1, column=0, pady=(8, 0), sticky="ew")
        self.continue_button = ttk.Button(
            story_tab, text="Continue story", command=self.continue_story
        )
        self.continue_button.grid(row=1, column=2, padx=(8, 0), pady=(8, 0), sticky="nsew")

        world_tab.columnconfigure(0, weight=1)
        world_tab.rowconfigure(0, weight=1)
        self.world_admin_output = tk.Text(
            world_tab,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 10),
            padx=10,
            pady=10,
        )
        world_scroll = ttk.Scrollbar(world_tab, command=self.world_admin_output.yview)
        self.world_admin_output.configure(yscrollcommand=world_scroll.set)
        self.world_admin_output.grid(row=0, column=0, sticky="nsew")
        world_scroll.grid(row=0, column=1, sticky="ns")
        self.world_admin_input = tk.Text(world_tab, height=4, wrap="word")
        self.world_admin_input.grid(row=1, column=0, pady=(8, 0), sticky="ew")
        world_actions = ttk.Frame(world_tab)
        world_actions.grid(row=1, column=1, padx=(8, 0), pady=(8, 0), sticky="ns")
        self.world_admin_send_button = ttk.Button(
            world_actions,
            text="Ask / Apply",
            command=self.send_world_admin,
        )
        self.world_admin_send_button.pack(fill="x")
        ttk.Button(
            world_actions,
            text="Refresh",
            command=self.refresh_world_admin_snapshot,
        ).pack(fill="x", pady=(6, 0))

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

    def show_token_usage(self):
        usage = token_usage_summary(limit=80)
        lines = [
            "Token usage from recorded LLM calls",
            "",
            f"Calls: {usage['totals']['call_count']}",
            f"Prompt tokens: {usage['totals']['prompt_tokens']}",
            f"Completion tokens: {usage['totals']['completion_tokens']}",
            f"Total tokens: {usage['totals']['total_tokens']}",
            "",
            "By source:",
        ]
        for item in usage.get("by_source", [])[:12]:
            lines.append(
                f"- {item['name']}: {item['total_tokens']} tokens / {item['call_count']} calls"
            )
        lines.append("")
        lines.append("By model:")
        for item in usage.get("by_model", [])[:12]:
            lines.append(
                f"- {item['name']}: {item['total_tokens']} tokens / {item['call_count']} calls"
            )
        lines.append("")
        lines.append(f"Log file: {usage.get('path')}")
        messagebox.showinfo("Token usage", "\n".join(lines), parent=self.root)

    def _append_world_admin(self, speaker, text):
        self.world_admin_output.configure(state="normal")
        current = self.world_admin_output.get("1.0", "end").strip()
        prefix = "\n\n" if current else ""
        self.world_admin_output.insert(
            "end",
            f"{prefix}{speaker}\n{text.strip()}",
        )
        self.world_admin_output.see("end")
        self.world_admin_output.configure(state="disabled")

    def refresh_world_admin_snapshot(self):
        snapshot = self.orchestrator.world_admin_snapshot()
        self._set_text(
            self.world_admin_output,
            json.dumps(snapshot, ensure_ascii=False, indent=2),
        )

    def send_world_admin(self):
        text = self.world_admin_input.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning(
                "World admin",
                "Enter a world/admin question or modification request first.",
                parent=self.root,
            )
            return
        self.world_admin_input.delete("1.0", "end")
        self._append_world_admin("You", text)
        self.progress_value.set(8)
        self.progress_text.set("World admin is reading runtime state...")
        self.eta_text.set("ETA: calculating")
        self.world_admin_send_button.configure(state="disabled")

        def worker():
            try:
                result = self.orchestrator.world_admin_chat(text)
                self.events.put(("world_admin_done", result))
            except Exception as error:
                self.events.put(("world_admin_error", error))

        threading.Thread(target=worker, daemon=True).start()

    def _restore_active_character(self):
        scene = self.store.runtime.get("active_scene") or {}
        character_id = scene.get("focus_character_id")
        if not character_id or character_id not in self.list_ids:
            return
        index = self.list_ids.index(character_id)
        self.agent_list.selection_clear(0, "end")
        self.agent_list.selection_set(index)
        self.agent_list.see(index)
        self._select_agent()

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
        self.save_button.configure(state="disabled")
        self.world_admin_send_button.configure(state="disabled")

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
                progress_percent=(
                    float(self.start_percent.get())
                    if float(self.start_percent.get()) > 0
                    else None
                ),
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
            text = "观察并让局势自然推进"
        self._run_operation(
            "Running the next simulation turn...",
            lambda: self.orchestrator.run_turn(
                text, progress_callback=self._progress_callback
            ),
        )

    def save_world(self):
        if not self.store.runtime.get("active_scene"):
            messagebox.showwarning(
                "No active scene",
                "Choose a character and enter the world before saving.",
            )
            return
        self._run_operation(
            "Preparing manual save...",
            lambda: {
                "manual_save": self.orchestrator.create_manual_save(
                    progress_callback=self._progress_callback
                )
            },
        )

    def _db_anchor_packet(self):
        story_progress = self._story_progress_snapshot()
        runtime = self.store.runtime
        timeline = (
            runtime.get("canonical_timeline")
            or self.orchestrator.canonical_timeline
            or self.runtime["world_db"]
            .get("canonical_timeline_db", {})
            .get("timeline_nodes", [])
        )
        scene = runtime.get("active_scene") or {}
        focus_id = scene.get("focus_character_id") or self.selected_character_id
        if timeline and not runtime.get("active_scene") and self.start_percent.get() > 0:
            cursor = round((len(timeline) - 1) * float(self.start_percent.get()) / 100)
        elif timeline and not runtime.get("active_scene") and focus_id:
            try:
                cursor, _anchor = self.orchestrator._opening_anchor(focus_id)
            except Exception:
                cursor = int(runtime.get("timeline_cursor", 0) or 0)
        else:
            cursor = int(runtime.get("timeline_cursor", 0) or 0)
        cursor = max(0, min(cursor, max(0, len(timeline) - 1)))
        current = timeline[cursor] if timeline else {}
        nearby = timeline[max(0, cursor - 2): cursor + 3] if timeline else []
        focus_name = self.character_by_id.get(focus_id, {}).get(
            "canonical_name", "No active character"
        )
        character_packet = self._character_preview_packet(
            focus_id,
            cursor,
            timeline,
        )
        return {
            "focus_character": focus_name,
            "focus_character_id": focus_id,
            "focus_character_packet": character_packet,
            "selected_start_percent": float(self.start_percent.get()),
            "preview_cursor": cursor,
            "active_scene": scene,
            "story_progress": story_progress,
            "current_canonical_anchor": current,
            "nearby_canonical_anchors": nearby,
            "recent_branch_records": runtime.get("branch_records", [])[-5:],
            "recent_runtime_events": [
                {
                    "event_type": item.get("event_type"),
                    "narration": item.get("narration", "")[:500],
                    "revision_after": item.get("revision_after"),
                }
                for item in self.store.branch.get("events", [])[-5:]
            ],
        }

    def _character_preview_packet(self, character_id, cursor, timeline):
        if not character_id:
            return {}
        character = self.character_by_id.get(character_id, {})
        try:
            profile = self.orchestrator._dynamic_profile(character_id)
        except Exception:
            profile = {}
        world_db = self.runtime["world_db"]
        canonical_db = world_db.get("canonical_novel_db", {})
        entity_track = canonical_db.get("entity_tracks", {}).get(character_id, {})
        names = {
            character.get("canonical_name", ""),
            entity_track.get("canonical_name", ""),
            profile.get("canonical_name", ""),
            *character.get("aliases", []),
            *character.get("forms", []),
            *entity_track.get("aliases", []),
            *entity_track.get("forms", []),
            *profile.get("identity", {}).get("aliases", []),
            *profile.get("identity", {}).get("forms", []),
        }
        names = {item for item in names if item}
        source_orders = set()
        for value in (
            character.get("first_seen_order"),
            entity_track.get("first_seen_order"),
            profile.get("first_seen_order"),
        ):
            if str(value).isdigit():
                source_orders.add(int(value))
        for source in (
            character.get("source_chunk_ids", []),
            entity_track.get("source_chunk_ids", []),
            profile.get("source_chunk_refs", []),
        ):
            for value in source:
                if str(value).isdigit():
                    source_orders.add(int(value))
        for evidence in [
            *character.get("evidence_refs", []),
            *entity_track.get("evidence_refs", []),
            *profile.get("evidence_refs", []),
        ]:
            value = evidence.get("source_chunk_id")
            if str(value).isdigit():
                source_orders.add(int(value))
        if timeline and not source_orders and 0 <= cursor < len(timeline):
            for value in timeline[cursor].get("source_chunk_ids", []):
                if str(value).isdigit():
                    source_orders.add(int(value))

        relationship_lines = []
        for relation in canonical_db.get("relationship_development_lines", []):
            if character_id in {
                relation.get("source_entity_id"),
                relation.get("target_entity_id"),
            }:
                relationship_lines.append(relation)

        scene_beat_hits = []
        for beat in self.orchestrator.canonical_timeline:
            text = json.dumps(beat, ensure_ascii=False)
            if character_id in text or any(name and name in text for name in names):
                scene_beat_hits.append(beat)
                continue
            beat_orders = {
                int(value)
                for value in beat.get("source_chunk_ids", [])
                if str(value).isdigit()
            }
            if beat_orders & source_orders:
                scene_beat_hits.append(beat)

        raw_contexts = self._raw_chunk_preview_context(names, source_orders)
        raw_text = json.dumps(raw_contexts, ensure_ascii=False)
        related_names = set()
        evidence_digest = {
            "identity_or_forms": [],
            "locations": [],
            "abilities": [],
            "items": [],
            "relationship_or_conflict_lines": [],
            "nearby_event_lines": [],
            "raw_focus_descriptions": [],
        }
        focus_names = {name for name in names if name}
        for chunk in raw_contexts:
            for node in chunk.get("nodes", []):
                node_name = node.get("surface_name")
                node_type = node.get("type")
                description = node.get("description")
                if node_name:
                    related_names.add(node_name)
                node_line = "：".join(
                    part for part in (node_name, description) if part
                )
                if not node_line:
                    continue
                if node_name in focus_names or any(
                    name and name in node_line for name in focus_names
                ):
                    evidence_digest["raw_focus_descriptions"].append(node_line)
                if node_type in {"TitleOrIdentity", "Identity", "Form"}:
                    evidence_digest["identity_or_forms"].append(node_line)
                elif node_type == "Location":
                    evidence_digest["locations"].append(node_line)
                elif node_type == "Ability":
                    evidence_digest["abilities"].append(node_line)
                elif node_type in {"Artifact", "Item", "Weapon"}:
                    evidence_digest["items"].append(node_line)
            for edge in chunk.get("edges", []):
                if edge.get("source_surface_name"):
                    related_names.add(edge["source_surface_name"])
                if edge.get("target_surface_name"):
                    related_names.add(edge["target_surface_name"])
                summary = edge.get("relation_summary") or edge.get("summary")
                if summary:
                    evidence_digest["nearby_event_lines"].append(summary)
                edge_text = json.dumps(edge, ensure_ascii=False)
                if summary and any(
                    name and name in edge_text for name in focus_names
                ):
                    evidence_digest["relationship_or_conflict_lines"].append(
                        summary
                    )
        for beat in scene_beat_hits:
            beat_line = beat.get("summary") or beat.get("event")
            if beat_line:
                evidence_digest["nearby_event_lines"].append(beat_line)
        for key, values in list(evidence_digest.items()):
            deduped = []
            seen = set()
            for value in values:
                if value and value not in seen:
                    deduped.append(value)
                    seen.add(value)
                if len(deduped) >= 12:
                    break
            evidence_digest[key] = deduped

        return {
            "character_name": character.get("canonical_name")
            or entity_track.get("canonical_name")
            or profile.get("canonical_name"),
            "character": {
                "character_id": character_id,
                "canonical_name": character.get("canonical_name")
                or entity_track.get("canonical_name")
                or profile.get("canonical_name"),
                "aliases": character.get("aliases", []),
                "titles": character.get("titles", []),
                "forms": character.get("forms", [])
                or entity_track.get("forms", []),
                "first_seen_order": character.get("first_seen_order")
                or entity_track.get("first_seen_order"),
                "description": character.get("background_summary")
                or "；".join(entity_track.get("descriptions", [])),
                "attributes": entity_track.get("attributes", {}),
                "profile_tier": profile.get("profile_tier", "reference"),
                "runtime_mode": profile.get("runtime_mode", "dynamic_reference_agent"),
            },
            "capabilities": profile.get("capabilities", {}),
            "relationships": profile.get("relationships", [])[:16],
            "canonical_relationship_lines": relationship_lines[:16],
            "related_scene_beats": scene_beat_hits[:12],
            "raw_chunk_contexts": raw_contexts[:6],
            "evidence_digest": evidence_digest,
            "related_surface_names": sorted(related_names)[:40],
            "evidence_gaps": {
                "has_prebuilt_agent_profile": bool(
                    character_id in self.orchestrator.agent_by_character_id
                ),
                "has_direct_relationship_lines": bool(relationship_lines),
                "has_raw_chunk_context": bool(raw_contexts),
                "raw_context_mentions_focus": any(
                    name and name in raw_text for name in names
                ),
            },
            "preview_policy": {
                "prefer_character_centered_summary": True,
                "state_uncertainty_when_db_is_sparse": True,
                "do_not_use_external_story_knowledge": True,
                "include_playable_opening_hooks": True,
            },
        }

    def _raw_chunk_preview_context(self, names, source_orders):
        graph_path = generated_db_path("graph", "raw_graph_triples.json")
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        target_orders = set(source_orders)
        for order in list(source_orders):
            target_orders.update({order - 1, order, order + 1})
        contexts = []
        for chunk in graph.get("results", []):
            chunk_id = chunk.get("chunk_id")
            try:
                chunk_order = int(chunk_id)
            except (TypeError, ValueError):
                chunk_order = None
            text = json.dumps(chunk, ensure_ascii=False)
            if chunk_order not in target_orders and not any(
                name and name in text for name in names
            ):
                continue
            nodes = [
                node
                for node in chunk.get("nodes", [])
                if any(name and name in json.dumps(node, ensure_ascii=False) for name in names)
                or chunk_order in source_orders
            ][:16]
            edges = [
                edge
                for edge in chunk.get("edges", [])
                if any(name and name in json.dumps(edge, ensure_ascii=False) for name in names)
                or chunk_order in source_orders
            ][:24]
            if not nodes and not edges:
                continue
            contexts.append(
                {
                    "chunk_id": chunk_id,
                    "chunk_index": chunk.get("chunk_index"),
                    "nodes": nodes,
                    "edges": edges,
                    "validation_status": chunk.get("validation_status"),
                }
            )
            if len(contexts) >= 8:
                break
        return contexts

    def _compact_db_anchor_packet_for_llm(self, payload):
        character_packet = payload.get("focus_character_packet") or {}

        def compact_anchor(anchor):
            if not anchor:
                return {}
            return {
                "scheduled_order": anchor.get("scheduled_order"),
                "event": anchor.get("event"),
                "summary": anchor.get("summary"),
                "location_id": anchor.get("location_id"),
                "participant_ids": anchor.get("participant_ids", [])[:8],
                "source_chunk_ids": anchor.get("source_chunk_ids", [])[:8],
                "confidence": anchor.get("confidence"),
            }

        def compact_relation(relation):
            return {
                "source": relation.get("source_entity_name")
                or relation.get("source_surface_name")
                or relation.get("source_entity_id"),
                "target": relation.get("target_entity_name")
                or relation.get("target_surface_name")
                or relation.get("target_entity_id"),
                "type": relation.get("relation_type") or relation.get("type"),
                "summary": relation.get("summary")
                or relation.get("relation_summary"),
            }

        return {
            "focus_character": payload.get("focus_character"),
            "selected_start_percent": payload.get("selected_start_percent"),
            "preview_cursor": payload.get("preview_cursor"),
            "story_progress": payload.get("story_progress"),
            "current_anchor": compact_anchor(
                payload.get("current_canonical_anchor")
            ),
            "nearby_anchors": [
                compact_anchor(anchor)
                for anchor in payload.get("nearby_canonical_anchors", [])[:5]
            ],
            "character": character_packet.get("character", {}),
            "character_evidence_digest": character_packet.get(
                "evidence_digest", {}
            ),
            "canonical_relationship_lines": [
                compact_relation(relation)
                for relation in character_packet.get(
                    "canonical_relationship_lines", []
                )[:8]
            ],
            "related_scene_beats": [
                compact_anchor(anchor)
                for anchor in character_packet.get("related_scene_beats", [])[:8]
            ],
            "related_surface_names": character_packet.get(
                "related_surface_names", []
            )[:30],
            "evidence_gaps": character_packet.get("evidence_gaps", {}),
            "recent_branch_records": payload.get("recent_branch_records", []),
            "recent_runtime_events": payload.get("recent_runtime_events", []),
        }

    def _render_db_anchor_preview_material(self, material):
        character = material.get("character") or {}
        digest = material.get("character_evidence_digest") or {}
        current = material.get("current_anchor") or {}
        gaps = material.get("evidence_gaps") or {}
        lines = [
            f"焦点角色：{material.get('focus_character') or 'DB 暂无'}",
            f"当前剧情锚点：{current.get('event') or 'DB 暂无'}",
            f"锚点顺序：{current.get('scheduled_order') or 'DB 暂无'}",
            f"角色身份描述：{character.get('description') or 'DB 暂无'}",
            f"首次出场顺序：{character.get('first_seen_order') or 'DB 暂无'}",
            "身份/别名/形态证据："
            + "；".join(digest.get("identity_or_forms") or ["DB 暂无"]),
            "地点证据：" + "；".join(digest.get("locations") or ["DB 暂无"]),
            "能力证据：" + "；".join(digest.get("abilities") or ["DB 暂无"]),
            "物品证据：" + "；".join(digest.get("items") or ["DB 暂无"]),
            "关系/冲突证据："
            + "；".join(
                digest.get("relationship_or_conflict_lines") or ["DB 暂无"]
            ),
            "附近事件证据："
            + "；".join(digest.get("nearby_event_lines") or ["DB 暂无"]),
            "角色原始描述："
            + "；".join(digest.get("raw_focus_descriptions") or ["DB 暂无"]),
            "相关表面名："
            + "；".join(material.get("related_surface_names") or ["DB 暂无"]),
        ]
        gap_lines = []
        if not gaps.get("has_prebuilt_agent_profile"):
            gap_lines.append("没有预制角色画像，需要运行时用原文片段补充性格、口吻和行动习惯")
        if not gaps.get("has_direct_relationship_lines"):
            gap_lines.append("没有直接关系线，需要从附近事件推断与关键人物或组织的冲突关系")
        if not gaps.get("has_raw_chunk_context"):
            gap_lines.append("没有原始片段上下文，需要回查原文")
        if not gap_lines:
            gap_lines.append("暂无明显缺口")
        lines.append("资料缺口：" + "；".join(gap_lines))
        related_beats = []
        for beat in material.get("related_scene_beats", [])[:5]:
            beat_line = beat.get("summary") or beat.get("event")
            if beat_line:
                related_beats.append(beat_line)
        if related_beats:
            lines.append("相关剧情片段：" + "；".join(related_beats))
        return "\n".join(lines)

    def preview_db_anchor(self):
        payload = self._db_anchor_packet()
        preview_material = self._compact_db_anchor_packet_for_llm(payload)
        preview_material_text = self._render_db_anchor_preview_material(
            preview_material
        )
        self.progress_text.set("Summarizing DB anchor...")

        def worker():
            try:
                summary = self.orchestrator._call_text(
                    (
                        "你是给玩家看的小说剧情预览撰稿人，不是技术顾问。"
                        "只根据给出的事实清单写角色开局预览，不使用外部知识。"
                        "你的目标是帮助玩家判断：这个角色此刻是谁、处在什么剧情附近、"
                        "有什么能力/关系/前因后果，以及适合从哪里开局。"
                        "禁止写技术方案、代码、schema、pipeline、API、节点、边、字段、ID、表格。"
                        "证据不足时写“资料暂缺”，不要编造。"
                        "你的第一行必须直接是“角色名 - 剧情阶段”，不要写"
                        "“根据提供的信息/数据”等开场白。"
                    ),
                    (
                        "请只根据下面的事实清单写预览，不要提到事实清单、"
                        "技术系统或内部结构。\n\n"
                        "输出格式：\n"
                        "角色名 - 当前剧情阶段\n"
                        "2到4句话可玩定位。\n\n"
                        "【已知信息】\n"
                        "- 身份/别名/形态：...\n"
                        "- 目标或动机：...\n"
                        "- 地点：...\n"
                        "- 人物关系：...\n\n"
                        "【能力与限制】\n"
                        "- 能力：...\n"
                        "- 物品：...\n"
                        "- 弱点或限制：...\n\n"
                        "【前因后果】\n"
                        "...\n\n"
                        "【开局切入点】\n"
                        "1. ...\n2. ...\n3. ...\n\n"
                        "【资料缺口】\n"
                        "- ...\n\n"
                        "事实清单：\n"
                        f"{preview_material_text}"
                    ),
                    temperature=0.2,
                    max_tokens=1600,
                )
                self.events.put(
                    (
                        "preview",
                        {
                            "title": "DB anchor preview",
                            "text": summary
                            + "\n\n--- DB anchor packet ---\n"
                            + json.dumps(payload, ensure_ascii=False, indent=2),
                        },
                    )
                )
            except Exception as error:
                self.events.put(("operation_error", error))

        threading.Thread(target=worker, daemon=True).start()

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

    def _latest_story_event(self):
        events = self._story_events()
        return events[-1] if events else {}

    def _character_name(self, character_id):
        try:
            profile = self.orchestrator._dynamic_profile(character_id)
            if profile.get("canonical_name"):
                return profile["canonical_name"]
        except Exception:
            pass
        return (
            self.character_by_id.get(character_id, {}).get("canonical_name")
            or self.catalog_by_id.get(character_id, {}).get("canonical_name")
            or character_id
        )

    def _agent_trace_snapshot(self, event=None):
        result = event if isinstance(event, dict) and "event" in event else {}
        event = (
            result.get("event")
            if result
            else event or self._latest_story_event()
        )
        if not isinstance(event, dict):
            event = {}
        scene = self.store.runtime.get("active_scene") or {}
        participant_ids = [
            item for item in scene.get("participant_ids", []) if item
        ]
        focus_id = scene.get("focus_character_id")
        pipeline = result.get("pipeline", {}) if result else event.get("pipeline", {})
        if not pipeline:
            pipeline = event.get("backend_pipeline", {})
        active_agent_ids = {
            clean_text(item.get("character_id"))
            for item in pipeline.get("nearby_npc_agents", [])
            if isinstance(item, dict) and clean_text(item.get("character_id"))
        }
        if not active_agent_ids and isinstance(event, dict):
            active_agent_ids = {
                clean_text(item.get("character_id"))
                for item in event.get("npc_agent_outputs", [])
                if isinstance(item, dict) and clean_text(item.get("character_id"))
            }
        agent_controlled = []
        passive_present = []
        for character_id in participant_ids:
            catalog = self.catalog_by_id.get(character_id, {})
            try:
                profile = self.orchestrator._dynamic_profile(character_id)
            except Exception:
                profile = {}
            tier = catalog.get("tier") or profile.get("profile_tier", "reference")
            runtime_mode = (
                catalog.get("runtime_mode")
                or profile.get("runtime_mode")
                or ""
            )
            control = (
                "MANUAL"
                if character_id == focus_id
                else self.store.runtime.get("agent_control", {}).get(
                    character_id,
                    "AUTO",
                )
            )
            is_agent_controlled = bool(
                character_id == focus_id
                or character_id in active_agent_ids
                or (
                    control == "AUTO"
                    and tier in {"full", "light", "runtime"}
                    and "reference" not in runtime_mode
                )
            )
            row = {
                "character_id": character_id,
                "name": self._character_name(character_id),
                "tier": tier,
                "control": control,
                "runtime_mode": runtime_mode,
                "agent_awake": character_id in active_agent_ids,
            }
            if is_agent_controlled:
                agent_controlled.append(row)
            else:
                passive_present.append(row)
        contributors = []
        seen_contributors = set()

        def add_contributor(name, kind, status=""):
            name = clean_text(name) if "clean_text" in globals() else str(name or "").strip()
            kind = clean_text(kind) if "clean_text" in globals() else str(kind or "").strip()
            status = clean_text(status) if "clean_text" in globals() else str(status or "").strip()
            if not name:
                return
            marker = (name, kind, status)
            if marker in seen_contributors:
                return
            seen_contributors.add(marker)
            contributors.append({
                "name": name,
                "kind": kind,
                "status": status,
            })

        if pipeline.get("player_controller"):
            add_contributor("Player Controller", "角色控制", "ran")
        if pipeline.get("time_agent"):
            source = pipeline.get("time_agent", {}).get("source", "")
            add_contributor(
                "Time Service" if source else "Time Agent",
                "时间",
                "deterministic" if source else "ran",
            )
        rules = pipeline.get("rules_agent", {})
        if rules:
            add_contributor(
                "Rule Checker",
                "规则",
                f"{rules.get('validation_count', 0)} checks",
            )
        for item in pipeline.get("nearby_npc_agents", []):
            if not isinstance(item, dict):
                continue
            add_contributor(
                item.get("canonical_name") or item.get("character_id"),
                "Character Agent",
                item.get("visible_behavior")
                or item.get("action_intent", {}).get("description", "")
                or "proposal",
            )
        group_controller = pipeline.get("group_controller", {})
        if pipeline.get("group_controller_ran") or (
            isinstance(group_controller, dict) and group_controller.get("ran")
        ):
            group_count = (
                len(group_controller.get("groups", []))
                if isinstance(group_controller, dict)
                else 0
            )
            labels = []
            if isinstance(group_controller, dict):
                labels = [
                    clean_text(item.get("label"))
                    for item in group_controller.get("groups", [])
                    if isinstance(item, dict) and clean_text(item.get("label"))
                ]
            add_contributor(
                "Group Controller",
                "群体",
                "、".join(labels[:2]) if labels else f"groups x{group_count}",
            )
        local_world = pipeline.get("local_world_agent", {})
        if pipeline.get("local_world_agent_ran") or local_world:
            ambient_count = (
                len(local_world.get("ambient_npc_reactions", []))
                if isinstance(local_world, dict)
                else 0
            )
            event_count = (
                len(local_world.get("new_events", []))
                if isinstance(local_world, dict)
                else 0
            )
            status_parts = []
            if ambient_count:
                status_parts.append(f"ambient NPC x{ambient_count}")
            if event_count:
                status_parts.append(f"events x{event_count}")
            add_contributor(
                "Local World Agent",
                "局部世界",
                " / ".join(status_parts) if status_parts else "ran",
            )
        if pipeline.get("gm_resolver_ran") or pipeline.get("gm_resolver"):
            add_contributor(
                "GM Resolver",
                "裁决",
                pipeline.get("gm_resolver", {}).get(
                    "outcome",
                    "ran",
                ),
            )
        if pipeline.get("global_world_agent_ran"):
            add_contributor("Global World Agent", "大世界", "ran")
        if pipeline.get("memory_agent"):
            memory = pipeline.get("memory_agent", {})
            add_contributor(
                "Memory Agent",
                "记忆",
                (
                    "compacted"
                    if memory.get("summary_compaction_ran")
                    else "recorded"
                ),
            )
        if pipeline.get("scene_renderer"):
            add_contributor("Scene Renderer", "叙事", "rendered")

        if not pipeline and event:
            if event.get("player_intent"):
                add_contributor("Player Controller", "角色控制", "committed")
            if event.get("elapsed_minutes") is not None:
                add_contributor(
                    "Time Service",
                    "时间",
                    f"{event.get('elapsed_minutes', 0)} min",
                )
            validation_summary = event.get("validation_summary", {})
            if validation_summary:
                add_contributor(
                    "Rule Checker",
                    "规则",
                    validation_summary.get("status", "checked"),
                )
            for item in event.get("npc_agent_outputs", []):
                if not isinstance(item, dict):
                    continue
                add_contributor(
                    item.get("canonical_name") or item.get("character_id"),
                    "Character Agent",
                    item.get("visible_behavior")
                    or item.get("action_intent", {}).get("description", "")
                    or "proposal",
                )
            local_world = event.get("local_world", {})
            group_controller = (
                local_world.get("group_controller", {})
                if isinstance(local_world, dict)
                else {}
            )
            if isinstance(group_controller, dict) and group_controller.get("ran"):
                labels = [
                    clean_text(item.get("label"))
                    for item in group_controller.get("groups", [])
                    if isinstance(item, dict) and clean_text(item.get("label"))
                ]
                add_contributor(
                    "Group Controller",
                    "群体",
                    "、".join(labels[:2]) if labels else "committed",
                )
            if isinstance(local_world, dict) and (
                local_world.get("world_changes")
                or local_world.get("new_events")
                or local_world.get("ambient_npc_reactions")
                or local_world.get("npc_position_updates")
                or event.get("event_type") == "immersive_scene_turn"
            ):
                ambient_count = len(local_world.get("ambient_npc_reactions", []))
                event_count = len(local_world.get("new_events", []))
                status_parts = []
                if ambient_count:
                    status_parts.append(f"ambient NPC x{ambient_count}")
                if event_count:
                    status_parts.append(f"events x{event_count}")
                add_contributor(
                    "Local World Agent",
                    "局部世界",
                    " / ".join(status_parts) if status_parts else "committed",
                )
            gm = event.get("gm_resolution", {})
            if isinstance(gm, dict) and gm:
                add_contributor(
                    "GM Resolver",
                    "裁决",
                    gm.get("outcome", "committed"),
                )
            if event.get("world_projection"):
                add_contributor("Global World Agent", "大世界", "committed")
            if event.get("event_type") == "immersive_scene_turn":
                add_contributor("Memory Agent", "记忆", "recorded")
                add_contributor("Scene Renderer", "叙事", "rendered")
            elif event.get("event_type") == "scene_opening_rendered":
                add_contributor("Scene Renderer", "叙事", "opening rendered")

        return {
            "active_controlled_agents": agent_controlled,
            "active_passive_characters": passive_present,
            "active_full_agents": agent_controlled,
            "active_other_agents": passive_present,
            "turn_contributors": contributors,
            "scene": scene,
            "event_id": event.get("event_id", "") if isinstance(event, dict) else "",
            "revision": event.get("revision_after", "") if isinstance(event, dict) else "",
            "pipeline": pipeline,
        }

    def _render_agent_trace(self, trace):
        scene = trace.get("scene", {})
        lines = []
        lines.append("当前局部场景")
        lines.append(f"地点：{scene.get('location_name') or scene.get('location_id') or '未定位'}")
        lines.append(f"事件：{trace.get('event_id') or '暂无'}")
        if trace.get("revision") != "":
            lines.append(f"Revision：{trace.get('revision')}")
        lines.append("")
        lines.append("独立 Agent 控制")
        controlled_agents = trace.get(
            "active_controlled_agents",
            trace.get("active_full_agents", []),
        )
        if controlled_agents:
            for item in controlled_agents:
                awake = " / 本轮已思考" if item.get("agent_awake") else ""
                lines.append(
                    f"- {item['name']} [{item.get('tier')}/{item['control']}{awake}]"
                )
        else:
            lines.append("- 当前场景没有独立控制中的角色 Agent")
        passive_characters = trace.get(
            "active_passive_characters",
            trace.get("active_other_agents", []),
        )
        if passive_characters:
            lines.append("")
            lines.append("在场但未独立思考")
            for item in passive_characters:
                lines.append(
                    f"- {item['name']} [{item['tier']}/{item['control']}]"
                )
        lines.append("")
        lines.append("本轮完成者")
        contributors = trace.get("turn_contributors", [])
        if contributors:
            for item in contributors:
                status = item.get("status", "")
                suffix = f"：{status}" if status else ""
                lines.append(
                    f"- {item.get('name')} ({item.get('kind')}){suffix}"
                )
        else:
            lines.append("- 等待下一轮运行")
        return "\n".join(lines)

    def _refresh_agent_trace(self, event=None):
        if not hasattr(self, "agent_trace"):
            return
        trace = self._agent_trace_snapshot(event=event)
        self._set_text(self.agent_trace, self._render_agent_trace(trace))

    def _show_latest_story(self, prefer_recovery=False):
        if prefer_recovery:
            recovery = self.store.runtime.get("recovery_snapshot") or {}
            if recovery.get("summary"):
                nearby = recovery.get("nearby_state", {})
                names = [
                    item.get("name")
                    for item in nearby.get("characters", [])
                    if item.get("name")
                ]
                clock = nearby.get("clock", {})
                minute = int(clock.get("minute_of_day", 480))
                text = "\n\n".join(
                    [
                        "上次存档回顾",
                        recovery.get("summary", ""),
                        (
                            f"当前位置：{nearby.get('location_name', '当前位置')}；"
                            f"附近人物：{'、'.join(names) or '暂无明确记录'}；"
                            f"时间：第 {clock.get('day', 1)} 天 "
                            f"{minute // 60:02d}:{minute % 60:02d}"
                        ),
                    ]
                )
                self._set_text(self.story, text)
                self._refresh_agent_trace()
                return
        events = self._story_events()
        text = events[-1].get("narration", "") if events else ""
        if text:
            self._set_text(self.story, text)
        self._refresh_agent_trace(events[-1] if events else None)

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
        self._refresh_agent_trace()

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
            or self.orchestrator.canonical_timeline
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
                    self.save_button.configure(state="normal")
                    self.world_admin_send_button.configure(state="normal")
                    self.progress_value.set(100)
                    self.progress_text.set(
                        "Save complete"
                        if "manual_save" in payload
                        else "Turn complete"
                    )
                    self.eta_text.set("ETA: complete")
                    self.user_input.delete("1.0", "end")
                    self._show_latest_story()
                    self._refresh_runtime_status()
                    self._refresh_agent_trace(payload)
                    pipeline = payload.get("pipeline", payload)
                    self._set_text(
                        self.diagnostics,
                        json.dumps(pipeline, ensure_ascii=False, indent=2),
                    )
                elif event == "operation_error":
                    self.enter_button.configure(state="normal")
                    self.continue_button.configure(state="normal")
                    self.save_button.configure(state="normal")
                    self.world_admin_send_button.configure(state="normal")
                    self.progress_text.set("Operation failed")
                    self.eta_text.set("ETA: stopped")
                    messagebox.showerror("Simulation error", str(payload))
                elif event == "world_admin_done":
                    self.world_admin_send_button.configure(state="normal")
                    self.progress_value.set(100)
                    self.progress_text.set(
                        "World admin applied changes"
                        if payload.get("applied")
                        else "World admin replied"
                    )
                    self.eta_text.set("ETA: complete")
                    reply = payload.get("reply") or payload.get("plot_summary") or ""
                    if payload.get("plot_summary") and payload.get("plot_summary") != reply:
                        reply = reply + "\n\nCurrent plot:\n" + payload["plot_summary"]
                    if payload.get("applied"):
                        reply = reply + "\n\n[Applied runtime admin changes.]"
                    self._append_world_admin("World admin", reply.strip() or "(No reply)")
                    self._refresh_runtime_status()
                    if self.selected_character_id:
                        self._show_character_status(self.selected_character_id)
                    self._set_text(
                        self.diagnostics,
                        json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                elif event == "world_admin_error":
                    self.world_admin_send_button.configure(state="normal")
                    self.progress_text.set("World admin failed")
                    self.eta_text.set("ETA: stopped")
                    messagebox.showerror("World admin error", str(payload), parent=self.root)
                elif event == "preview":
                    self.progress_text.set("Preview ready")
                    self.eta_text.set("ETA: --")
                    self._show_text_window(payload["title"], payload["text"])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _show_text_window(self, title, text):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("780x640")
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        output = tk.Text(frame, wrap="word")
        scroll = ttk.Scrollbar(frame, command=output.yview)
        output.configure(yscrollcommand=scroll.set)
        output.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        output.insert("1.0", text)
        output.configure(state="disabled")
        ttk.Button(frame, text="Close", command=window.destroy).grid(
            row=1, column=0, columnspan=2, pady=(8, 0), sticky="e"
        )


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
