"""Adapter that opens a compiled scenario with the unchanged simulation UI."""

from __future__ import annotations

import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import app_files
from scenario_library import project_required_files


def configure_project_paths(db_root):
    """Map a scenario DB to the file contract already used by the simulator."""
    db_root = Path(db_root)
    app_files.GENERATED_DB_DIR = db_root
    app_files.GRAPH_DB_DIR = db_root / "graph"
    app_files.CANONICAL_DB_DIR = db_root / "canonical"
    app_files.AGENT_DB_DIR = db_root / "agents"
    app_files.RUNTIME_DB_DIR = db_root / "runtime"


def main():
    project_root = Path(os.environ.get("NOVEL_PROJECT_DIR", "")).resolve()
    metadata_path = project_root / "project.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Scenario Project",
            f"无法读取 Project：{error}",
            parent=root,
        )
        root.destroy()
        return

    db_root = Path(metadata.get("db_root") or project_root / "db")
    missing = [
        item for item in project_required_files(db_root) if not item["exists"]
    ]
    if missing:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Scenario Project",
            "Project 数据库不完整：\n"
            + "\n".join(item["name"] for item in missing),
            parent=root,
        )
        root.destroy()
        return

    configure_project_paths(db_root)
    # Import after path mapping. simulation_ui and step17_runtime are not
    # modified for scenario behavior; they consume the standard DB contract.
    from simulation_ui import SimulationApp

    root = tk.Tk()
    app = SimulationApp(root)
    if root.winfo_exists():
        root.title(f"Story2World - {metadata.get('name', project_root.name)}")
        root.mainloop()
    return app


if __name__ == "__main__":
    main()
