from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from .favorites import (
    add_favorite,
    create_folder,
    favorites_snapshot,
    mark_favorite_read,
    move_favorite,
    post_to_favorite_payload,
    refresh_favorites,
    remove_favorite,
    rename_folder,
)
from .posts import normalize_post


class FavoritesWindow(tk.Toplevel):
    """Desktop view over the same favorites.json used by generated reports."""

    def __init__(self, master: tk.Misc, client_provider: Callable[[], Any]) -> None:
        super().__init__(master)
        self.client_provider = client_provider
        self._snapshot: dict[str, Any] = {"folders": [], "items": []}
        self._folders: list[dict[str, str]] = []
        self._busy = False
        self._poll_id: str | None = None
        self.title("北大树洞收藏夹")
        self.geometry("900x680")
        self.minsize(760, 520)
        self.configure(background="#f4f7fa")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self.refresh_view(preserve_open=False)
        self._schedule_poll()

    def _build(self) -> None:
        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)

        title_row = ttk.Frame(main)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="★ 收藏夹", style="Title.TLabel").pack(side="left")
        self.unread_label = ttk.Label(title_row, text="", foreground="#d73535")
        self.unread_label.pack(side="left", padx=9)
        ttk.Label(
            main,
            text="与所有日报共用同一份本地数据；双击帖子可查看原帖与全部评论。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 13))

        add_box = ttk.LabelFrame(main, text="通过洞号添加", padding=10)
        add_box.pack(fill="x")
        ttk.Label(add_box, text="洞号").grid(row=0, column=0, padx=(0, 6), sticky="w")
        self.pid_value = tk.StringVar()
        self.pid_entry = ttk.Entry(add_box, textvariable=self.pid_value, width=22)
        self.pid_entry.grid(row=0, column=1, sticky="ew")
        self.pid_entry.bind("<Return>", lambda _event: self.add_by_pid())
        ttk.Label(add_box, text="加入").grid(row=0, column=2, padx=(12, 6))
        self.add_folder = ttk.Combobox(add_box, state="readonly", width=20)
        self.add_folder.grid(row=0, column=3, sticky="ew")
        self.add_button = ttk.Button(add_box, text="读取并收藏", command=self.add_by_pid)
        self.add_button.grid(row=0, column=4, padx=(10, 0))
        add_box.columnconfigure(1, weight=1)
        add_box.columnconfigure(3, weight=1)

        tools = ttk.Frame(main, padding=(0, 10, 0, 8))
        tools.pack(fill="x")
        ttk.Button(tools, text="新建收藏夹", command=self.new_folder).pack(side="left")
        ttk.Button(tools, text="重命名收藏夹", command=self.rename_selected_folder).pack(
            side="left", padx=7
        )
        self.refresh_button = ttk.Button(tools, text="从树洞刷新全部收藏", command=self.refresh_online)
        self.refresh_button.pack(side="left")
        ttk.Label(tools, text="收藏夹默认折叠，点击左侧箭头展开。", style="Hint.TLabel").pack(
            side="right"
        )

        tree_frame = ttk.Frame(main)
        tree_frame.pack(fill="both", expand=True)
        columns = ("summary", "replies", "favorites", "likes", "new")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="收藏夹 / 洞号")
        self.tree.heading("summary", text="内容概述")
        self.tree.heading("replies", text="评论")
        self.tree.heading("favorites", text="收藏")
        self.tree.heading("likes", text="点赞")
        self.tree.heading("new", text="新消息")
        self.tree.column("#0", width=190, minwidth=150)
        self.tree.column("summary", width=390, minwidth=220)
        self.tree.column("replies", width=58, anchor="center", stretch=False)
        self.tree.column("favorites", width=58, anchor="center", stretch=False)
        self.tree.column("likes", width=58, anchor="center", stretch=False)
        self.tree.column("new", width=65, anchor="center", stretch=False)
        self.tree.tag_configure("unread", background="#fff0f0", foreground="#8e2020")
        self.tree.tag_configure("folder", background="#eaf3f8", foreground="#244f6b")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self.open_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._sync_move_folder())

        actions = ttk.Frame(main, padding=(0, 9, 0, 0))
        actions.pack(fill="x")
        ttk.Button(actions, text="查看帖子与评论", command=self.open_selected).pack(side="left")
        ttk.Label(actions, text="移动到").pack(side="left", padx=(14, 6))
        self.move_folder = ttk.Combobox(actions, state="readonly", width=20)
        self.move_folder.pack(side="left")
        ttk.Button(actions, text="移动", command=self.move_selected).pack(side="left", padx=7)
        ttk.Button(actions, text="移出收藏夹", command=self.remove_selected).pack(side="left")
        self.status = tk.StringVar(value="收藏数据已加载。")
        ttk.Label(actions, textvariable=self.status, foreground="#496a7f").pack(side="right")

    def _close(self) -> None:
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
        self.destroy()

    def _schedule_poll(self) -> None:
        if self.winfo_exists():
            if not self._busy:
                self.refresh_view(preserve_open=True, quiet=True)
            self._poll_id = self.after(3000, self._schedule_poll)

    def _folder_values(self) -> list[str]:
        return [folder["name"] for folder in self._folders]

    def _folder_id(self, combo: ttk.Combobox) -> str:
        index = combo.current()
        if index < 0 or index >= len(self._folders):
            if not self._folders:
                raise ValueError("当前没有可用的收藏夹。")
            index = 0
        return self._folders[index]["id"]

    def _selected_pid(self) -> str | None:
        selected = self.tree.selection()
        if not selected or not selected[0].startswith("post:"):
            return None
        return selected[0][5:]

    def _selected_folder_id(self) -> str | None:
        selected = self.tree.selection()
        if not selected:
            return None
        iid = selected[0]
        if iid.startswith("folder:"):
            return iid[7:]
        parent = self.tree.parent(iid)
        return parent[7:] if parent.startswith("folder:") else None

    def _item(self, pid: str) -> dict[str, Any] | None:
        return next(
            (item for item in self._snapshot.get("items", []) if str(item.get("pid")) == str(pid)),
            None,
        )

    def refresh_view(self, preserve_open: bool = True, quiet: bool = False) -> None:
        open_folders: set[str] = set()
        selected = self.tree.selection()
        if preserve_open:
            for iid in self.tree.get_children(""):
                if self.tree.item(iid, "open"):
                    open_folders.add(iid[7:])
        self._snapshot = favorites_snapshot()
        self._folders = list(self._snapshot.get("folders", []))
        values = self._folder_values()
        previous_add = self.add_folder.current()
        previous_move = self.move_folder.current()
        self.add_folder.configure(values=values)
        self.move_folder.configure(values=values)
        if values:
            self.add_folder.current(previous_add if 0 <= previous_add < len(values) else 0)
            self.move_folder.current(previous_move if 0 <= previous_move < len(values) else 0)

        self.tree.delete(*self.tree.get_children(""))
        items = list(self._snapshot.get("items", []))
        unread_count = sum(1 for item in items if item.get("unread"))
        self.unread_label.configure(text=f"● {unread_count} 条有新回复" if unread_count else "")
        for folder in self._folders:
            folder_id = folder["id"]
            folder_items = [item for item in items if item.get("folder_id") == folder_id]
            has_unread = any(item.get("unread") for item in folder_items)
            folder_text = f"{'● ' if has_unread else ''}{folder['name']}（{len(folder_items)}）"
            parent = self.tree.insert(
                "",
                "end",
                iid=f"folder:{folder_id}",
                text=folder_text,
                values=("", "", "", "", ""),
                tags=("folder",),
                open=folder_id in open_folders,
            )
            for item in folder_items:
                post = item.get("post", {})
                summary = str(post.get("summary") or post.get("text") or "暂无概述")
                if len(summary) > 90:
                    summary = summary[:89] + "…"
                new_replies = int(item.get("new_replies", 0) or 0)
                self.tree.insert(
                    parent,
                    "end",
                    iid=f"post:{item['pid']}",
                    text=str(item["pid"]),
                    values=(
                        summary,
                        int(post.get("replies", 0) or 0),
                        int(post.get("favorites", 0) or 0),
                        int(post.get("likes", 0) or 0),
                        f"+{new_replies}" if new_replies else "",
                    ),
                    tags=("unread",) if item.get("unread") else (),
                )
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        if not quiet:
            self.status.set(f"共 {len(items)} 条收藏；HTML 日报会自动同步。")

    def _run_background(self, label: str, operation: Callable[[], Any], success: str) -> None:
        if self._busy:
            messagebox.showinfo("操作进行中", "请等待当前收藏操作完成。", parent=self)
            return
        self._busy = True
        self.add_button.configure(state="disabled")
        self.refresh_button.configure(state="disabled")
        self.status.set(label)

        def finish(error: Exception | None = None) -> None:
            self._busy = False
            self.add_button.configure(state="normal")
            self.refresh_button.configure(state="normal")
            if error is not None:
                self.status.set(f"失败：{error}")
                messagebox.showerror("收藏操作失败", str(error), parent=self)
            else:
                self.refresh_view(preserve_open=True)
                self.status.set(success)

        def worker() -> None:
            try:
                operation()
            except Exception as exc:
                self.after(0, finish, exc)
            else:
                self.after(0, finish, None)

        threading.Thread(target=worker, daemon=True).start()

    def add_by_pid(self) -> None:
        pid = self.pid_value.get().strip()
        if not pid.isdigit():
            messagebox.showerror("洞号格式错误", "请输入纯数字洞号。", parent=self)
            return
        try:
            folder_id = self._folder_id(self.add_folder)
        except ValueError as exc:
            messagebox.showerror("无法添加", str(exc), parent=self)
            return

        def operation() -> None:
            client = self.client_provider()
            detail = client.post_detail(pid)
            post = normalize_post(detail) if detail else None
            if post is None:
                raise RuntimeError(f"没有找到洞号 {pid}，或该帖已不可访问。")
            client.enrich_post(post, 50)
            add_favorite(pid, folder_id, post_to_favorite_payload(post))

        self._run_background(
            f"正在读取洞号 {pid} 的原帖和全部评论……",
            operation,
            f"洞号 {pid} 已加入收藏夹。",
        )
        self.pid_value.set("")

    def refresh_online(self) -> None:
        self._run_background(
            "正在按洞号刷新全部收藏帖……",
            lambda: refresh_favorites(self.client_provider()),
            "全部收藏帖已刷新；HTML 日报将在几秒内同步。",
        )

    def new_folder(self) -> None:
        name = simpledialog.askstring("新建收藏夹", "收藏夹名称：", parent=self)
        if not name:
            return
        try:
            create_folder(name)
            self.refresh_view(preserve_open=True)
        except ValueError as exc:
            messagebox.showerror("新建失败", str(exc), parent=self)

    def rename_selected_folder(self) -> None:
        folder_id = self._selected_folder_id()
        if folder_id is None:
            try:
                folder_id = self._folder_id(self.add_folder)
            except ValueError as exc:
                messagebox.showerror("无法重命名", str(exc), parent=self)
                return
        folder = next((item for item in self._folders if item["id"] == folder_id), None)
        if folder is None:
            return
        name = simpledialog.askstring(
            "重命名收藏夹", "新的名称：", initialvalue=folder["name"], parent=self
        )
        if not name or name == folder["name"]:
            return
        try:
            rename_folder(folder_id, name)
            self.refresh_view(preserve_open=True)
        except ValueError as exc:
            messagebox.showerror("重命名失败", str(exc), parent=self)

    def _sync_move_folder(self) -> None:
        folder_id = self._selected_folder_id()
        for index, folder in enumerate(self._folders):
            if folder["id"] == folder_id:
                self.move_folder.current(index)
                break

    def move_selected(self) -> None:
        pid = self._selected_pid()
        if pid is None:
            messagebox.showinfo("请选择帖子", "请先选择一条收藏帖子。", parent=self)
            return
        try:
            move_favorite(pid, self._folder_id(self.move_folder))
            self.refresh_view(preserve_open=True)
        except ValueError as exc:
            messagebox.showerror("移动失败", str(exc), parent=self)

    def remove_selected(self) -> None:
        pid = self._selected_pid()
        if pid is None:
            messagebox.showinfo("请选择帖子", "请先选择一条收藏帖子。", parent=self)
            return
        if not messagebox.askyesno("移出收藏夹", f"确定移出洞号 {pid}？", parent=self):
            return
        remove_favorite(pid)
        self.refresh_view(preserve_open=True)

    def open_selected(self) -> None:
        pid = self._selected_pid()
        if pid is None:
            return
        item = self._item(pid)
        if item is None:
            return
        self._show_post(item)
        mark_favorite_read(pid)
        self.refresh_view(preserve_open=True, quiet=True)

    def _show_post(self, item: dict[str, Any]) -> None:
        pid = str(item.get("pid", ""))
        post = item.get("post", {})
        dialog = tk.Toplevel(self)
        dialog.title(f"洞号 {pid} · 收藏帖")
        dialog.geometry("760x650")
        dialog.minsize(620, 460)
        dialog.transient(self)
        text = ScrolledText(
            dialog,
            wrap="word",
            padx=20,
            pady=18,
            font=("Microsoft YaHei UI", 10),
            background="#fbfdff",
            relief="flat",
        )
        text.pack(fill="both", expand=True)
        text.tag_configure("heading", font=("Microsoft YaHei UI", 16, "bold"), foreground="#174d73")
        text.tag_configure("section", font=("Microsoft YaHei UI", 11, "bold"), foreground="#285f85")
        text.tag_configure("meta", foreground="#6b7d89")
        text.tag_configure("original", background="#eaf5fb", lmargin1=12, lmargin2=12, rmargin=12, spacing1=8, spacing3=8)
        text.tag_configure("quote", background="#edf1f4", foreground="#4f6573", lmargin1=24, lmargin2=24, rmargin=24)
        text.insert("end", f"洞号 {pid}\n", "heading")
        text.insert(
            "end",
            f"评论 {post.get('replies', 0)}  ·  收藏 {post.get('favorites', 0)}  ·  点赞 {post.get('likes', 0)}\n\n",
            "meta",
        )
        text.insert("end", "原帖\n", "section")
        text.insert("end", f"{post.get('text', '')}\n\n", "original")
        comments = post.get("comments", []) if isinstance(post.get("comments", []), list) else []
        text.insert("end", f"全部评论（{len(comments)}）\n", "section")
        if not comments:
            text.insert("end", "当前没有可读取的评论。\n", "meta")
        palette = ("#fff5e8", "#edf7ef", "#eef4ff", "#f8efff", "#fff0f3", "#eef8f8")
        seen = int(item.get("last_seen_replies", 0) or 0)
        for index, comment in enumerate(comments, start=1):
            author = str(comment.get("author") or "匿名")
            tag = f"author_{abs(hash(author)) % len(palette)}"
            text.tag_configure(tag, background=palette[abs(hash(author)) % len(palette)], lmargin1=12, lmargin2=12, rmargin=12, spacing1=8, spacing3=8)
            prefix = "【新回复】" if item.get("unread") and index > seen else ""
            text.insert("end", f"\n{prefix}{author}  #{index}\n", tag)
            if comment.get("quoted_text"):
                text.insert(
                    "end",
                    f"回复 {comment.get('quoted_author') or '匿名'}：{comment.get('quoted_text')}\n",
                    "quote",
                )
            text.insert("end", f"{comment.get('text', '')}\n", tag)
        text.configure(state="disabled")

