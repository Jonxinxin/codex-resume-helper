"""Presentation for the local Codex resume helper."""
from __future__ import annotations

import json
import time
from pathlib import PureWindowsPath

import customtkinter as ctk


BG = "#F4F6F5"
WHITE = "#FFFFFF"
TEXT = "#202C29"
MUTED = "#7A8781"
LINE = "#E5EAE7"
GREEN = "#237A60"
GREEN_HOVER = "#1A624C"
TINT = "#EDF5F0"
FONT = "Microsoft YaHei UI"


def row_key(row):
    return (row.get("window_id"), row.get("pane_id"), row.get("agent", {}).get("session_id"))


class ResumeView(ctk.CTk):
    def font(self, size=13, bold=False):
        return ctk.CTkFont(family=FONT, size=size, weight="bold" if bold else "normal")

    def label(self, parent, text="", size=13, color=TEXT, bold=False, **kwargs):
        return ctk.CTkLabel(parent, text=text, font=self.font(size, bold),
                            text_color=color, anchor="w", **kwargs)

    def button(self, parent, text, command, primary=False, **kwargs):
        return ctk.CTkButton(
            parent, text=text, command=command, font=self.font(13, primary), height=36,
            corner_radius=8, border_width=0 if primary else 1,
            border_color=LINE, fg_color=GREEN if primary else WHITE,
            hover_color=GREEN_HOVER if primary else "#F0F3F1",
            text_color=WHITE if primary else TEXT, text_color_disabled="#A2AEA8",
            **kwargs)

    def card(self, parent, **kwargs):
        return ctk.CTkFrame(parent, fg_color=WHITE, corner_radius=14,
                            border_width=1, border_color=LINE, **kwargs)

    def build_ui(self):
        self.selected_key = None
        self.row_widgets = {}
        self.refreshing = False
        self.binding_pending = False
        self.attempts = 0
        self.log_started = False
        self.activity_tab = "监测日志"
        self.configure(fg_color=BG)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.grid(row=0, column=0, sticky="nsew", padx=28, pady=(22, 14))
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(2, weight=3, minsize=334)
        root.grid_rowconfigure(3, weight=2, minsize=160)

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        mark = ctk.CTkFrame(header, width=44, height=44, corner_radius=13, fg_color=GREEN)
        mark.pack(side="left", padx=(0, 13))
        mark.pack_propagate(False)
        ctk.CTkLabel(mark, text=">_", font=ctk.CTkFont("Consolas", 22, "bold"),
                     text_color=WHITE).pack(expand=True)
        heading = ctk.CTkFrame(header, fg_color="transparent")
        heading.pack(side="left")
        self.label(heading, "Codex 续接助手", size=22, bold=True, height=28).pack(anchor="w")
        self.label(heading, "临时断线后，让任务继续。", size=12, color=MUTED, height=22).pack(anchor="w")
        self.connection_badge = self.label(header, "●  正在连接 Pebrel", size=12, color=MUTED)
        self.connection_badge.pack(side="right", padx=(16, 0))
        self.choose_button = self.button(header, "连接设置", self.choose_cli, width=88)
        self.choose_button.pack(side="right")

        hero = ctk.CTkFrame(root, fg_color=TINT, corner_radius=14, border_width=1, border_color="#DCEADF")
        self.status_card = hero
        hero.grid(row=1, column=0, sticky="ew", pady=(0, 18))
        hero.grid_columnconfigure(1, weight=1)
        self.state_icon = self.label(hero, "○", size=26, color=GREEN, width=34)
        self.state_icon.grid(row=0, column=0, rowspan=2, padx=(22, 12), pady=22)
        self.state_heading = self.label(hero, "准备续接", size=18, bold=True, height=28)
        self.state_heading.grid(row=0, column=1, sticky="sw", pady=(19, 0))
        self.status_label = self.label(hero, textvariable=self.status_var, size=12, color="#66796E", height=26,
                                      wraplength=540, justify="left")
        self.status_label.grid(row=1, column=1, sticky="new", pady=(0, 19), padx=(0, 14))
        self.status_label.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(220, e.width)))
        self.counter_label = self.label(hero, textvariable=self.counter_var, size=12, color=GREEN)
        self.counter_label.grid(row=0, column=2, rowspan=2, padx=(4, 24))
        self.start_button = self.button(hero, "开始监测  →", self.toggle_monitor, primary=True, width=144)
        self.start_button.grid(row=0, column=3, rowspan=2, padx=(0, 24))

        body = ctk.CTkFrame(root, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", pady=(0, 18))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, minsize=300)
        body.grid_rowconfigure(0, weight=1)

        tasks = self.card(body)
        tasks.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        tasks.grid_columnconfigure(0, weight=1)
        tasks.grid_rowconfigure(2, weight=1)
        task_header = ctk.CTkFrame(tasks, fg_color="transparent")
        task_header.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 9))
        self.label(task_header, "选择任务", size=15, bold=True).pack(side="left")
        self.task_count = self.label(task_header, "0 个窗格", size=12, color=MUTED)
        self.task_count.pack(side="left", padx=10)
        self.refresh_button = self.button(task_header, "↻  刷新", self.refresh, width=76)
        self.refresh_button.pack(side="right")
        search_wrap = ctk.CTkFrame(tasks, fg_color="#F5F7F6", corner_radius=8, height=36)
        search_wrap.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))
        search_wrap.grid_columnconfigure(1, weight=1)
        self.label(search_wrap, "搜索", size=12, color=MUTED).grid(row=0, column=0, padx=(12, 6))
        self.search_entry = ctk.CTkEntry(search_wrap, textvariable=self.search,
                                        font=self.font(12), height=36, border_width=0,
                                        fg_color="#F5F7F6", text_color=TEXT)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self.task_list = ctk.CTkScrollableFrame(tasks, fg_color=WHITE, corner_radius=0,
                                               scrollbar_button_color=LINE,
                                               scrollbar_button_hover_color="#B9C8BE", height=110)
        self.task_list.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 6))
        self.task_list.grid_columnconfigure(0, weight=1)

        target = ctk.CTkFrame(tasks, fg_color="transparent")
        target.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 14))
        target.grid_columnconfigure(0, weight=1)
        ctk.CTkFrame(target, fg_color=LINE, height=1).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.target_label = self.label(target, textvariable=self.target_var, size=12, color=MUTED,
                                       wraplength=330, justify="left")
        self.target_label.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.target_label.bind("<Configure>", lambda e: self.target_label.configure(wraplength=max(150, e.width)))
        self.bind_button = self.button(target, "绑定任务", self.bind_selected, width=88)
        self.bind_button.grid(row=1, column=1, sticky="e")
        self.details_toggle = ctk.CTkButton(target, text="会话详情  ↗", width=84, height=22,
                                          font=self.font(11), fg_color="transparent", hover_color=BG,
                                          text_color=MUTED, anchor="w", command=self.toggle_details)
        self.details_toggle.grid(row=2, column=0, sticky="w", pady=(3, 0))
        self.details_box = None
        self.details_window = None

        settings = self.card(body, width=300)
        settings.grid(row=0, column=1, sticky="nsew")
        settings.grid_columnconfigure(0, weight=1)
        self.label(settings, "续接设置", size=15, bold=True).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 8))
        self.label(settings, "恢复时发送的消息", size=12, color=MUTED).grid(row=1, column=0, sticky="w", padx=20)
        self.message_entry = ctk.CTkEntry(settings, textvariable=self.message_var, height=36,
                                         fg_color="#FAFBFA", border_color=LINE, corner_radius=8,
                                         font=self.font(), text_color=TEXT)
        self.message_entry.grid(row=2, column=0, sticky="ew", padx=20, pady=(3, 12))
        self.settings_widgets = [self.message_entry]
        self.step_buttons = []
        for i, (text, unit, var, low, high) in enumerate((
                ("检查间隔", "秒", self.interval_var, 3, 120),
                ("首次等待", "秒", self.delay_var, 10, 300),
                ("重试上限", "次", self.limit_var, 1, 20))):
            line = ctk.CTkFrame(settings, fg_color="transparent")
            line.grid(row=3 + i, column=0, sticky="ew", padx=20, pady=5)
            self.label(line, text, size=12).pack(side="left")
            self.label(line, unit, size=12, color=MUTED, width=16).pack(side="right", padx=(7, 0))
            stepper = ctk.CTkFrame(line, fg_color="#F5F7F6", corner_radius=7)
            stepper.pack(side="right")
            for symbol, delta in (("−", -1), ("+", 1)):
                button = ctk.CTkButton(stepper, text=symbol, width=27, height=31, corner_radius=7,
                                       fg_color="transparent", hover_color=LINE, text_color=MUTED,
                                       font=self.font(15), command=lambda v=var, d=delta, lo=low, hi=high: self.step(v, d, lo, hi))
                button.pack(side="left" if delta == -1 else "right")
                self.step_buttons.append(button)
            entry = ctk.CTkEntry(stepper, textvariable=var, width=43, height=31, justify="center",
                                 border_width=0, fg_color="#F5F7F6", text_color=TEXT, font=self.font(13))
            entry.pack(side="left")
            self.settings_widgets.append(entry)
        self.label(settings, "故障按“首次等待”递增退避，最长等 5 分钟。\n报错给出等待时间时以它为准。", size=11,
                   color=MUTED, justify="left", height=38).grid(
            row=6, column=0, sticky="w", padx=20, pady=(12, 12))

        activity = self.card(root)
        activity.grid(row=3, column=0, sticky="nsew")
        activity.grid_columnconfigure(0, weight=1)
        activity.grid_rowconfigure(1, weight=1)
        activity_header = ctk.CTkFrame(activity, fg_color="transparent")
        activity_header.grid(row=0, column=0, sticky="ew", padx=20, pady=(11, 6))
        self.tab_buttons = {}
        for name in ("监测日志", "窗格预览"):
            btn = ctk.CTkButton(activity_header, text=name, width=92, height=32, corner_radius=7,
                                font=self.font(12), fg_color=TINT if name == self.activity_tab else "transparent",
                                hover_color=BG, text_color=GREEN if name == self.activity_tab else MUTED,
                                command=lambda n=name: self.show_activity(n))
            btn.pack(side="left", padx=(0, 4))
            self.tab_buttons[name] = btn
        self.log_button = self.button(activity_header, "打开日志", self.open_logs, width=88)
        self.log_button.pack(side="right")
        self.reset_button = ctk.CTkButton(activity_header, text="重置重试", width=88, height=32,
                                         font=self.font(12), fg_color="transparent", hover_color=BG,
                                         text_color=MUTED, command=self.reset)
        self.reset_button.pack(side="right", padx=(0, 5))
        self.preview_refresh = ctk.CTkButton(activity_header, text="更新预览", width=88, height=32,
                                            font=self.font(12), fg_color="transparent", hover_color=BG,
                                            text_color=MUTED, command=self.refresh_preview)
        self.activity_body = ctk.CTkFrame(activity, fg_color="transparent")
        self.activity_body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 12))
        self.activity_body.grid_columnconfigure(0, weight=1)
        self.activity_body.grid_rowconfigure(0, weight=1)
        self.logbox = ctk.CTkTextbox(self.activity_body, height=65, wrap="word", fg_color=WHITE,
                                     text_color="#5E7067", font=ctk.CTkFont(FONT, 12), spacing1=5, spacing3=5,
                                     scrollbar_button_color=LINE, scrollbar_button_hover_color="#B9C8BE")
        self.logbox.grid(row=0, column=0, sticky="nsew")
        self.logbox.tag_config("time", foreground="#9AA79F")
        self.logbox.insert("end", "等待连接，监测记录会显示在这里。")
        self.previewbox = ctk.CTkTextbox(self.activity_body, height=65, wrap="none", fg_color="#202D28",
                                         text_color="#D7E6DC", corner_radius=8, font=ctk.CTkFont("Consolas", 12),
                                         scrollbar_button_color="#40554A", scrollbar_button_hover_color="#63786B")
        self.previewbox.insert("end", "绑定任务后，这里会显示原窗格的输出。")
        self.logbox.configure(state="disabled")
        self.previewbox.configure(state="disabled")

        footer = ctk.CTkFrame(root, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.label(footer, "仅在临时故障后续接 · 需要人工处理时暂停", size=11, color=MUTED).pack(side="left")
        self.label(footer, textvariable=self.checked_var, size=11, color=MUTED).pack(side="right")
        self.render_rows()
        self.update_controls()

    def step(self, variable, delta, low, high):
        try:
            value = int(variable.get())
        except ValueError:
            value = low
        variable.set(str(max(low, min(high, value + delta))))

    def render_rows(self):
        # Selection belongs to a session, never to its position in a refreshed list.
        if self.selected_key not in {row_key(row) for row in self.rows}:
            self.selected_key = None
        for widget in self.task_list.winfo_children():
            widget.destroy()
        self.row_widgets.clear()
        query = self.search.get().strip().lower()
        visible = [(i, row) for i, row in enumerate(self.rows)
                   if not query or query in json.dumps(row, ensure_ascii=False).lower()]
        self.task_count.configure(text=f"{len(visible)} 个窗格" if not query else f"{len(visible)} / {len(self.rows)} 个窗格")
        labels = {"running": ("运行中", GREEN, TINT), "idle": ("空闲", MUTED, BG),
                  "waiting_input": ("等待输入", "#8D7339", "#FAF4E8"),
                  "attention": ("待处理", "#A47A34", "#FAF4E8"),
                  "failed": ("故障", "#B35C4C", "#FAEEEA"), "finished": ("已结束", MUTED, BG)}
        for pos, (index, row) in enumerate(visible):
            key = row_key(row)
            selected = key == self.selected_key
            frame = ctk.CTkFrame(self.task_list, corner_radius=9, fg_color=TINT if selected else WHITE,
                                 border_width=1, border_color="#AFCEBC" if selected else LINE, cursor="hand2")
            frame.grid(row=pos, column=0, sticky="ew", padx=3, pady=(3, 5))
            frame.grid_columnconfigure(1, weight=1)
            radio = self.label(frame, "●" if selected else "○", size=18, color=GREEN if selected else "#B4C0B9", width=22)
            radio.grid(row=0, column=0, rowspan=2, padx=(11, 7))
            title = row.get("title") or "Codex"
            title_label = self.label(frame, title, size=13, bold=True, height=24, wraplength=300, justify="left")
            title_label.grid(row=0, column=1, sticky="ew", pady=(10, 0), padx=(0, 10))
            cwd = row.get("cwd", "")
            short_dir = PureWindowsPath(cwd).name or cwd or "未提供目录"
            path_label = self.label(frame, f"窗格 {row['pane_id']}  ·  {short_dir}", size=11, color=MUTED,
                                    height=22, wraplength=300, justify="left")
            path_label.grid(row=1, column=1, sticky="ew", pady=(0, 10), padx=(0, 10))
            for label in (title_label, path_label):
                label.bind("<Configure>", lambda e, w=label: w.configure(wraplength=max(100, e.width)))
            name, color, tint = labels.get(row.get("task_state"), (row.get("task_state") or "未知", MUTED, BG))
            badge = self.label(frame, f"  {name}  ", size=11, color=color, fg_color=tint, corner_radius=5, height=25)
            badge.grid(row=0, column=2, rowspan=2, padx=(0, 12))
            for widget in (frame, radio, title_label, path_label, badge):
                widget.bind("<Button-1>", lambda _, k=key: self.select_row(k))
                widget.bind("<Double-Button-1>", lambda _, k=key: self.select_and_bind(k))
            self.row_widgets[key] = (frame, radio)
        if not visible:
            blank = ctk.CTkFrame(self.task_list, fg_color="transparent")
            blank.grid(row=0, column=0, sticky="ew", pady=20)
            self.label(blank, "没有匹配的任务" if query else "还没有发现 Codex 任务", size=14, color=TEXT, bold=True).pack()
            self.label(blank, "试试其他任务名或目录" if query else "在 Pebrel 中启动 Codex，然后刷新", size=12, color=MUTED).pack(pady=(2, 0))
        self.update_target()
        self.update_controls()

    def selected_row(self):
        return next((r for r in self.rows if row_key(r) == self.selected_key and row_key(r) in self.row_widgets), None)

    def select_row(self, key):
        self.selected_key = key
        for row_id, (frame, radio) in self.row_widgets.items():
            active = row_id == key
            frame.configure(fg_color=TINT if active else WHITE, border_color="#AFCEBC" if active else LINE)
            radio.configure(text="●" if active else "○", text_color=GREEN if active else "#B4C0B9")
        self.update_target()
        self.update_controls()

    def select_and_bind(self, key):
        self.select_row(key)
        self.bind_selected()

    def update_target(self):
        row = self.selected_row()
        if self.bound:
            self.target_var.set(f"已绑定 · 窗格 {self.bound.pane_id} · {PureWindowsPath(self.bound.cwd).name or 'Codex'}")
        elif row:
            self.target_var.set(f"已选择窗格 {row['pane_id']}，点击绑定任务")
        else:
            self.target_var.set("选择一个任务，绑定后开始监测")
        if row:
            details = f"窗格 {row['pane_id']} · 窗口 {row['window_id']}\n{row.get('cwd', '')}\n会话 {row.get('agent', {}).get('session_id') or '尚未就绪'}"
        elif self.bound:
            details = f"窗格 {self.bound.pane_id} · 窗口 {self.bound.window_id}\n{self.bound.cwd}\n会话 {self.bound.session_id}"
        else:
            details = "选择任务后可查看完整目录与会话 ID。"
        self.details_text = details
        if self.details_box is not None and self.details_box.winfo_exists():
            self.details_box.configure(state="normal")
            self.details_box.delete("1.0", "end")
            self.details_box.insert("end", details)
            self.details_box.configure(state="disabled")

    def toggle_details(self):
        if self.details_window is not None and self.details_window.winfo_exists():
            self.details_window.lift()
            return
        dialog = ctk.CTkToplevel(self, fg_color=WHITE)
        self.details_window = dialog
        dialog.title("会话详情")
        dialog.geometry(f"580x240+{max(0, self.winfo_rootx() + 80)}+{max(0, self.winfo_rooty() + 160)}")
        dialog.minsize(480, 220)
        dialog.transient(self)
        self.label(dialog, "会话详情", size=17, bold=True).pack(anchor="w", padx=24, pady=(18, 8))
        self.details_box = ctk.CTkTextbox(dialog, height=100, corner_radius=8, fg_color=BG,
                                         text_color=TEXT, font=self.font(12), wrap="char")
        self.details_box.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        self.details_box.insert("end", self.details_text)
        self.details_box.configure(state="disabled")
        self.button(dialog, "关闭", dialog.destroy, width=76).pack(anchor="e", padx=24, pady=(0, 18))

    def update_controls(self):
        if not hasattr(self, "start_button"):
            return
        locked = self.enabled or self.binding_pending
        for widget in self.settings_widgets + self.step_buttons:
            widget.configure(state="disabled" if locked else "normal")
        self.start_button.configure(text="暂停监测" if self.enabled else "开始监测  →",
                                    state="normal" if self.bound and not self.binding_pending else "disabled")
        self.bind_button.configure(text="正在绑定…" if self.binding_pending else "绑定任务",
                                   state="normal" if self.selected_row() and not locked else "disabled")
        self.reset_button.configure(state="normal" if self.bound and not self.binding_pending else "disabled")
        self.preview_refresh.configure(state="normal" if self.bound else "disabled")
        self.refresh_button.configure(text="刷新中…" if self.refreshing else "↻  刷新",
                                      state="disabled" if self.refreshing else "normal")
        self.choose_button.configure(state="disabled" if self.binding_pending else "normal")
        if self.enabled:
            heading, icon = "正在监测", "●"
        elif self.binding_pending:
            heading, icon = "正在绑定任务", "○"
        elif self.bound:
            heading, icon = "监测已暂停" if self.has_started else "准备就绪", "○"
        else:
            heading, icon = "准备续接", "○"
        tone = "normal"
        if self.health["title"] and (self.enabled or self.health["tone"] == "error"):
            heading, tone = self.health["title"], self.health["tone"]
        if self.connection_ok is False:
            heading, tone = "Pebrel 未连接", "error"
        palettes = {"normal": (TINT, "#DCEADF", GREEN, "#66796E"),
                    "warning": ("#FFF6E5", "#F0DFC1", "#9D691D", "#8E703E"),
                    "error": ("#FBEFEC", "#EED8D1", "#B35842", "#9A6759")}
        bg, border, accent, detail = palettes[tone]
        self.status_card.configure(fg_color=bg, border_color=border)
        self.status_label.configure(text_color=detail)
        self.state_heading.configure(text_color=accent if tone != "normal" else TEXT)
        self.counter_label.configure(text_color=accent)
        self.state_heading.configure(text=heading)
        self.state_icon.configure(text="!" if tone != "normal" else icon, text_color=accent)
        self.counter_var.set(f"已重试 {self.attempts} 次")

    def show_activity(self, name):
        self.activity_tab = name
        for tab, button in self.tab_buttons.items():
            button.configure(fg_color=TINT if tab == name else "transparent", text_color=GREEN if tab == name else MUTED)
        if name == "窗格预览":
            self.logbox.grid_remove()
            self.previewbox.grid(row=0, column=0, sticky="nsew")
            self.preview_refresh.pack(side="right", padx=(0, 5))
            self.refresh_preview()
        else:
            self.previewbox.grid_remove()
            self.preview_refresh.pack_forget()
            self.logbox.grid(row=0, column=0, sticky="nsew")

    def append_log(self, value):
        self.logbox.configure(state="normal")
        if not self.log_started:
            self.logbox.delete("1.0", "end")
            self.log_started = True
        self.logbox.insert("end", f"{time.strftime('%H:%M:%S')}    ", "time")
        self.logbox.insert("end", f"{value}\n")
        if int(self.logbox.index("end-1c").split(".")[0]) > 600:
            self.logbox.delete("1.0", "100.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
