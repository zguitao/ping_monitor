import tkinter as tk
from tkinter import ttk
import subprocess
import platform
import re
import time
import threading
import sqlite3
import os
from datetime import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.dates as mdates

# 配置 Matplotlib 支持中文显示
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

# ==================== 常量配置 ====================
TARGET_WAN = "223.5.5.5"

# 常见网关地址（获取失败时轮询）
COMMON_GATEWAYS = ("192.168.1.1", "192.168.0.1", "10.0.0.1", "192.168.31.1")

PING_COUNT = 1            # 每次 ping 发送的包数
PING_INTERVAL = 1         # ping 间隔（秒）
PING_TIMEOUT_MS = 1000    # ping 超时（毫秒）
CLEANUP_AGE = 259200      # 数据保留时长（3 天，秒）
HISTORY_WINDOW = 86400    # 历史图时间窗口（24 小时，秒）
REALTIME_WINDOW = 60      # 实时图时间窗口（秒）

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
        self._lock = threading.Lock()

        self.setup_ui()
        self._init_db()
        self.start_monitor_threads()

        self.update_ui_elements()
        self.update_graphs()
        self.update_tables()
        self.update_realtime_diagnosis()
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
            text=f"外网 ({TARGET_WAN}): 正在检测...",
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
        right_panel = tk.Frame(main_body, width=370, bg="#f8f9fa", relief=tk.GROOVE, bd=2)
        right_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 10), pady=10)
        right_panel.pack_propagate(False)

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

        ttk.Separator(right_panel, orient='horizontal').pack(fill=tk.X, pady=15, padx=10)

        # 历史责任判定表格
        tk.Label(
            right_panel, text="📋 历史断网责任判定",
            font=("Microsoft YaHei", 12, "bold"), bg="#f8f9fa"
        ).pack(pady=(0, 10))

        diag_frame = tk.Frame(right_panel)
        diag_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 15))

        cols_diag = ("time", "lan_ip", "lan_status", "reason")
        self.tree_diag = ttk.Treeview(diag_frame, columns=cols_diag, show="headings")
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
        self.canvas_scroll.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas_scroll.configure(yscrollcommand=self.scrollbar.set)
        self.canvas_scroll.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            self.canvas_scroll.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.root.bind_all("<MouseWheel>", _on_mousewheel)

        self.wan_ui = self._create_monitor_section(
            self.scrollable_frame,
            f"🌐 外网链路监控 (Internet - {TARGET_WAN})",
            "#3498db"
        )
        self.lan_ui = self._create_monitor_section(
            self.scrollable_frame,
            f"🖧 路由器网关监控 (Local - {TARGET_LAN})",
            "#27ae60"
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
            "fig": fig, "canvas": canvas,
            "ax_rt": ax_rt, "ax_hist": ax_hist,
            "rt_line": rt_line, "hist_line": hist_line,
            "hist_scatter": hist_scatter,
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

    def monitor_loop(self, target_ip):
        """后台线程：持续 ping 指定 IP，写入 SQLite 并跟踪中断事件"""
        conn = sqlite3.connect(DB_NAME, timeout=10)
        try:
            c = conn.cursor()
            was_offline = False
            outage_start = None

            # 恢复上次未结束的中断
            c.execute(
                "SELECT start_time FROM outages_v2 WHERE end_time IS NULL "
                "AND ip = ? ORDER BY start_time DESC LIMIT 1",
                (target_ip,)
            )
            row = c.fetchone()
            if row:
                was_offline = True
                outage_start = row[0]

            while self.is_running:
                latency = self.ping_ip(target_ip)

                # 线程安全更新共享延迟值
                with self._lock:
                    if target_ip == TARGET_WAN:
                        self.lat_wan = latency
                    else:
                        self.lat_lan = latency

                now = time.time()
                for attempt in range(3):
                    try:
                        c.execute(
                            "INSERT INTO ping_data_v2 VALUES (?, ?, ?)",
                            (now, target_ip, latency)
                        )
                        if latency == -1:
                            if not was_offline:
                                was_offline = True
                                outage_start = now
                                c.execute(
                                    "INSERT INTO outages_v2 (start_time, end_time, ip) "
                                    "VALUES (?, NULL, ?)",
                                    (outage_start, target_ip)
                                )
                        else:
                            if was_offline:
                                was_offline = False
                                c.execute(
                                    "UPDATE outages_v2 SET end_time = ? "
                                    "WHERE start_time = ? AND ip = ?",
                                    (now, outage_start, target_ip)
                                )
                                outage_start = None

                        c.execute(
                            "DELETE FROM ping_data_v2 WHERE timestamp < ?",
                            (now - CLEANUP_AGE,)
                        )
                        c.execute(
                            "DELETE FROM outages_v2 WHERE start_time < ?",
                            (now - CLEANUP_AGE,)
                        )
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
        """启动双线程分别监控外网和网关"""
        self.thread_wan = threading.Thread(
            target=self.monitor_loop, args=(TARGET_WAN,), daemon=True
        )
        self.thread_lan = threading.Thread(
            target=self.monitor_loop, args=(TARGET_LAN,), daemon=True
        )
        self.thread_wan.start()
        self.thread_lan.start()

    # ---- UI 更新 ----

    def update_ui_elements(self):
        """更新顶部状态栏（每秒）"""
        with self._lock:
            wan_ok = self.lat_wan != -1
            lan_ok = self.lat_lan != -1
            wan_lat = self.lat_wan
            lan_lat = self.lat_lan

        if wan_ok:
            self.status_wan_label.config(
                text=f"🌐 外网 ({TARGET_WAN}): 正常 ({wan_lat} ms)", fg="#2ecc71"
            )
        else:
            self.status_wan_label.config(
                text=f"🌐 外网 ({TARGET_WAN}): 丢包断开", fg="#e74c3c"
            )

        if lan_ok:
            self.status_lan_label.config(
                text=f"🖧 网关 ({TARGET_LAN}): 正常 ({lan_lat} ms)", fg="#2ecc71"
            )
        else:
            self.status_lan_label.config(
                text=f"🖧 网关 ({TARGET_LAN}): 丢包断开", fg="#e74c3c"
            )
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
                    ui_dict["hist_scatter"].set_offsets([])

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
        self._draw_charts(self.wan_ui, TARGET_WAN, "外网")
        self._draw_charts(self.lan_ui, TARGET_LAN, "网关")
        self.root.after(UI_INTERVAL_MS, self.update_graphs)

    def _refresh_table_data(self, ui_dict, target_ip):
        """刷新某个 IP 的中断记录表格"""
        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()

            # 所有单次断网记录
            c.execute(
                "SELECT timestamp FROM ping_data_v2 "
                "WHERE ip = ? AND latency = -1 ORDER BY timestamp DESC",
                (target_ip,)
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
                "WHERE ip = ? ORDER BY start_time DESC",
                (target_ip,)
            )
            for item in ui_dict["tree_dur"].get_children():
                ui_dict["tree_dur"].delete(item)
            for r in c.fetchall():
                start_str = datetime.fromtimestamp(r[0]).strftime('%m-%d %H:%M:%S')
                if r[1] is None:
                    dur_str = "仍在中断中..."
                else:
                    secs = int(r[1] - r[0])
                    dur_str = (
                        f"{secs} 秒" if secs < 60
                        else f"{secs // 60}分 {secs % 60}秒"
                    )
                ui_dict["tree_dur"].insert("", "end", values=(start_str, dur_str))
        except Exception as e:
            print(f"[Table] {target_ip} 刷新失败: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def update_tables(self):
        """定时刷新两侧表格（每 5 秒）"""
        self._refresh_table_data(self.wan_ui, TARGET_WAN)
        self._refresh_table_data(self.lan_ui, TARGET_LAN)
        self.root.after(TABLE_INTERVAL_MS, self.update_tables)

    def update_realtime_diagnosis(self):
        """实时诊断：根据内外网状态给出故障判定"""
        with self._lock:
            wan_down = self.lat_wan == -1
            lan_down = self.lat_lan == -1

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
                    f"⚠️ 警报：外网断开，但网关({TARGET_LAN})畅通！\n\n"
                    "🔍 判定：【外部宽带故障】\n"
                    "💡 建议：你的路由器和电脑正常。"
                    "绝对是光猫断网、宽带欠费，"
                    "或者运营商所在片区机房故障，请联系宽带客服。"
                )
                self.diag_text.config(bg="#fcf3cf", fg="#b7950b")
        else:
            msg = (
                "✅ 当前网络一切正常。\n\n"
                f"外网畅通，路由器网关({TARGET_LAN})响应良好。"
                "系统正在持续监控中..."
            )
            self.diag_text.config(bg="#e8f8f5", fg="#117a65")

        self.diag_text.insert(tk.END, msg)
        self.diag_text.config(state=tk.DISABLED)
        self.root.after(UI_INTERVAL_MS, self.update_realtime_diagnosis)

    def update_diagnosis_table(self):
        """更新历史断网责任判定表格"""
        conn = None
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()

            c.execute(
                "SELECT start_time FROM outages_v2 "
                "WHERE ip = ? ORDER BY start_time DESC",
                (TARGET_WAN,)
            )
            wan_outages = c.fetchall()

            for item in self.tree_diag.get_children():
                self.tree_diag.delete(item)

            for row in wan_outages:
                t_start = row[0]
                time_str = datetime.fromtimestamp(t_start).strftime('%m-%d %H:%M:%S')

                # 动态查找：在案发前后 3 秒内网关的连通状态
                c.execute(
                    "SELECT ip, MIN(latency) FROM ping_data_v2 "
                    "WHERE ip != ? AND timestamp BETWEEN ? AND ? GROUP BY ip",
                    (TARGET_WAN, t_start - 3, t_start + 3)
                )
                lan_status_result = c.fetchone()

                if lan_status_result:
                    lan_ip = lan_status_result[0]
                    lan_min_lat = lan_status_result[1]
                    if lan_min_lat == -1.0:
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
