"""Project picker for the unchanged novel simulation executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from app_files import APP_DIR, GENERATED_DB_DIR, SIMULATION_REQUIRED_FILES, file_status
from scenario_library import list_projects, project_required_files


def current_novel_label():
    character_path = GENERATED_DB_DIR / "canonical" / "canonical_character_db.json"
    try:
        data = json.loads(character_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "当前小说数据库"
    names = [
        str(item.get("canonical_name") or "").strip()
        for item in data.get("characters", [])
        if str(item.get("canonical_name") or "").strip()
    ][:4]
    return "当前小说数据库" + (f"（{'、'.join(names)}…）" if names else "")


class ProjectLauncher:
    def __init__(self, root):
        self.root = root
        self.root.title("Story2World - 模拟项目选择器")
        self.root.geometry("760x520")
        self.root.minsize(620, 420)
        self.rows = []

        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        ttk.Label(
            frame,
            text="选择要运行的 Project",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text=(
                "小说数据库和设定项目使用同一个固定模拟器；"
                "设定项目 只通过数据库适配器接入。"
            ),
            foreground="#666666",
        ).grid(row=1, column=0, sticky="w", pady=(4, 10))

        self.project_list = tk.Listbox(
            frame,
            exportselection=False,
            font=("Microsoft YaHei UI", 11),
        )
        self.project_list.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=self.project_list.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.project_list.configure(yscrollcommand=scroll.set)
        self.project_list.bind("<<ListboxSelect>>", self._show_detail)
        self.project_list.bind("<Double-Button-1>", lambda _event: self.launch())

        self.detail = ttk.Label(
            frame,
            text="",
            justify="left",
            wraplength=690,
        )
        self.detail.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(
            actions, text="用主 Agent 新建模拟", command=self.open_studio
        ).pack(side="left")
        ttk.Button(actions, text="刷新", command=self.refresh).pack(
            side="left", padx=6
        )
        ttk.Button(actions, text="取消", command=root.destroy).pack(side="right")
        ttk.Button(actions, text="运行所选 Project", command=self.launch).pack(
            side="right", padx=(0, 6)
        )
        self.refresh()

    def refresh(self):
        self.rows = [
            {
                "kind": "novel",
                "name": current_novel_label(),
                "db_root": str(GENERATED_DB_DIR),
            }
        ]
        self.rows.extend(
            {
                **item,
                "kind": "scenario",
            }
            for item in list_projects()
        )
        self.project_list.delete(0, "end")
        for item in self.rows:
            kind = "小说" if item["kind"] == "novel" else "设定项目"
            self.project_list.insert("end", f"{item['name']}    [{kind}]")
        if self.rows:
            self.project_list.selection_set(0)
            self._show_detail()

    def _selected(self):
        selected = self.project_list.curselection()
        return self.rows[selected[0]] if selected else None

    def _show_detail(self, _event=None):
        item = self._selected()
        if not item:
            self.detail.configure(text="")
            return
        if item["kind"] == "novel":
            checks = file_status(SIMULATION_REQUIRED_FILES)
            missing = [row["name"] for row in checks if not row["exists"]]
            text = (
                "来源：小说准备流程生成的当前 db\n"
                f"状态：{'可运行' if not missing else '缺少 ' + ', '.join(missing)}"
            )
        else:
            checks = project_required_files(item["db_root"])
            missing = [row["name"] for row in checks if not row["exists"]]
            package = item.get("scenario_package", {})
            text = (
                f"设定：{item.get('template_name') or '未知'}\n"
                f"角色 Agent：{len(package.get('characters', []))}\n"
                f"状态：{'可运行' if not missing else '缺少 ' + ', '.join(missing)}"
            )
        self.detail.configure(text=text)

    def launch(self):
        item = self._selected()
        if not item:
            messagebox.showinfo("运行 Project", "请选择一个 Project。", parent=self.root)
            return
        if item["kind"] == "novel":
            missing = [
                row for row in file_status(SIMULATION_REQUIRED_FILES) if not row["exists"]
            ]
            script = APP_DIR / "simulation_ui.py"
            env = dict(os.environ)
        else:
            missing = [
                row
                for row in project_required_files(item["db_root"])
                if not row["exists"]
            ]
            script = APP_DIR / "scenario_project_runner.py"
            env = dict(os.environ)
            env["NOVEL_PROJECT_DIR"] = item["project_root"]
        if missing:
            messagebox.showerror(
                "运行 Project",
                "所选 Project 数据不完整：\n"
                + "\n".join(row["name"] for row in missing),
                parent=self.root,
            )
            return
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(APP_DIR),
            env=env,
        )
        self.root.destroy()

    def open_studio(self):
        subprocess.Popen(
            [sys.executable, str(APP_DIR / "scenario_studio.py")],
            cwd=str(APP_DIR),
            env=dict(os.environ),
        )


def main():
    root = tk.Tk()
    ProjectLauncher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
