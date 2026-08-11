from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import yaml

from .auth import (
    delete_login_secret,
    load_deepseek_key,
    load_login_secret,
    login_with_iaaa,
    save_deepseek_key,
    save_login_secret,
)
from .client import AuthenticationError, TreeholeClient
from .cli import command_run
from .config import load_config
from .deepseek_classifier import DeepSeekClassifier
from .favorites import FavoritesServer
from .favorites_gui import FavoritesWindow
from .state import latest_report_path


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "项目代码"
    return Path(__file__).resolve().parents[2]


class DigestWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.code_root = project_root()
        self.config_path = self.code_root / "config.yaml"
        self.config = load_config(self.config_path)
        self.profile_path = (self.code_root / self.config.profile.path).resolve()
        self.report_dir = (self.code_root / self.config.output.directory).resolve()
        self._verification_client: TreeholeClient | None = None
        self._busy = False
        self._favorites_window: FavoritesWindow | None = None
        self._favorites_server = FavoritesServer()
        self._favorites_server.start()
        self.title("北大树洞每日摘要")
        width = min(980, max(780, self.winfo_screenwidth() - 100))
        self.geometry(f"{width}x500")
        self.minsize(760, 450)
        self.configure(background="#f4f7fb")
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"), foreground="#173a5e")
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 9), foreground="#60758a")
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        self._build()
        self._load_saved()
        self.protocol("WM_DELETE_WINDOW", self._close_window)

    def _close_window(self) -> None:
        self._favorites_server.stop()
        self.destroy()

    def _build(self) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="北大树洞每日摘要", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            main,
            text="按时间增量读取 · DeepSeek 分类 · 本地收藏夹",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        credentials = ttk.LabelFrame(main, text="登录与 API（敏感信息保存在 Windows 凭据管理器）", padding=10)
        credentials.pack(fill="x")
        ttk.Label(credentials, text="北大学号").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.username = tk.StringVar()
        ttk.Entry(credentials, textvariable=self.username, width=28).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(credentials, text="IAAA 密码").grid(row=0, column=2, sticky="w", padx=4)
        self.password = tk.StringVar()
        ttk.Entry(credentials, textvariable=self.password, show="●", width=28).grid(row=0, column=3, sticky="ew", padx=4)
        ttk.Label(credentials, text="DeepSeek API Key").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.deepseek_key = tk.StringVar()
        ttk.Entry(credentials, textvariable=self.deepseek_key, show="●").grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=4
        )
        ttk.Label(credentials, text="短信验证码").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        self.sms_code = tk.StringVar()
        ttk.Entry(credentials, textvariable=self.sms_code, width=28).grid(
            row=2, column=1, sticky="ew", padx=4
        )
        self.send_sms_button = ttk.Button(credentials, text="发送验证码", command=self.send_sms_code)
        self.send_sms_button.grid(row=2, column=2, padx=4)
        self.submit_sms_button = ttk.Button(credentials, text="提交验证", command=self.submit_sms_code)
        self.submit_sms_button.grid(row=2, column=3, padx=4)
        self.save_secrets = tk.BooleanVar(value=True)
        ttk.Checkbutton(credentials, text="保存账号、密码和 API Key", variable=self.save_secrets).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=4, pady=4
        )
        ttk.Button(credentials, text="保存设置", command=self.save_settings).grid(row=3, column=2, padx=4)
        self.test_button = ttk.Button(credentials, text="测试登录与 API", command=self.test_connections)
        self.test_button.grid(row=3, column=3, padx=4)
        credentials.columnconfigure(1, weight=1)
        credentials.columnconfigure(3, weight=1)

        actions = ttk.Frame(main, padding=(0, 12, 0, 0))
        actions.pack(fill="x")
        self.run_button = ttk.Button(
            actions, text="生成今日日报", command=self.generate_report, style="Accent.TButton"
        )
        self.run_button.pack(side="left")
        ttk.Button(actions, text="打开每日报告", command=self.open_reports).pack(side="left", padx=8)
        self.profile_expanded = tk.BooleanVar(value=False)
        self.profile_button = ttk.Button(actions, text="展开个人画像", command=self.toggle_profile)
        self.profile_button.pack(side="left")
        ttk.Button(actions, text="★ 打开收藏夹", command=self.open_favorites).pack(side="left", padx=8)
        ttk.Button(actions, text="添加到开始菜单", command=self.add_to_start_menu).pack(side="right")

        self.profile_frame = ttk.LabelFrame(main, text="个人画像与分类依据（可直接修改）", padding=10)
        self.profile_text = tk.Text(
            self.profile_frame,
            wrap="word",
            undo=True,
            height=12,
            font=("Microsoft YaHei UI", 10),
            relief="flat",
            background="#fbfdff",
        )
        scroll = ttk.Scrollbar(self.profile_frame, orient="vertical", command=self.profile_text.yview)
        self.profile_text.configure(yscrollcommand=scroll.set)
        self.profile_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.status = tk.StringVar(value="请先填写或确认账号、密码、DeepSeek API Key 和个人画像。")
        self.status_label = ttk.Label(
            main, textvariable=self.status, foreground="#174a7e", wraplength=840
        )
        self.status_label.pack(fill="x", pady=(12, 0))

    def toggle_profile(self) -> None:
        expanded = not self.profile_expanded.get()
        self.profile_expanded.set(expanded)
        if expanded:
            self.profile_frame.pack(fill="both", expand=True, pady=(12, 0), before=self.status_label)
            self.profile_button.configure(text="收起个人画像")
            width = self.winfo_width()
            height = min(800, max(650, self.winfo_screenheight() - 100))
            self.geometry(f"{width}x{height}")
        else:
            self.profile_frame.pack_forget()
            self.profile_button.configure(text="展开个人画像")
            self.geometry(f"{self.winfo_width()}x500")

    @staticmethod
    def _vbs_quote(value: str) -> str:
        return value.replace('"', '""')

    def add_to_start_menu(self) -> None:
        try:
            appdata = os.environ.get("APPDATA")
            if not appdata:
                raise RuntimeError("无法确定 Windows 开始菜单目录。")
            app_root = self.code_root.parent
            executable = app_root / "树洞日报.exe"
            if not executable.exists():
                raise RuntimeError("没有找到树洞日报.exe，请先重新构建程序。")
            target = executable
            start_menu = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            start_menu.mkdir(parents=True, exist_ok=True)
            shortcut = start_menu / "北大树洞日报.lnk"
            fd, script_name = tempfile.mkstemp(prefix="pku-treehole-shortcut-", suffix=".vbs")
            os.close(fd)
            script_path = Path(script_name)
            vbs = (
                'Set shell = CreateObject("WScript.Shell")\n'
                f'Set link = shell.CreateShortcut("{self._vbs_quote(str(shortcut))}")\n'
                f'link.TargetPath = "{self._vbs_quote(str(target))}"\n'
                f'link.WorkingDirectory = "{self._vbs_quote(str(app_root))}"\n'
                f'link.IconLocation = "{self._vbs_quote(str(executable))},0"\n'
                'link.Description = "北大树洞每日摘要"\n'
                "link.Save\n"
            )
            try:
                script_path.write_text(vbs, encoding="utf-8-sig")
                subprocess.run(
                    ["cscript.exe", "//nologo", str(script_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            finally:
                script_path.unlink(missing_ok=True)
            messagebox.showinfo(
                "已添加到开始菜单",
                "“北大树洞日报”已加入开始菜单。若要固定，请在开始菜单中搜索它，右键选择“固定到开始屏幕”。",
            )
        except Exception as exc:
            messagebox.showerror("添加失败", str(exc))

    def _client_for_favorites(self) -> TreeholeClient:
        from .auth import Credentials

        if Credentials.load() is None and (
            not self.username.get().strip() or not self.password.get()
        ):
            raise AuthenticationError("请先在主界面填写北大学号和 IAAA 密码并完成登录。")
        return self._saved_or_new_client(self.username.get().strip(), self.password.get())

    def open_favorites(self) -> None:
        if self._favorites_window is not None and self._favorites_window.winfo_exists():
            self._favorites_window.deiconify()
            self._favorites_window.lift()
            self._favorites_window.focus_force()
            return
        self._favorites_window = FavoritesWindow(self, self._client_for_favorites)

    def _load_saved(self) -> None:
        saved = load_login_secret()
        if saved:
            self.username.set(saved[0])
            self.password.set(saved[1])
        key = load_deepseek_key()
        if key:
            self.deepseek_key.set(key)
        if self.profile_path.exists():
            self.profile_text.insert("1.0", self.profile_path.read_text(encoding="utf-8"))

    def _profile_value(self) -> str:
        value = self.profile_text.get("1.0", "end").strip() + "\n"
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError as exc:
            raise ValueError(f"个人画像 YAML 格式错误：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("个人画像必须是 YAML 对象。")
        return value

    def save_settings(self) -> bool:
        try:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            self.profile_path.write_text(self._profile_value(), encoding="utf-8")
            if self.save_secrets.get():
                if not self.username.get().strip() or not self.password.get():
                    raise ValueError("请填写北大学号和 IAAA 密码。")
                if not self.deepseek_key.get().strip():
                    raise ValueError("请填写 DeepSeek API Key。")
                save_login_secret(self.username.get().strip(), self.password.get())
                save_deepseek_key(self.deepseek_key.get().strip())
            else:
                delete_login_secret()
            self.status.set("设置已保存。密码和 API Key 未写入项目文件。")
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return False

    def _background(self, label: str, operation) -> None:
        if self._busy:
            messagebox.showinfo("操作进行中", "请等待当前操作完成，不要重复点击。")
            return
        self._busy = True
        operation_buttons = (
            self.run_button,
            self.test_button,
            self.send_sms_button,
            self.submit_sms_button,
        )
        for button in operation_buttons:
            button.configure(state="disabled")
        self.status.set(label)

        def finish(result: str | None = None, error: Exception | None = None) -> None:
            if error is not None:
                messagebox.showerror("操作失败", str(error))
                self.status.set(f"失败：{error}")
            elif result is not None:
                self.status.set(result)
            self._busy = False
            for button in operation_buttons:
                button.configure(state="normal")

        def worker() -> None:
            try:
                result = operation()
            except Exception as exc:
                self.after(0, finish, None, exc)
            else:
                self.after(0, finish, str(result), None)

        threading.Thread(target=worker, daemon=True).start()

    def _saved_or_new_client(self, username: str, password: str) -> TreeholeClient:
        from .auth import Credentials

        credentials = Credentials.load()
        if credentials is not None:
            client = TreeholeClient(credentials)
            try:
                client.feed_page(page=1, limit=1, comment_limit=1)
            except AuthenticationError as exc:
                if "已过期" not in str(exc):
                    return client
            else:
                return client
        credentials = login_with_iaaa(username, password)
        credentials.save()
        return TreeholeClient(credentials)

    def test_connections(self) -> None:
        if not self.save_settings():
            return
        username = self.username.get().strip()
        password = self.password.get()
        api_key = self.deepseek_key.get().strip()

        def operation() -> str:
            classifier = DeepSeekClassifier(api_key, self.config.deepseek)
            classifier.verify()
            client = self._saved_or_new_client(username, password)
            self._verification_client = client
            try:
                client.feed_page(page=1, limit=1, comment_limit=1)
            except AuthenticationError as exc:
                if "验证" in str(exc):
                    raise AuthenticationError(
                        "北大账号和 DeepSeek API 均正常，但树洞要求手机短信验证。"
                        "请点击“发送验证码”，收到短信后填写并点击“提交验证”。"
                    ) from exc
                raise
            return "树洞登录和 DeepSeek API 均连接成功。"

        self._background("正在测试树洞登录与 DeepSeek API……", operation)

    def send_sms_code(self) -> None:
        if not self.save_settings():
            return
        username = self.username.get().strip()
        password = self.password.get()

        def operation() -> str:
            client = self._saved_or_new_client(username, password)
            if not client.needs_sms_verification():
                self._verification_client = client
                return "当前设备的树洞会话已经验证，可以直接生成日报，无需再次发送验证码。"
            client.send_sms_code()
            self._verification_client = client
            return "短信验证码已发送到北大账号绑定的手机，请在 5 分钟内填写并提交。"

        self._background("正在请求树洞发送短信验证码……", operation)

    def submit_sms_code(self) -> None:
        code = self.sms_code.get().strip()
        if not code:
            messagebox.showerror("缺少验证码", "请输入收到的短信验证码。")
            return

        def operation() -> str:
            client = self._verification_client
            if client is None:
                from .auth import Credentials

                credentials = Credentials.load()
                if credentials is None:
                    raise AuthenticationError("没有可验证的树洞会话，请先点击“发送验证码”。")
                client = TreeholeClient(credentials)
            client.verify_sms_code(code)
            self._verification_client = client
            return "短信验证成功。现在可以直接点击“生成今日日报”。"

        self._background("正在提交短信验证码（通常几秒内完成）……", operation)

    def generate_report(self) -> None:
        if not self.save_settings():
            return

        def progress(page: int, count: int) -> None:
            if page == -2:
                message = "正在按洞号刷新收藏夹帖子……"
            elif page == -3:
                message = f"收藏夹已刷新 {count} 条，正在读取本次时间范围……"
            elif page < 0:
                message = f"已按时间读取 {count} 条帖子，正在调用 DeepSeek 分类……"
            else:
                message = f"正在按时间读取树洞：已读取 {page} 页、{count} 条……"
            self.after(0, self.status.set, message)

        def operation() -> str:
            args = argparse.Namespace(config=str(self.config_path), progress=progress)
            command_run(args)
            latest = latest_report_path(self.report_dir)
            return f"日报已生成：{latest}" if latest else "日报生成完成。"

        self._background("正在确定上次报告时间；首次运行会回溯 24 小时……", operation)

    def open_reports(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        latest = latest_report_path(self.report_dir)
        os.startfile(latest if latest is not None else self.report_dir)

def main() -> None:
    DigestWindow().mainloop()


if __name__ == "__main__":
    main()
