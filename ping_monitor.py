import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import csv
import html
import subprocess
import platform
import re
import time
import threading
import sqlite3
import os
import numpy as np
from datetime import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.dates as mdates

# 配置 Matplotlib 支持中文显示
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

# ==================== 常量配置 ====================
# 外网监控目标（按优先级排序，当前目标失败时自动切换到下一项）
WAN_TARGETS = ["223.5.5.5", "119.29.29.29", "8.8.8.8", "1.1.1.1"]
CUSTOM_TARGET = "192.168.1.1"

# 常见网关地址（获取失败时轮询）
COMMON_GATEWAYS = ("192.168.1.1", "192.168.0.1", "10.0.0.1", "192.168.31.1")

PING_COUNT = 1            # 每次 ping 发送的包数
PING_INTERVAL = 1         # ping 间隔（秒）
PING_TIMEOUT_MS = 1000    # ping 超时（毫秒）
CLEANUP_AGE = 259200      # 数据保留时长（3 天，秒）
CLEANUP_INTERVAL = 60     # 清理过期数据的最小间隔（秒）
HISTORY_WINDOW = 86400    # 历史图时间窗口（24 小时，秒）
REALTIME_WINDOW = 60      # 实时图时间窗口（秒）
QUALITY_WINDOW = 60       # 延迟质量评分时间窗口（秒）
TABLE_ROW_LIMIT = 200     # 表格最多显示的最近记录数

UI_INTERVAL_MS = 1000     # 状态栏 / 图表 / 诊断刷新间隔（毫秒）
TABLE_INTERVAL_MS = 5000  # 表格刷新间隔（毫秒）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "ping_history.db")


def get_default_gateway():
    """跨平台获取当前默认网关 IP"""
    system = platform.system().lower()
    try:
        if system == 'windows':
            result = subprocess.run(
                ['route', 'print', '0.0.0.0'],
                stdout=subprocess.PIPE, text=True, errors='ignore'
            )
            for line in result.stdout.splitlines():
                if '0.0.0.0' in line:
                    parts = line.split()
                    if len(parts) >= 3 and parts[0] == '0.0.0.0':
                        return parts[2]
        elif system == 'darwin':
            result = subprocess.run(
                ['route', '-n', 'get', 'default'],
                stdout=subprocess.PIPE, text=True, errors='ignore'
            )
            for line in result.stdout.splitlines():
                if 'gateway:' in line:
                    return line.split(':')[1].strip()
        else:
            result = subprocess.run(
                ['ip', 'route'],
                stdout=subprocess.PIPE, text=True, errors='ignore'
            )
            for line in result.stdout.splitlines():
                if line.startswith('default via'):
                    return line.split()[2]
    except Exception:
        pass

    # 兜底：尝试 ping 常见网关，选第一个通的
    for gw in COMMON_GATEWAYS:
        try:
            ping_cmd = (
                ['ping', '-n', '1', '-w', '500', gw]
                if system == 'windows'
                else ['ping', '-c', '1', '-W', '1', gw]
            )
            result = subprocess.run(
                ping_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if result.returncode == 0:
                return gw
        except Exception:
            continue
    return "192.168.1.1"


TARGET_LAN = get_default_gateway()


class NetworkMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"全链路网络诊断看板 - 自动识别网关 ({TARGET_LAN})")
        self.root.geometry("1350x900")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.is_running = True
        self.lat_wan = -1.0
        self.lat_lan = -1.0
        self.lat_custom = -1.0
        self.current_wan_target = WAN_TARGETS[0]
        self.lan_target = TARGET_LAN
        self.custom_target = CUSTOM_TARGET
        self._lock = threading.Lock()

        self.setup_ui()
        self._init_db()
        self.start_monitor_threads()

        self.update_ui_elements()
        self.update_graphs()
        self.update_tables()
        self.update_realtime_diagnosis()
        self.update_quality_scores()
        self.update_daily_summary()
        self.update_diagnosis_table()

    # ---- 数据库初始化 ----

    def _init_db(self):
        """初始化数据库表结构和索引（线程安全，仅启动时调用一次）"""
        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=10)
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS ping_data_v2 (
                timestamp REAL, ip TEXT, latency REAL
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS outages_v2 (
                start_time REAL, end_time REAL, ip TEXT
            )''')
            # 复合索引：加速按 IP + 时间范围的查询（图表和表格的核心查询）
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_ping_ip_ts "
                "ON ping_data_v2(ip, timestamp)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_outages_ip_ts "
                "ON outages_v2(ip, start_time)"
            )
            conn.commit()
        except Exception as e:
            print(f"[DB] 初始化失败: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ---- UI 搭建 ----

    def setup_ui(self):
        # --- 顶部全局状态栏 ---
        header_frame = tk.Frame(self.root, pady=10, bg="#2c3e50")
        header_frame.pack(fill=tk.X)

        self.status_wan_label = tk.Label(
            header_frame,
            text=f"外网 ({WAN_TARGETS[0]}): 正在检测...",
            font=("Microsoft YaHei", 14, "bold"),
            bg="#2c3e50", fg="white"
        )
        self.status_wan_label.pack(side=tk.LEFT, expand=True)

        self.status_lan_label = tk.Label(
            header_frame,
            text=f"网关 ({TARGET_LAN}): 正在检测...",
            font=("Microsoft YaHei", 14, "bold"),
            bg="#2c3e50", fg="white"
        )
        self.status_lan_label.pack(side=tk.RIGHT, expand=True)

        # --- 主体容器 ---
        main_body = tk.Frame(self.root)
        main_body.pack(fill=tk.BOTH, expand=True)

        # ================= 右侧：智能诊断面板 =================
        right_outer = tk.Frame(main_body, width=390, bg="#f8f9fa", relief=tk.GROOVE, bd=2)
        right_outer.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 10), pady=10)
        right_outer.pack_propagate(False)

        self.right_canvas = tk.Canvas(right_outer, bg="#f8f9fa", highlightthickness=0)
        self.right_scrollbar = ttk.Scrollbar(
            right_outer, orient="vertical", command=self.right_canvas.yview
        )
        right_panel = tk.Frame(self.right_canvas, bg="#f8f9fa")
        self.right_window = self.right_canvas.create_window(
            (0, 0), window=right_panel, anchor="nw"
        )
        right_panel.bind(
            "<Configure>",
            lambda e: self.right_canvas.configure(scrollregion=self.right_canvas.bbox("all"))
        )
        self.right_canvas.bind(
            "<Configure>",
            lambda e: self.right_canvas.itemconfigure(self.right_window, width=e.width)
        )
        self.right_canvas.configure(yscrollcommand=self.right_scrollbar.set)
        self.right_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.right_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        tk.Label(
            right_panel, text="🤖 实时故障诊断",
            font=("Microsoft YaHei", 12, "bold"), bg="#f8f9fa"
        ).pack(pady=(15, 5))

        self.diag_text = tk.Text(
            right_panel, height=7, font=("Microsoft YaHei", 10),
            wrap=tk.WORD, relief=tk.FLAT, padx=10, pady=10
        )
        self.diag_text.pack(fill=tk.X, padx=10)
        self.diag_text.insert(tk.END, "正在分析当前网络环境...")
        self.diag_text.config(state=tk.DISABLED)

        tk.Label(
            right_panel, text="📶 延迟质量评分",
            font=("Microsoft YaHei", 12, "bold"), bg="#f8f9fa"
        ).pack(pady=(12, 8))

        quality_frame = tk.Frame(right_panel, bg="#f8f9fa")
        quality_frame.pack(fill=tk.X, padx=12)
        self.quality_labels = {}
        quality_items = (
            ("wan", "外网质量"),
            ("lan", "网关质量"),
            ("custom", "自定义质量"),
        )
        for key, name in quality_items:
            item = tk.Frame(quality_frame, bg="#ecf0f1", padx=8, pady=6)
            item.pack(fill=tk.X, pady=3)
            top_line = tk.Frame(item, bg="#ecf0f1")
            top_line.pack(fill=tk.X)
            tk.Label(
                top_line, text=name, font=("Microsoft YaHei", 9),
                bg="#ecf0f1", fg="#566573"
            ).pack(side=tk.LEFT)
            score_label = tk.Label(
                top_line, text="采集中", font=("Microsoft YaHei", 11, "bold"),
                bg="#ecf0f1", fg="#7f8c8d"
            )
            score_label.pack(side=tk.RIGHT)
            detail_label = tk.Label(
                item, text="等待最近 60 秒数据",
                font=("Microsoft YaHei", 9), bg="#ecf0f1", fg="#566573"
            )
            detail_label.pack(anchor=tk.W, pady=(3, 0))
            self.quality_labels[key] = {
                "frame": item,
                "score": score_label,
                "detail": detail_label,
            }

        tk.Label(
            right_panel, text="📊 今日概览",
            font=("Microsoft YaHei", 12, "bold"), bg="#f8f9fa"
        ).pack(pady=(15, 8))

        summary_frame = tk.Frame(right_panel, bg="#f8f9fa")
        summary_frame.pack(fill=tk.X, padx=12)

        self.summary_labels = {}
        summary_items = (
            ("count", "外网断网", "0 次"),
            ("total", "累计时长", "0 秒"),
            ("longest", "最长中断", "0 秒"),
            ("reason", "责任倾向", "暂无"),
        )
        for idx, (key, name, value) in enumerate(summary_items):
            row = idx // 2
            col = idx % 2
            item = tk.Frame(summary_frame, bg="#ecf0f1", padx=8, pady=6)
            item.grid(row=row, column=col, sticky="ew", padx=3, pady=3)
            tk.Label(
                item, text=name, font=("Microsoft YaHei", 9),
                bg="#ecf0f1", fg="#566573"
            ).pack(anchor=tk.W)
            value_label = tk.Label(
                item, text=value, font=("Microsoft YaHei", 11, "bold"),
                bg="#ecf0f1", fg="#2c3e50"
            )
            value_label.pack(anchor=tk.W)
            self.summary_labels[key] = value_label
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.columnconfigure(1, weight=1)

        export_frame = tk.Frame(right_panel, bg="#f8f9fa")
        export_frame.pack(fill=tk.X, padx=12, pady=(8, 0))
        ttk.Button(
            export_frame, text="导出 CSV",
            command=lambda: self.export_report("csv")
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(
            export_frame, text="导出 HTML",
            command=lambda: self.export_report("html")
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        action_frame = tk.Frame(right_panel, bg="#f8f9fa")
        action_frame.pack(fill=tk.X, padx=12, pady=(8, 0))
        ttk.Button(
            action_frame, text="配置",
            command=self.open_config_panel
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(
            action_frame, text="清空数据",
            command=self.clear_history_data
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        ttk.Separator(right_panel, orient='horizontal').pack(fill=tk.X, pady=15, padx=10)

        # 历史责任判定表格
        tk.Label(
            right_panel, text="📋 历史断网责任判定",
            font=("Microsoft YaHei", 12, "bold"), bg="#f8f9fa"
        ).pack(pady=(0, 10))

        diag_frame = tk.Frame(right_panel)
        diag_frame.pack(fill=tk.X, padx=10, pady=(0, 15))

        cols_diag = ("time", "lan_ip", "lan_status", "reason")
        self.tree_diag = ttk.Treeview(
            diag_frame, columns=cols_diag, show="headings", height=7
        )
        self.tree_diag.heading("time", text="断开时间")
        self.tree_diag.heading("lan_ip", text="当时网关")
        self.tree_diag.heading("lan_status", text="状态")
        self.tree_diag.heading("reason", text="责任判定")

        self.tree_diag.column("time", width=100, anchor=tk.CENTER)
        self.tree_diag.column("lan_ip", width=100, anchor=tk.CENTER)
        self.tree_diag.column("lan_status", width=60, anchor=tk.CENTER)
        self.tree_diag.column("reason", width=80, anchor=tk.CENTER)

        scroll_diag = ttk.Scrollbar(diag_frame, orient=tk.VERTICAL, command=self.tree_diag.yview)
        self.tree_diag.configure(yscroll=scroll_diag.set)
        scroll_diag.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_diag.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ================= 左侧：滚动图表区域 =================
        left_main_frame = tk.Frame(main_body)
        left_main_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas_scroll = tk.Canvas(left_main_frame)
        self.scrollbar = ttk.Scrollbar(left_main_frame, orient="vertical", command=self.canvas_scroll.yview)
        self.scrollable_frame = ttk.Frame(self.canvas_scroll)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas_scroll.configure(scrollregion=self.canvas_scroll.bbox("all"))
        )
        self.left_window = self.canvas_scroll.create_window(
            (0, 0), window=self.scrollable_frame, anchor="nw"
        )
        self.canvas_scroll.bind(
            "<Configure>",
            lambda e: self.canvas_scroll.itemconfigure(self.left_window, width=e.width)
        )
        self.canvas_scroll.configure(yscrollcommand=self.scrollbar.set)
        self.canvas_scroll.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        def _is_descendant(widget, parent):
            while widget is not None:
                if widget == parent:
                    return True
                parent_name = widget.winfo_parent()
                widget = widget.nametowidget(parent_name) if parent_name else None
            return False

        def _on_mousewheel(event):
            units = int(-1 * (event.delta / 120))
            if _is_descendant(event.widget, right_outer):
                self.right_canvas.yview_scroll(units, "units")
            else:
                self.canvas_scroll.yview_scroll(units, "units")
        self.root.bind_all("<MouseWheel>", _on_mousewheel)

        self.wan_ui = self._create_monitor_section(
            self.scrollable_frame,
            f"🌐 外网链路监控 (Internet - {WAN_TARGETS[0]})",
            "#3498db"
        )
        self.lan_ui = self._create_monitor_section(
            self.scrollable_frame,
            f"🖧 路由器网关监控 (Local - {TARGET_LAN})",
            "#27ae60"
        )
        self.custom_ui = self._create_monitor_section(
            self.scrollable_frame,
            f"🎯 自定义目标监控 (Custom - {CUSTOM_TARGET})",
            "#8e44ad"
        )

    def _create_monitor_section(self, parent, title_text, color_theme):
        frame = tk.LabelFrame(
            parent, text=title_text,
            font=("Microsoft YaHei", 14, "bold"),
            fg=color_theme, padx=10, pady=10
        )
        frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        main_container = tk.Frame(frame)
        main_container.pack(fill=tk.BOTH, expand=True)

        chart_frame = tk.Frame(main_container)
        chart_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        fig, (ax_rt, ax_hist) = plt.subplots(2, 1, figsize=(7, 6), dpi=90)
        fig.tight_layout(pad=4.0)
        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # 创建空线段（后续用 set_data 更新，避免每秒重建图表）
        (rt_line,) = ax_rt.plot([], [], color=color_theme, linewidth=2,
                                label=f"实时延迟 (ms)")
        (hist_line,) = ax_hist.plot([], [], color='#2c3e50', linewidth=1,
                                    label="历史延迟")
        hist_scatter = ax_hist.scatter([], [], color='red', s=10, label="中断点",
                                       zorder=5)

        # 一次性配置图表格式，不再重复设置
        ax_rt.set_title("最近 60 秒实时动态", fontsize=10)
        ax_rt.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        ax_rt.grid(True, linestyle=':', alpha=0.7)
        ax_rt.legend(loc="upper right")

        ax_hist.set_title("过去 24 小时趋势", fontsize=10)
        ax_hist.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax_hist.grid(True, linestyle=':', alpha=0.7)
        ax_hist.legend(prop={'size': 8}, loc="upper right")

        # --- 右侧列表 ---
        list_frame = tk.Frame(main_container, width=320)
        list_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(10, 0))

        latency_panel = tk.Frame(list_frame, bg="#eef3f6", padx=10, pady=8)
        latency_panel.pack(fill=tk.X, pady=(0, 10))
        tk.Label(
            latency_panel, text="实时延迟",
            font=("Microsoft YaHei", 9), bg="#eef3f6", fg="#566573"
        ).pack(anchor=tk.W)
        latency_label = tk.Label(
            latency_panel, text="采集中",
            font=("Microsoft YaHei", 18, "bold"), bg="#eef3f6", fg=color_theme
        )
        latency_label.pack(anchor=tk.W)
        target_label = tk.Label(
            latency_panel, text="目标待确认",
            font=("Microsoft YaHei", 9), bg="#eef3f6", fg="#566573"
        )
        target_label.pack(anchor=tk.W, pady=(2, 0))

        tk.Label(list_frame, text="所有单次记录",
                 font=("Microsoft YaHei", 10, "bold")).pack(pady=(0, 5))
        frame_tv1 = tk.Frame(list_frame)
        frame_tv1.pack(fill=tk.BOTH, expand=True)
        tree_all = ttk.Treeview(frame_tv1, columns=("time", "status"),
                                show="headings", height=8)
        tree_all.heading("time", text="断网发生时间")
        tree_all.heading("status", text="状态")
        tree_all.column("time", width=160, anchor=tk.CENTER)
        tree_all.column("status", width=80, anchor=tk.CENTER)
        scroll1 = ttk.Scrollbar(frame_tv1, orient=tk.VERTICAL, command=tree_all.yview)
        tree_all.configure(yscroll=scroll1.set)
        scroll1.pack(side=tk.RIGHT, fill=tk.Y)
        tree_all.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Separator(list_frame, orient='horizontal').pack(fill=tk.X, pady=10)

        tk.Label(list_frame, text="中断持续时间统计",
                 font=("Microsoft YaHei", 10, "bold")).pack(pady=(0, 5))
        frame_tv2 = tk.Frame(list_frame)
        frame_tv2.pack(fill=tk.BOTH, expand=True)
        tree_dur = ttk.Treeview(frame_tv2, columns=("start", "duration"),
                                show="headings", height=8)
        tree_dur.heading("start", text="开始断网时间")
        tree_dur.heading("duration", text="持续时长")
        tree_dur.column("start", width=160, anchor=tk.CENTER)
        tree_dur.column("duration", width=80, anchor=tk.CENTER)
        scroll2 = ttk.Scrollbar(frame_tv2, orient=tk.VERTICAL, command=tree_dur.yview)
        tree_dur.configure(yscroll=scroll2.set)
        scroll2.pack(side=tk.RIGHT, fill=tk.Y)
        tree_dur.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        return {
            "frame": frame,
            "fig": fig, "canvas": canvas,
            "ax_rt": ax_rt, "ax_hist": ax_hist,
            "rt_line": rt_line, "hist_line": hist_line,
            "hist_scatter": hist_scatter,
            "latency_panel": latency_panel,
            "latency_label": latency_label,
            "target_label": target_label,
            "tree_all": tree_all, "tree_dur": tree_dur,
        }

    # ---- 网络检测 ----

    def ping_ip(self, ip):
        """ping 指定 IP，返回延迟（ms），超时或失败返回 -1.0"""
        system = platform.system().lower()
        cmd = (
            ['ping', '-n', str(PING_COUNT), '-w', str(PING_TIMEOUT_MS), ip]
            if system == 'windows'
            else ['ping', '-c', str(PING_COUNT), '-W', str(PING_TIMEOUT_MS // 1000), ip]
        )
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors='ignore'
            )
            if result.returncode == 0:
                match = re.search(r'([0-9.]+)\s*ms', result.stdout, re.IGNORECASE)
                return float(match.group(1)) if match else 1.0
            return -1.0
        except Exception:
            return -1.0

    def monitor_loop(self, target_ip, role):
        """后台线程：持续 ping 指定 IP，写入 SQLite 并跟踪中断事件"""
        conn = sqlite3.connect(DB_NAME, timeout=10)
        try:
            c = conn.cursor()
            was_offline = False
            outage_start = None
            outage_ip = None
            last_cleanup = 0

            # 判断当前线程职责：WAN 线程需要自动切换备用目标
            is_wan = (role == "wan")
            current_target = target_ip

            # 恢复上次未结束的中断
            outage_targets = WAN_TARGETS if is_wan else (target_ip,)
            placeholders = ','.join('?' * len(outage_targets))
            c.execute(
                f"SELECT start_time, ip FROM outages_v2 WHERE end_time IS NULL "
                f"AND ip IN ({placeholders}) ORDER BY start_time DESC LIMIT 1",
                outage_targets
            )
            row = c.fetchone()
            if row:
                was_offline = True
                outage_start = row[0]
                outage_ip = row[1]

            while self.is_running:
                if role == "lan":
                    with self._lock:
                        next_target = self.lan_target
                    if next_target != current_target:
                        if was_offline and outage_start is not None and outage_ip is not None:
                            try:
                                c.execute(
                                    "UPDATE outages_v2 SET end_time = ? "
                                    "WHERE start_time = ? AND ip = ?",
                                    (time.time(), outage_start, outage_ip)
                                )
                                conn.commit()
                            except sqlite3.OperationalError:
                                pass
                        was_offline = False
                        outage_start = None
                        outage_ip = None
                        current_target = next_target

                if role == "custom":
                    with self._lock:
                        next_target = self.custom_target
                        lan_target = self.lan_target
                    if next_target != current_target:
                        if was_offline and outage_start is not None and outage_ip is not None:
                            try:
                                c.execute(
                                    "UPDATE outages_v2 SET end_time = ? "
                                    "WHERE start_time = ? AND ip = ?",
                                    (time.time(), outage_start, outage_ip)
                                )
                                conn.commit()
                            except sqlite3.OperationalError:
                                pass
                        was_offline = False
                        outage_start = None
                        outage_ip = None
                        current_target = next_target
                    if current_target == lan_target:
                        with self._lock:
                            self.lat_custom = self.lat_lan
                        time.sleep(PING_INTERVAL)
                        continue

                latency = self.ping_ip(current_target)

                # WAN 线程：当前目标失败时自动尝试备用目标
                if latency == -1 and is_wan:
                    for alt in WAN_TARGETS:
                        if alt == current_target:
                            continue
                        alt_lat = self.ping_ip(alt)
                        if alt_lat != -1:
                            current_target = alt
                            latency = alt_lat
                            break

                # 线程安全更新共享延迟值和当前活跃目标
                with self._lock:
                    if role == "wan":
                        self.lat_wan = latency
                        self.current_wan_target = current_target
                    elif role == "lan":
                        self.lat_lan = latency
                    else:
                        self.lat_custom = latency

                now = time.time()
                for attempt in range(3):
                    try:
                        c.execute(
                            "INSERT INTO ping_data_v2 VALUES (?, ?, ?)",
                            (now, current_target, latency)
                        )
                        if latency == -1:
                            if not was_offline:
                                was_offline = True
                                outage_start = now
                                outage_ip = current_target
                                c.execute(
                                    "INSERT INTO outages_v2 (start_time, end_time, ip) "
                                    "VALUES (?, NULL, ?)",
                                    (outage_start, outage_ip)
                                )
                        else:
                            if was_offline:
                                was_offline = False
                                c.execute(
                                    "UPDATE outages_v2 SET end_time = ? "
                                    "WHERE start_time = ? AND ip = ?",
                                    (now, outage_start, outage_ip)
                                )
                                outage_start = None
                                outage_ip = None

                        if now - last_cleanup >= CLEANUP_INTERVAL:
                            c.execute(
                                "DELETE FROM ping_data_v2 WHERE timestamp < ?",
                                (now - CLEANUP_AGE,)
                            )
                            c.execute(
                                "DELETE FROM outages_v2 WHERE start_time < ?",
                                (now - CLEANUP_AGE,)
                            )
                            last_cleanup = now
                        conn.commit()
                        break
                    except sqlite3.OperationalError:
                        if attempt < 2:
                            time.sleep(0.1)
                        else:
                            print(f"[DB] {target_ip} 写入重试耗尽，跳过该次")
                time.sleep(PING_INTERVAL)
        finally:
            conn.close()

    def start_monitor_threads(self):
        """启动后台线程分别监控外网、网关和自定义目标"""
        self.thread_wan = threading.Thread(
            target=self.monitor_loop, args=(WAN_TARGETS[0], "wan"), daemon=True
        )
        self.thread_lan = threading.Thread(
            target=self.monitor_loop, args=(TARGET_LAN, "lan"), daemon=True
        )
        self.thread_custom = threading.Thread(
            target=self.monitor_loop, args=(CUSTOM_TARGET, "custom"), daemon=True
        )
        self.thread_wan.start()
        self.thread_lan.start()
        self.thread_custom.start()

    # ---- UI 更新 ----

    def _update_latency_panel(self, ui_dict, latency, target_ip, ok_color):
        """更新监控块旁边的实时延迟读数"""
        if latency == -1:
            text = "超时"
            fg = "#e74c3c"
            bg = "#fdedec"
        elif latency < 1:
            text = f"{latency:.2f} ms"
            fg = ok_color
            bg = "#eef3f6"
        else:
            text = f"{latency:.1f} ms"
            fg = ok_color
            bg = "#eef3f6"

        ui_dict["latency_panel"].config(bg=bg)
        ui_dict["latency_label"].config(text=text, fg=fg, bg=bg)
        ui_dict["target_label"].config(text=f"目标：{target_ip}", bg=bg)
        for child in ui_dict["latency_panel"].winfo_children():
            child.config(bg=bg)

    def update_ui_elements(self):
        """更新顶部状态栏（每秒）"""
        with self._lock:
            wan_ok = self.lat_wan != -1
            lan_ok = self.lat_lan != -1
            wan_lat = self.lat_wan
            lan_lat = self.lat_lan
            custom_lat = self.lat_custom
            current_wan = self.current_wan_target
            lan_target = self.lan_target
            custom_target = self.custom_target

        if wan_ok:
            self.status_wan_label.config(
                text=f"🌐 外网 ({current_wan}): 正常 ({wan_lat} ms)", fg="#2ecc71"
            )
        else:
            self.status_wan_label.config(
                text=f"🌐 外网 ({current_wan}): 丢包断开", fg="#e74c3c"
            )

        if lan_ok:
            self.status_lan_label.config(
                text=f"🖧 网关 ({lan_target}): 正常 ({lan_lat} ms)", fg="#2ecc71"
            )
        else:
            self.status_lan_label.config(
                text=f"🖧 网关 ({lan_target}): 丢包断开", fg="#e74c3c"
            )
        self._update_latency_panel(self.wan_ui, wan_lat, current_wan, "#3498db")
        self._update_latency_panel(self.lan_ui, lan_lat, lan_target, "#27ae60")
        self._update_latency_panel(self.custom_ui, custom_lat, custom_target, "#8e44ad")
        self.root.after(UI_INTERVAL_MS, self.update_ui_elements)

    def _draw_charts(self, ui_dict, target_ip, label_name):
        """更新图表数据（原地更新 set_data，避免每秒重建图表）"""
        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()
            now = time.time()

            # --- 实时图（最近 60 秒）---
            c.execute(
                "SELECT timestamp, latency FROM ping_data_v2 "
                "WHERE ip = ? AND timestamp >= ? ORDER BY timestamp ASC",
                (target_ip, now - REALTIME_WINDOW)
            )
            data_rt = c.fetchall()
            if data_rt:
                times_rt = [datetime.fromtimestamp(r[0]) for r in data_rt]
                lats_rt = [r[1] if r[1] != -1 else 0 for r in data_rt]
                ui_dict["rt_line"].set_data(times_rt, lats_rt)
                # 手动设定纵轴：从数据取最大值，留出余量，确保所有延迟值可见
                y_max_rt = max(lats_rt) if lats_rt else 10
                ui_dict["ax_rt"].set_ylim(0, y_max_rt * 1.2 + 5)
                # 横轴仍用 autoscale
                ui_dict["ax_rt"].relim()
                ui_dict["ax_rt"].autoscale_view(scalex=True, scaley=False)
            else:
                ui_dict["rt_line"].set_data([], [])

            # --- 历史图（过去 24 小时）---
            c.execute(
                "SELECT timestamp, latency FROM ping_data_v2 "
                "WHERE ip = ? AND timestamp >= ? ORDER BY timestamp ASC",
                (target_ip, now - HISTORY_WINDOW)
            )
            data_hist = c.fetchall()
            if data_hist:
                times_h = [datetime.fromtimestamp(r[0]) for r in data_hist]
                lats_h = [r[1] for r in data_hist]
                clean_times = [times_h[i] for i in range(len(lats_h))
                               if lats_h[i] != -1]
                clean_lats = [lats_h[i] for i in range(len(lats_h))
                              if lats_h[i] != -1]
                drop_times = [times_h[i] for i in range(len(lats_h))
                              if lats_h[i] == -1]

                ui_dict["hist_line"].set_data(clean_times, clean_lats)
                # 手动设定历史图纵轴
                if clean_lats:
                    y_max_hist = max(clean_lats)
                    ui_dict["ax_hist"].set_ylim(0, y_max_hist * 1.2 + 5)
                # 横轴仍用 autoscale
                ui_dict["ax_hist"].relim()
                ui_dict["ax_hist"].autoscale_view(scalex=True, scaley=False)

                # 更新散点（中断点）
                if drop_times:
                    ui_dict["hist_scatter"].set_offsets(
                        list(zip(drop_times, [0] * len(drop_times)))
                    )
                else:
                    ui_dict["hist_scatter"].set_offsets(np.empty((0, 2)))
            else:
                ui_dict["hist_line"].set_data([], [])
                ui_dict["hist_scatter"].set_offsets(np.empty((0, 2)))

            ui_dict["canvas"].draw_idle()
        except Exception as e:
            print(f"[Chart] {label_name} 绘图出错: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def update_graphs(self):
        """定时刷新两张图表的趋势数据（每秒）"""
        with self._lock:
            current_wan = self.current_wan_target
            lan_target = self.lan_target
            custom_target = self.custom_target
        self._draw_charts(self.wan_ui, current_wan, "外网")
        self._draw_charts(self.lan_ui, lan_target, "网关")
        self._draw_charts(self.custom_ui, custom_target, "自定义")
        self.root.after(UI_INTERVAL_MS, self.update_graphs)

    @staticmethod
    def _format_duration(seconds):
        """把秒数格式化成适合表格展示的短文本"""
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds} 秒"
        if seconds < 3600:
            return f"{seconds // 60}分 {seconds % 60}秒"
        return f"{seconds // 3600}时 {(seconds % 3600) // 60}分"

    def _refresh_table_data(self, ui_dict, target_ips):
        """刷新一个或多个 IP 的中断记录表格"""
        conn = None
        try:
            if isinstance(target_ips, str):
                target_ips = (target_ips,)
            else:
                target_ips = tuple(target_ips)
            placeholders = ','.join('?' * len(target_ips))

            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()

            # 所有单次断网记录
            c.execute(
                "SELECT timestamp FROM ping_data_v2 "
                f"WHERE ip IN ({placeholders}) AND latency = -1 "
                "ORDER BY timestamp DESC LIMIT ?",
                (*target_ips, TABLE_ROW_LIMIT)
            )
            for item in ui_dict["tree_all"].get_children():
                ui_dict["tree_all"].delete(item)
            for r in c.fetchall():
                ui_dict["tree_all"].insert(
                    "", "end",
                    values=(
                        datetime.fromtimestamp(r[0]).strftime('%Y-%m-%d %H:%M:%S'),
                        "超时断开"
                    )
                )

            # 中断持续时间
            c.execute(
                "SELECT start_time, end_time FROM outages_v2 "
                f"WHERE ip IN ({placeholders}) ORDER BY start_time DESC LIMIT ?",
                (*target_ips, TABLE_ROW_LIMIT)
            )
            for item in ui_dict["tree_dur"].get_children():
                ui_dict["tree_dur"].delete(item)
            for r in c.fetchall():
                start_str = datetime.fromtimestamp(r[0]).strftime('%m-%d %H:%M:%S')
                if r[1] is None:
                    dur_str = "仍在中断中..."
                else:
                    dur_str = self._format_duration(r[1] - r[0])
                ui_dict["tree_dur"].insert("", "end", values=(start_str, dur_str))
        except Exception as e:
            print(f"[Table] {target_ips} 刷新失败: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def update_tables(self):
        """定时刷新两侧表格（每 5 秒）"""
        with self._lock:
            lan_target = self.lan_target
            custom_target = self.custom_target
        self._refresh_table_data(self.wan_ui, WAN_TARGETS)
        self._refresh_table_data(self.lan_ui, lan_target)
        self._refresh_table_data(self.custom_ui, custom_target)
        self.root.after(TABLE_INTERVAL_MS, self.update_tables)

    def update_realtime_diagnosis(self):
        """实时诊断：根据内外网状态给出故障判定"""
        with self._lock:
            wan_down = self.lat_wan == -1
            lan_down = self.lat_lan == -1
            lan_target = self.lan_target

        self.diag_text.config(state=tk.NORMAL)
        self.diag_text.delete(1.0, tk.END)

        if wan_down:
            if lan_down:
                msg = (
                    "⚠️ 警报：外网与网关均已中断！\n\n"
                    "🔍 判定：【本地网络故障】\n"
                    "💡 建议：你的电脑连不上路由器了。"
                    "请检查网线是否脱落、Wi-Fi是否断开，"
                    "或者路由器是否死机需要拔插头重启。"
                )
                self.diag_text.config(bg="#fdedec", fg="#c0392b")
            else:
                msg = (
                    f"⚠️ 警报：外网断开，但网关({lan_target})畅通！\n\n"
                    "🔍 判定：【外部宽带故障】\n"
                    "💡 建议：你的路由器和电脑正常。"
                    "更可能是光猫断网、宽带欠费，"
                    "或者运营商所在片区机房故障，可以联系宽带客服。"
                )
                self.diag_text.config(bg="#fcf3cf", fg="#b7950b")
        else:
            msg = (
                "✅ 当前网络一切正常。\n\n"
                f"外网畅通，路由器网关({lan_target})响应良好。"
                "系统正在持续监控中..."
            )
            self.diag_text.config(bg="#e8f8f5", fg="#117a65")

        self.diag_text.insert(tk.END, msg)
        self.diag_text.config(state=tk.DISABLED)
        self.root.after(UI_INTERVAL_MS, self.update_realtime_diagnosis)

    def _query_quality_stats(self, cursor, target_ips):
        """查询最近一段时间的成功率、平均延迟和抖动"""
        if isinstance(target_ips, str):
            target_ips = (target_ips,)
        else:
            target_ips = tuple(target_ips)
        placeholders = ','.join('?' * len(target_ips))
        since = time.time() - QUALITY_WINDOW
        cursor.execute(
            "SELECT latency FROM ping_data_v2 "
            f"WHERE ip IN ({placeholders}) AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (*target_ips, since)
        )
        samples = [row[0] for row in cursor.fetchall()]
        if not samples:
            return {
                "total": 0,
                "success": 0,
                "loss_rate": 0.0,
                "avg": None,
                "jitter": None,
            }

        ok_samples = [value for value in samples if value != -1]
        loss_rate = (len(samples) - len(ok_samples)) / len(samples)
        avg_latency = sum(ok_samples) / len(ok_samples) if ok_samples else None
        if len(ok_samples) >= 2:
            avg_diff = sum(
                abs(ok_samples[i] - ok_samples[i - 1])
                for i in range(1, len(ok_samples))
            ) / (len(ok_samples) - 1)
        else:
            avg_diff = 0.0 if ok_samples else None

        return {
            "total": len(samples),
            "success": len(ok_samples),
            "loss_rate": loss_rate,
            "avg": avg_latency,
            "jitter": avg_diff,
        }

    def _score_quality(self, stats, profile):
        """把质量统计转换成可读评分和颜色"""
        if stats["total"] == 0:
            return "采集中", "等待最近 60 秒数据", "#7f8c8d", "#ecf0f1"
        if stats["success"] == 0:
            return "中断", "最近窗口全部超时", "#c0392b", "#fdedec"

        loss_pct = stats["loss_rate"] * 100
        avg_latency = stats["avg"] or 0.0
        jitter = stats["jitter"] or 0.0
        detail = (
            f"均值 {avg_latency:.1f} ms | 抖动 {jitter:.1f} ms | "
            f"丢包 {loss_pct:.0f}%"
        )

        if loss_pct >= 20:
            return "丢包", detail, "#c0392b", "#fdedec"
        if profile == "lan":
            high_latency = 20
            jitter_limit = 10
            excellent_latency = 5
            excellent_jitter = 2
        else:
            high_latency = 180
            jitter_limit = 60
            excellent_latency = 50
            excellent_jitter = 15

        if avg_latency >= high_latency:
            return "高延迟", detail, "#b7950b", "#fcf3cf"
        if jitter >= jitter_limit:
            return "抖动", detail, "#b7950b", "#fcf3cf"
        if loss_pct == 0 and avg_latency <= excellent_latency and jitter <= excellent_jitter:
            return "优秀", detail, "#117a65", "#e8f8f5"
        return "正常", detail, "#1f618d", "#ebf5fb"

    def update_quality_scores(self):
        """刷新外网和网关的延迟质量评分"""
        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()
            with self._lock:
                lan_target = self.lan_target
                custom_target = self.custom_target
            quality_targets = (
                ("wan", WAN_TARGETS, "wan"),
                ("lan", lan_target, "lan"),
                ("custom", custom_target, "wan"),
            )
            for key, targets, profile in quality_targets:
                stats = self._query_quality_stats(c, targets)
                score, detail, fg, bg = self._score_quality(stats, profile)
                labels = self.quality_labels[key]
                labels["frame"].config(bg=bg)
                labels["score"].config(text=score, fg=fg, bg=bg)
                labels["detail"].config(text=detail, bg=bg)
                for child in labels["frame"].winfo_children():
                    child.config(bg=bg)
                    for grandchild in child.winfo_children():
                        grandchild.config(bg=bg)
        except Exception as e:
            print(f"[Quality] 延迟质量评分刷新失败: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self.root.after(UI_INTERVAL_MS, self.update_quality_scores)

    def update_daily_summary(self):
        """刷新今日断网次数、累计时长和责任倾向"""
        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()
            now = time.time()
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            wan_placeholders = ','.join('?' * len(WAN_TARGETS))

            c.execute(
                f"SELECT start_time, end_time FROM outages_v2 "
                f"WHERE ip IN ({wan_placeholders}) "
                f"AND (end_time IS NULL OR end_time >= ?) AND start_time <= ?",
                (*WAN_TARGETS, today_start, now)
            )
            outages = c.fetchall()

            durations = []
            local_faults = 0
            external_faults = 0
            unknown_faults = 0
            for start_time, end_time in outages:
                effective_end = end_time if end_time is not None else now
                effective_start = max(start_time, today_start)
                durations.append(max(0, effective_end - effective_start))

                c.execute(
                    f"SELECT latency FROM ping_data_v2 "
                    f"WHERE ip NOT IN ({wan_placeholders}) "
                    f"AND timestamp BETWEEN ? AND ? "
                    f"ORDER BY ABS(timestamp - ?) ASC LIMIT 1",
                    (*WAN_TARGETS, start_time - 3, start_time + 3, start_time)
                )
                lan_row = c.fetchone()
                if lan_row is None:
                    unknown_faults += 1
                elif lan_row[0] == -1.0:
                    local_faults += 1
                else:
                    external_faults += 1

            count = len(outages)
            total = sum(durations)
            longest = max(durations) if durations else 0
            if count == 0:
                reason = "暂无"
            elif external_faults > local_faults and external_faults >= unknown_faults:
                reason = "偏外部"
            elif local_faults > external_faults and local_faults >= unknown_faults:
                reason = "偏本地"
            else:
                reason = "需观察"

            self.summary_labels["count"].config(text=f"{count} 次")
            self.summary_labels["total"].config(text=self._format_duration(total))
            self.summary_labels["longest"].config(text=self._format_duration(longest))
            self.summary_labels["reason"].config(text=reason)
        except Exception as e:
            print(f"[Summary] 今日概览刷新失败: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self.root.after(TABLE_INTERVAL_MS, self.update_daily_summary)

    def _classify_outage(self, cursor, start_time, wan_placeholders):
        """根据断网时刻附近的网关状态判断责任倾向"""
        cursor.execute(
            f"SELECT ip, latency FROM ping_data_v2 "
            f"WHERE ip NOT IN ({wan_placeholders}) AND timestamp BETWEEN ? AND ? "
            f"ORDER BY ABS(timestamp - ?) ASC LIMIT 1",
            (*WAN_TARGETS, start_time - 3, start_time + 3, start_time)
        )
        lan_row = cursor.fetchone()
        if lan_row is None:
            return "无记录", "--", "早期单线数据"
        lan_ip, lan_latency = lan_row
        if lan_latency == -1.0:
            return lan_ip, "断开", "本地路由器"
        return lan_ip, "畅通", "光猫/运营商"

    def _build_daily_report(self):
        """收集今日报告数据，供 CSV 和 HTML 导出复用"""
        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()
            now = time.time()
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_start = today.timestamp()
            wan_placeholders = ','.join('?' * len(WAN_TARGETS))
            with self._lock:
                lan_target = self.lan_target

            c.execute(
                f"SELECT start_time, end_time, ip FROM outages_v2 "
                f"WHERE ip IN ({wan_placeholders}) "
                f"AND (end_time IS NULL OR end_time >= ?) AND start_time <= ? "
                f"ORDER BY start_time ASC",
                (*WAN_TARGETS, today_start, now)
            )

            rows = []
            local_faults = 0
            external_faults = 0
            unknown_faults = 0
            for start_time, end_time, wan_ip in c.fetchall():
                effective_end = end_time if end_time is not None else now
                effective_start = max(start_time, today_start)
                duration = max(0, effective_end - effective_start)
                lan_ip, lan_status, reason = self._classify_outage(
                    c, start_time, wan_placeholders
                )
                if reason == "本地路由器":
                    local_faults += 1
                elif reason == "光猫/运营商":
                    external_faults += 1
                else:
                    unknown_faults += 1

                rows.append({
                    "start": datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S'),
                    "end": (
                        datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')
                        if end_time is not None else "仍在中断中"
                    ),
                    "wan_ip": wan_ip,
                    "duration": self._format_duration(duration),
                    "duration_seconds": int(duration),
                    "lan_ip": lan_ip,
                    "lan_status": lan_status,
                    "reason": reason,
                })

            durations = [row["duration_seconds"] for row in rows]
            count = len(rows)
            if count == 0:
                tendency = "暂无"
            elif external_faults > local_faults and external_faults >= unknown_faults:
                tendency = "偏外部"
            elif local_faults > external_faults and local_faults >= unknown_faults:
                tendency = "偏本地"
            else:
                tendency = "需观察"

            return {
                "date": today.strftime('%Y-%m-%d'),
                "generated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "wan_targets": ', '.join(WAN_TARGETS),
                "gateway": lan_target,
                "count": count,
                "total": self._format_duration(sum(durations)),
                "longest": self._format_duration(max(durations) if durations else 0),
                "tendency": tendency,
                "rows": rows,
            }
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def export_report(self, report_type):
        """导出今日断网报告"""
        try:
            report = self._build_daily_report()
            ext = "csv" if report_type == "csv" else "html"
            default_name = f"ping_report_{report['date'].replace('-', '')}.{ext}"
            path = filedialog.asksaveasfilename(
                title="导出今日断网报告",
                defaultextension=f".{ext}",
                initialfile=default_name,
                filetypes=(
                    ("CSV 文件", "*.csv"),
                    ("HTML 文件", "*.html"),
                    ("所有文件", "*.*"),
                )
            )
            if not path:
                return

            if report_type == "csv":
                self._write_csv_report(path, report)
            else:
                self._write_html_report(path, report)
            messagebox.showinfo("导出完成", f"报告已保存到：\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _write_csv_report(self, path, report):
        """写入 CSV 报告，使用 utf-8-sig 方便 Excel 识别中文"""
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["报告日期", report["date"]])
            writer.writerow(["生成时间", report["generated_at"]])
            writer.writerow(["外网目标", report["wan_targets"]])
            writer.writerow(["网关", report["gateway"]])
            writer.writerow(["断网次数", report["count"]])
            writer.writerow(["累计时长", report["total"]])
            writer.writerow(["最长中断", report["longest"]])
            writer.writerow(["责任倾向", report["tendency"]])
            writer.writerow([])
            writer.writerow([
                "开始时间", "结束时间", "外网目标", "持续时长",
                "持续秒数", "当时网关", "网关状态", "责任判定"
            ])
            for row in report["rows"]:
                writer.writerow([
                    row["start"], row["end"], row["wan_ip"], row["duration"],
                    row["duration_seconds"], row["lan_ip"], row["lan_status"],
                    row["reason"]
                ])

    def _write_html_report(self, path, report):
        """写入可直接浏览的 HTML 报告"""
        summary_items = (
            ("断网次数", f"{report['count']} 次"),
            ("累计时长", report["total"]),
            ("最长中断", report["longest"]),
            ("责任倾向", report["tendency"]),
        )
        summary_html = ''.join(
            f"<div class='metric'><span>{html.escape(name)}</span>"
            f"<strong>{html.escape(value)}</strong></div>"
            for name, value in summary_items
        )
        rows_html = ''.join(
            "<tr>"
            f"<td>{html.escape(row['start'])}</td>"
            f"<td>{html.escape(row['end'])}</td>"
            f"<td>{html.escape(row['wan_ip'])}</td>"
            f"<td>{html.escape(row['duration'])}</td>"
            f"<td>{html.escape(row['lan_ip'])}</td>"
            f"<td>{html.escape(row['lan_status'])}</td>"
            f"<td>{html.escape(row['reason'])}</td>"
            "</tr>"
            for row in report["rows"]
        )
        if not rows_html:
            rows_html = "<tr><td colspan='7'>今日暂无外网中断记录</td></tr>"

        content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>网络断网报告 {html.escape(report['date'])}</title>
  <style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 32px; color: #263238; }}
    h1 {{ margin-bottom: 6px; }}
    .meta {{ color: #607d8b; margin-bottom: 22px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 24px; }}
    .metric {{ background: #eef3f6; padding: 12px; border-radius: 6px; }}
    .metric span {{ display: block; color: #607d8b; font-size: 13px; }}
    .metric strong {{ display: block; margin-top: 6px; font-size: 18px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border: 1px solid #d5dde2; padding: 8px 10px; text-align: left; }}
    th {{ background: #2c3e50; color: white; }}
    tr:nth-child(even) {{ background: #f7f9fa; }}
  </style>
</head>
<body>
  <h1>网络断网报告</h1>
  <div class="meta">
    报告日期：{html.escape(report['date'])}　
    生成时间：{html.escape(report['generated_at'])}<br>
    外网目标：{html.escape(report['wan_targets'])}　
    网关：{html.escape(report['gateway'])}
  </div>
  <div class="metrics">{summary_html}</div>
  <table>
    <thead>
      <tr>
        <th>开始时间</th><th>结束时间</th><th>外网目标</th>
        <th>持续时长</th><th>当时网关</th><th>网关状态</th><th>责任判定</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
</body>
</html>
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def open_config_panel(self):
        """打开运行时配置面板"""
        config_window = tk.Toplevel(self.root)
        config_window.title("监控配置")
        config_window.resizable(False, False)
        config_window.transient(self.root)
        config_window.grab_set()

        form = ttk.Frame(config_window, padding=16)
        form.pack(fill=tk.BOTH, expand=True)

        fields = {
            "wan_targets": tk.StringVar(value=', '.join(WAN_TARGETS)),
            "lan_target": tk.StringVar(value=self.lan_target),
            "custom_target": tk.StringVar(value=self.custom_target),
            "ping_interval": tk.StringVar(value=str(PING_INTERVAL)),
            "timeout": tk.StringVar(value=str(PING_TIMEOUT_MS)),
            "retention_days": tk.StringVar(value=str(CLEANUP_AGE // 86400)),
            "quality_window": tk.StringVar(value=str(QUALITY_WINDOW)),
            "table_limit": tk.StringVar(value=str(TABLE_ROW_LIMIT)),
        }
        rows = (
            ("外网目标", "wan_targets", "多个 IP 用英文逗号分隔"),
            ("路由器 IP", "lan_target", "当前网关/路由器地址"),
            ("自定义目标", "custom_target", "默认 192.168.1.1"),
            ("Ping 间隔(秒)", "ping_interval", "建议 1-10"),
            ("Ping 超时(毫秒)", "timeout", "建议 500-5000"),
            ("数据保留(天)", "retention_days", "建议 1-30"),
            ("质量评分窗口(秒)", "quality_window", "建议 30-300"),
            ("表格显示条数", "table_limit", "建议 50-1000"),
        )

        for row, (label, key, hint) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky=tk.W, pady=5)
            entry = ttk.Entry(form, textvariable=fields[key], width=34)
            entry.grid(row=row, column=1, sticky=tk.EW, padx=(10, 8), pady=5)
            ttk.Label(form, text=hint, foreground="#667").grid(
                row=row, column=2, sticky=tk.W, pady=5
            )
        form.columnconfigure(1, weight=1)

        ttk.Label(
            form,
            text="配置会在下一轮刷新/下一次 ping 时生效。",
            foreground="#667"
        ).grid(row=len(rows), column=0, columnspan=3, sticky=tk.W, pady=(10, 4))

        button_frame = ttk.Frame(form)
        button_frame.grid(row=len(rows) + 1, column=0, columnspan=3, sticky=tk.E, pady=(12, 0))

        def save_config():
            try:
                new_targets = [
                    item.strip() for item in fields["wan_targets"].get().split(',')
                    if item.strip()
                ]
                if not new_targets:
                    raise ValueError("外网目标至少需要 1 个 IP。")
                new_lan_target = fields["lan_target"].get().strip()
                if not new_lan_target:
                    raise ValueError("路由器 IP 不能为空。")
                new_custom_target = fields["custom_target"].get().strip()
                if not new_custom_target:
                    raise ValueError("自定义目标不能为空。")
                for ip in (*new_targets, new_lan_target, new_custom_target):
                    if not re.fullmatch(r"[0-9A-Za-z:.\\-]+", ip):
                        raise ValueError(f"目标格式不合法：{ip}")

                new_ping_interval = float(fields["ping_interval"].get())
                new_timeout = int(fields["timeout"].get())
                new_retention_days = int(fields["retention_days"].get())
                new_quality_window = int(fields["quality_window"].get())
                new_table_limit = int(fields["table_limit"].get())

                if new_ping_interval <= 0:
                    raise ValueError("Ping 间隔必须大于 0。")
                if new_timeout < 100:
                    raise ValueError("Ping 超时不能小于 100 毫秒。")
                if new_retention_days < 1:
                    raise ValueError("数据至少保留 1 天。")
                if new_quality_window < 5:
                    raise ValueError("质量评分窗口不能小于 5 秒。")
                if new_table_limit < 10:
                    raise ValueError("表格显示条数不能小于 10。")

                global WAN_TARGETS, PING_INTERVAL, PING_TIMEOUT_MS
                global TARGET_LAN, CUSTOM_TARGET, CLEANUP_AGE, QUALITY_WINDOW, TABLE_ROW_LIMIT
                WAN_TARGETS = new_targets
                TARGET_LAN = new_lan_target
                CUSTOM_TARGET = new_custom_target
                PING_INTERVAL = new_ping_interval
                PING_TIMEOUT_MS = new_timeout
                CLEANUP_AGE = new_retention_days * 86400
                QUALITY_WINDOW = new_quality_window
                TABLE_ROW_LIMIT = new_table_limit

                with self._lock:
                    if self.current_wan_target not in WAN_TARGETS:
                        self.current_wan_target = WAN_TARGETS[0]
                    self.lan_target = new_lan_target
                    self.custom_target = new_custom_target
                self.root.title(f"全链路网络诊断看板 - 自动识别网关 ({new_lan_target})")
                self.lan_ui["frame"].config(
                    text=f"🖧 路由器网关监控 (Local - {new_lan_target})"
                )
                self.custom_ui["frame"].config(
                    text=f"🎯 自定义目标监控 (Custom - {new_custom_target})"
                )

                messagebox.showinfo("配置已保存", "新配置已生效。", parent=config_window)
                config_window.destroy()
            except ValueError as e:
                messagebox.showerror("配置无效", str(e), parent=config_window)

        ttk.Button(button_frame, text="取消", command=config_window.destroy).pack(
            side=tk.RIGHT, padx=(8, 0)
        )
        ttk.Button(button_frame, text="保存", command=save_config).pack(side=tk.RIGHT)

    def _clear_tree(self, tree):
        """清空 Treeview 所有行"""
        for item in tree.get_children():
            tree.delete(item)

    def _clear_ui_data(self):
        """清空界面上的历史线条和表格"""
        for ui_dict in (self.wan_ui, self.lan_ui, self.custom_ui):
            ui_dict["rt_line"].set_data([], [])
            ui_dict["hist_line"].set_data([], [])
            ui_dict["hist_scatter"].set_offsets(np.empty((0, 2)))
            ui_dict["canvas"].draw_idle()
            self._clear_tree(ui_dict["tree_all"])
            self._clear_tree(ui_dict["tree_dur"])
        self._clear_tree(self.tree_diag)
        for key, value in {
            "count": "0 次",
            "total": "0 秒",
            "longest": "0 秒",
            "reason": "暂无",
        }.items():
            self.summary_labels[key].config(text=value)

    def clear_history_data(self):
        """清空数据库里的历史监控数据"""
        confirmed = messagebox.askyesno(
            "确认清空数据",
            "这会删除所有延迟记录和断网记录，导出报告也将无法再使用这些历史数据。\n\n确定要清空吗？",
            parent=self.root
        )
        if not confirmed:
            return

        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=10)
            c = conn.cursor()
            c.execute("DELETE FROM ping_data_v2")
            c.execute("DELETE FROM outages_v2")
            conn.commit()
            self._clear_ui_data()
            messagebox.showinfo("已清空", "历史监控数据已清空。", parent=self.root)
        except Exception as e:
            messagebox.showerror("清空失败", str(e), parent=self.root)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def update_diagnosis_table(self):
        """更新历史断网责任判定表格"""
        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()

            wan_placeholders = ','.join('?' * len(WAN_TARGETS))

            c.execute(
                f"SELECT start_time FROM outages_v2 "
                f"WHERE ip IN ({wan_placeholders}) ORDER BY start_time DESC LIMIT ?",
                (*WAN_TARGETS, TABLE_ROW_LIMIT)
            )
            wan_outages = c.fetchall()

            for item in self.tree_diag.get_children():
                self.tree_diag.delete(item)

            for row in wan_outages:
                t_start = row[0]
                time_str = datetime.fromtimestamp(t_start).strftime('%m-%d %H:%M:%S')

                # 动态查找：在案发前后 3 秒内网关的连通状态（排除所有外网目标 IP）
                c.execute(
                    f"SELECT ip, latency FROM ping_data_v2 "
                    f"WHERE ip NOT IN ({wan_placeholders}) AND timestamp BETWEEN ? AND ? "
                    f"ORDER BY ABS(timestamp - ?) ASC LIMIT 1",
                    (*WAN_TARGETS, t_start - 3, t_start + 3, t_start)
                )
                lan_status_result = c.fetchone()

                if lan_status_result:
                    lan_ip = lan_status_result[0]
                    lan_latency = lan_status_result[1]
                    if lan_latency == -1.0:
                        lan_status = "❌ 断开"
                        reason = "本地路由器"
                    else:
                        lan_status = "✅ 畅通"
                        reason = "光猫/运营商"
                else:
                    lan_ip = "无记录"
                    lan_status = "--"
                    reason = "早期单线数据"

                self.tree_diag.insert(
                    "", "end",
                    values=(time_str, lan_ip, lan_status, reason)
                )
        except Exception as e:
            print(f"[Diag] 诊断表刷新失败: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self.root.after(TABLE_INTERVAL_MS, self.update_diagnosis_table)

    def on_closing(self):
        self.is_running = False
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = NetworkMonitorApp(root)
    root.mainloop()
