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

def get_default_gateway():
    """跨平台获取当前默认网关 IP"""
    system = platform.system().lower()
    try:
        if system == 'windows':
            result = subprocess.run(['route', 'print', '0.0.0.0'], stdout=subprocess.PIPE, text=True, errors='ignore')
            for line in result.stdout.splitlines():
                if '0.0.0.0' in line:
                    parts = line.split()
                    if len(parts) >= 3 and parts[0] == '0.0.0.0':
                        return parts[2]
        elif system == 'darwin':
            result = subprocess.run(['route', '-n', 'get', 'default'], stdout=subprocess.PIPE, text=True, errors='ignore')
            for line in result.stdout.splitlines():
                if 'gateway:' in line:
                    return line.split(':')[1].strip()
        else:
            result = subprocess.run(['ip', 'route'], stdout=subprocess.PIPE, text=True, errors='ignore')
            for line in result.stdout.splitlines():
                if line.startswith('default via'):
                    return line.split()[2]
    except Exception:
        pass
    
    return "192.168.1.1" # 兜底值

TARGET_WAN = "223.5.5.5"
TARGET_LAN = get_default_gateway() 

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "ping_history.db")

class NetworkMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"全链路网络诊断看板 - 自动识别网关 ({TARGET_LAN})")
        self.root.geometry("1350x900")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.is_running = True
        self.lat_wan = -1
        self.lat_lan = -1
        
        self.setup_ui()
        self.start_monitor_threads()
        
        self.update_ui_elements()
        self.update_graphs()
        self.update_tables()
        self.update_realtime_diagnosis()
        self.update_diagnosis_table()

    def setup_ui(self):
        # --- 顶部全局状态栏 ---
        header_frame = tk.Frame(self.root, pady=10, bg="#2c3e50")
        header_frame.pack(fill=tk.X)

        self.status_wan_label = tk.Label(header_frame, text=f"外网 ({TARGET_WAN}): 正在检测...", font=("Microsoft YaHei", 14, "bold"), bg="#2c3e50", fg="white")
        self.status_wan_label.pack(side=tk.LEFT, expand=True)

        self.status_lan_label = tk.Label(header_frame, text=f"网关 ({TARGET_LAN}): 正在检测...", font=("Microsoft YaHei", 14, "bold"), bg="#2c3e50", fg="white")
        self.status_lan_label.pack(side=tk.RIGHT, expand=True)

        # --- 主体容器 ---
        main_body = tk.Frame(self.root)
        main_body.pack(fill=tk.BOTH, expand=True)

        # ================= 右侧：智能诊断面板 =================
        self.right_panel = tk.Frame(main_body, width=370, bg="#f8f9fa", relief=tk.GROOVE, bd=2)
        self.right_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 10), pady=10)
        self.right_panel.pack_propagate(False) 

        tk.Label(self.right_panel, text="🤖 实时故障诊断", font=("Microsoft YaHei", 12, "bold"), bg="#f8f9fa").pack(pady=(15, 5))
        
        self.diag_text = tk.Text(self.right_panel, height=7, font=("Microsoft YaHei", 10), wrap=tk.WORD, relief=tk.FLAT, padx=10, pady=10)
        self.diag_text.pack(fill=tk.X, padx=10)
        self.diag_text.insert(tk.END, "正在分析当前网络环境...")
        self.diag_text.config(state=tk.DISABLED)

        ttk.Separator(self.right_panel, orient='horizontal').pack(fill=tk.X, pady=15, padx=10)

        # 历史责任判定表格 (新增网关IP列)
        tk.Label(self.right_panel, text="📋 历史断网责任判定", font=("Microsoft YaHei", 12, "bold"), bg="#f8f9fa").pack(pady=(0, 10))
        
        diag_frame = tk.Frame(self.right_panel)
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

        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas_scroll.configure(scrollregion=self.canvas_scroll.bbox("all")))
        self.canvas_scroll.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas_scroll.configure(yscrollcommand=self.scrollbar.set)
        self.canvas_scroll.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        self.wan_ui = self.create_monitor_section(self.scrollable_frame, f"🌐 外网链路监控 (Internet - {TARGET_WAN})", "#3498db")
        self.lan_ui = self.create_monitor_section(self.scrollable_frame, f"🖧 路由器网关监控 (Local - {TARGET_LAN})", "#27ae60")

    def _on_mousewheel(self, event):
        self.canvas_scroll.yview_scroll(int(-1*(event.delta/120)), "units")

    def create_monitor_section(self, parent, title_text, color_theme):
        frame = tk.LabelFrame(parent, text=title_text, font=("Microsoft YaHei", 14, "bold"), fg=color_theme, padx=10, pady=10)
        frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        main_container = tk.Frame(frame)
        main_container.pack(fill=tk.BOTH, expand=True)

        chart_frame = tk.Frame(main_container)
        chart_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        fig, (ax_rt, ax_hist) = plt.subplots(2, 1, figsize=(7, 6), dpi=90)
        fig.tight_layout(pad=4.0)
        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        list_frame = tk.Frame(main_container, width=320)
        list_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(10, 0))

        tk.Label(list_frame, text="所有单次记录", font=("Microsoft YaHei", 10, "bold")).pack(pady=(0, 5))
        frame_tv1 = tk.Frame(list_frame)
        frame_tv1.pack(fill=tk.BOTH, expand=True)
        tree_all = ttk.Treeview(frame_tv1, columns=("time", "status"), show="headings", height=8)
        tree_all.heading("time", text="断网发生时间")
        tree_all.heading("status", text="状态")
        tree_all.column("time", width=160, anchor=tk.CENTER)
        tree_all.column("status", width=80, anchor=tk.CENTER)
        scroll1 = ttk.Scrollbar(frame_tv1, orient=tk.VERTICAL, command=tree_all.yview)
        tree_all.configure(yscroll=scroll1.set)
        scroll1.pack(side=tk.RIGHT, fill=tk.Y)
        tree_all.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Separator(list_frame, orient='horizontal').pack(fill=tk.X, pady=10)

        tk.Label(list_frame, text="中断持续时间统计", font=("Microsoft YaHei", 10, "bold")).pack(pady=(0, 5))
        frame_tv2 = tk.Frame(list_frame)
        frame_tv2.pack(fill=tk.BOTH, expand=True)
        tree_dur = ttk.Treeview(frame_tv2, columns=("start", "duration"), show="headings", height=8)
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
            "tree_all": tree_all, "tree_dur": tree_dur,
            "color": color_theme
        }

    def ping_ip(self, ip):
        system = platform.system().lower()
        cmd = ['ping', '-n', '1', '-w', '1000', ip] if system == 'windows' else ['ping', '-c', '1', '-W', '1', ip]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='ignore')
            if result.returncode == 0:
                match = re.search(r'([0-9.]+)\s*ms', result.stdout, re.IGNORECASE)
                return float(match.group(1)) if match else 1.0
            return -1.0
        except:
            return -1.0

    def monitor_loop(self, target_ip):
        conn = sqlite3.connect(DB_NAME, timeout=10)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS ping_data_v2 (timestamp REAL, ip TEXT, latency REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS outages_v2 (start_time REAL, end_time REAL, ip TEXT)''')
        
        was_offline = False
        outage_start = None
        
        c.execute("SELECT start_time FROM outages_v2 WHERE end_time IS NULL AND ip = ? ORDER BY start_time DESC LIMIT 1", (target_ip,))
        row = c.fetchone()
        if row:
            was_offline = True
            outage_start = row[0]
        conn.commit()

        while self.is_running:
            latency = self.ping_ip(target_ip)
            if target_ip == TARGET_WAN: self.lat_wan = latency
            else: self.lat_lan = latency
            
            now = time.time()
            for _ in range(3):
                try:
                    c.execute("INSERT INTO ping_data_v2 VALUES (?, ?, ?)", (now, target_ip, latency))
                    if latency == -1:
                        if not was_offline:
                            was_offline = True
                            outage_start = now
                            c.execute("INSERT INTO outages_v2 (start_time, end_time, ip) VALUES (?, NULL, ?)", (outage_start, target_ip))
                    else:
                        if was_offline:
                            was_offline = False
                            c.execute("UPDATE outages_v2 SET end_time = ? WHERE start_time = ? AND ip = ?", (now, outage_start, target_ip))
                            outage_start = None

                    c.execute("DELETE FROM ping_data_v2 WHERE timestamp < ?", (now - 259200,))
                    c.execute("DELETE FROM outages_v2 WHERE start_time < ?", (now - 259200,))
                    conn.commit()
                    break
                except sqlite3.OperationalError:
                    time.sleep(0.1)
            time.sleep(1)
        conn.close()

    def start_monitor_threads(self):
        self.thread_wan = threading.Thread(target=self.monitor_loop, args=(TARGET_WAN,), daemon=True)
        self.thread_lan = threading.Thread(target=self.monitor_loop, args=(TARGET_LAN,), daemon=True)
        self.thread_wan.start()
        self.thread_lan.start()

    def update_ui_elements(self):
        if self.lat_wan != -1: self.status_wan_label.config(text=f"🌐 外网 ({TARGET_WAN}): 正常 ({self.lat_wan} ms)", fg="#2ecc71")
        else: self.status_wan_label.config(text=f"🌐 外网 ({TARGET_WAN}): 丢包断开", fg="#e74c3c")
            
        if self.lat_lan != -1: self.status_lan_label.config(text=f"🖧 网关 ({TARGET_LAN}): 正常 ({self.lat_lan} ms)", fg="#2ecc71")
        else: self.status_lan_label.config(text=f"🖧 网关 ({TARGET_LAN}): 丢包断开", fg="#e74c3c")
        self.root.after(1000, self.update_ui_elements)

    def draw_charts(self, ui_dict, target_ip, label_name):
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()
            now = time.time()
            
            c.execute("SELECT timestamp, latency FROM ping_data_v2 WHERE ip = ? AND timestamp >= ? ORDER BY timestamp ASC", (target_ip, now - 60))
            data_rt = c.fetchall()
            if data_rt:
                times_rt = [datetime.fromtimestamp(r[0]) for r in data_rt]
                lats_rt = [r[1] if r[1] != -1 else 0 for r in data_rt]
                ui_dict["ax_rt"].clear()
                ui_dict["ax_rt"].plot(times_rt, lats_rt, color=ui_dict["color"], linewidth=2, label=f"{label_name} 实时延迟 (ms)")
                ui_dict["ax_rt"].set_title("最近 60 秒实时动态", fontsize=10)
                ui_dict["ax_rt"].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
                ui_dict["ax_rt"].grid(True, linestyle=':', alpha=0.7)
                ui_dict["ax_rt"].legend(loc="upper right")

            c.execute("SELECT timestamp, latency FROM ping_data_v2 WHERE ip = ? AND timestamp >= ? ORDER BY timestamp ASC", (target_ip, now - 86400))
            data_hist = c.fetchall()
            if data_hist:
                times_h = [datetime.fromtimestamp(r[0]) for r in data_hist]
                lats_h = [r[1] for r in data_hist]
                ui_dict["ax_hist"].clear()
                clean_times = [times_h[i] for i in range(len(lats_h)) if lats_h[i] != -1]
                clean_lats = [lats_h[i] for i in range(len(lats_h)) if lats_h[i] != -1]
                drop_times = [times_h[i] for i in range(len(lats_h)) if lats_h[i] == -1]
                ui_dict["ax_hist"].plot(clean_times, clean_lats, color='#2c3e50', linewidth=1, label="历史延迟")
                if drop_times: ui_dict["ax_hist"].scatter(drop_times, [0]*len(drop_times), color='red', s=10, label="中断点")
                ui_dict["ax_hist"].set_title("过去 24 小时趋势", fontsize=10)
                ui_dict["ax_hist"].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
                ui_dict["ax_hist"].grid(True, linestyle=':', alpha=0.7)
                ui_dict["ax_hist"].legend(prop={'size': 8}, loc="upper right")
            
            conn.close()
            ui_dict["canvas"].draw()
        except: pass

    def update_graphs(self):
        self.draw_charts(self.wan_ui, TARGET_WAN, "外网")
        self.draw_charts(self.lan_ui, TARGET_LAN, "网关")
        self.root.after(1000, self.update_graphs)

    def refresh_table_data(self, ui_dict, target_ip):
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()
            c.execute("SELECT timestamp FROM ping_data_v2 WHERE ip = ? AND latency = -1 ORDER BY timestamp DESC", (target_ip,))
            for item in ui_dict["tree_all"].get_children(): ui_dict["tree_all"].delete(item)
            for r in c.fetchall(): ui_dict["tree_all"].insert("", "end", values=(datetime.fromtimestamp(r[0]).strftime('%Y-%m-%d %H:%M:%S'), "超时断开"))

            c.execute("SELECT start_time, end_time FROM outages_v2 WHERE ip = ? ORDER BY start_time DESC", (target_ip,))
            for item in ui_dict["tree_dur"].get_children(): ui_dict["tree_dur"].delete(item)
            for r in c.fetchall():
                start_str = datetime.fromtimestamp(r[0]).strftime('%m-%d %H:%M:%S')
                dur_str = "仍在中断中..." if r[1] is None else (f"{int(r[1]-r[0])} 秒" if int(r[1]-r[0]) < 60 else f"{int(r[1]-r[0])//60}分 {int(r[1]-r[0])%60}秒")
                ui_dict["tree_dur"].insert("", "end", values=(start_str, dur_str))
            conn.close()
        except: pass

    def update_tables(self):
        self.refresh_table_data(self.wan_ui, TARGET_WAN)
        self.refresh_table_data(self.lan_ui, TARGET_LAN)
        self.root.after(5000, self.update_tables)

    def update_realtime_diagnosis(self):
        self.diag_text.config(state=tk.NORMAL)
        self.diag_text.delete(1.0, tk.END)
        
        if self.lat_wan == -1:
            if self.lat_lan == -1:
                msg = "⚠️ 警报：外网与网关均已中断！\n\n🔍 判定：【本地网络故障】\n💡 建议：你的电脑连不上路由器了。请检查网线是否脱落、Wi-Fi是否断开，或者路由器是否死机需要拔插头重启。"
                self.diag_text.config(bg="#fdedec", fg="#c0392b") 
            else:
                msg = f"⚠️ 警报：外网断开，但网关({TARGET_LAN})畅通！\n\n🔍 判定：【外部宽带故障】\n💡 建议：你的路由器和电脑正常。绝对是光猫断网、宽带欠费，或者运营商所在片区机房故障，请联系宽带客服。"
                self.diag_text.config(bg="#fcf3cf", fg="#b7950b") 
        else:
             msg = f"✅ 当前网络一切正常。\n\n外网畅通，路由器网关({TARGET_LAN})响应良好。系统正在持续监控中..."
             self.diag_text.config(bg="#e8f8f5", fg="#117a65")    
             
        self.diag_text.insert(tk.END, msg)
        self.diag_text.config(state=tk.DISABLED)
        self.root.after(1000, self.update_realtime_diagnosis)

    def update_diagnosis_table(self):
        try:
            conn = sqlite3.connect(DB_NAME, timeout=5)
            c = conn.cursor()
            
            c.execute("SELECT start_time FROM outages_v2 WHERE ip = ? ORDER BY start_time DESC", (TARGET_WAN,))
            wan_outages = c.fetchall()
            
            for item in self.tree_diag.get_children():
                self.tree_diag.delete(item)
                
            for row in wan_outages:
                t_start = row[0]
                time_str = datetime.fromtimestamp(t_start).strftime('%m-%d %H:%M:%S')
                
                # 动态查找：在案发前后3秒内，除了外网IP之外的任意IP（即当时的网关IP）的连通状态
                c.execute("SELECT ip, MIN(latency) FROM ping_data_v2 WHERE ip != ? AND timestamp BETWEEN ? AND ? GROUP BY ip", (TARGET_WAN, t_start - 3, t_start + 3))
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
                    
                self.tree_diag.insert("", "end", values=(time_str, lan_ip, lan_status, reason))
            conn.close()
        except: pass
        self.root.after(5000, self.update_diagnosis_table)

    def on_closing(self):
        self.is_running = False
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except: pass
    app = NetworkMonitorApp(root)
    root.mainloop()