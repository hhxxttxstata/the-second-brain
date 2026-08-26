"""悬浮球 Agent — tkinter 桌面壳（原生无边框/置顶/透明/拖动）。

形态:
  - 折叠: 64×64 圆形悬浮球（可随意拖动，置顶）
  - 展开: 400×560 对话框 + 右侧控制条（模型切换 / 上下文红绿灯）

启动:
    python -m app.float_ball
"""
from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 单实例锁：防止重复启动两个悬浮球进程 ──
# 两个进程同时写 SQLite（WAL）会偶发 [Errno 22] Invalid argument / 锁冲突
# 用绑定文件锁实现：锁文件被持有 → 直接退出
import os

_LOCK_PATH = Path(os.environ.get("TEMP", ".")) / "float_ball.lock"


def _acquire_single_instance() -> bool:
    """获取单实例锁。成功返回 True；已有实例则返回 False。"""
    global _lock_file
    try:
        _lock_file = open(_LOCK_PATH, "w", encoding="utf-8")
        import msvcrt
        msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
        return True
    except OSError:
        return False


_lock_file = None


def _release_single_instance() -> None:
    global _lock_file
    try:
        if _lock_file is not None:
            _lock_file.close()  # close 自动释放锁
            _lock_file = None
    except Exception:
        pass

BALL_SIZE = 64
PANEL_W, PANEL_H = 400, 560
BALL_BG = "#3d3d5c"
PANEL_BG = "#1e1e2e"
PANEL2 = "#27273a"
TEXT = "#e6e6f0"
DIM = "#9a9ab0"
ACCENT = "#7c9cff"
GREEN, YELLOW, RED = "#4caf50", "#ff9800", "#f44336"


class BallAgent:
    """对话核心（与 pywebview 版共用逻辑）。"""

    def __init__(self) -> None:
        self._last_trace = None
        self._last_input = ""

    def get_models(self) -> list[dict]:
        from app.agent.model_switch import list_models
        return list_models()

    def switch_model(self, model_id: str) -> None:
        from app.agent.model_switch import set_current_model
        set_current_model(model_id)

    def get_context_status(self) -> dict:
        try:
            from app.agent.context_pressure import measure_pressure
            from app.agent.session import get_default_session
            p = measure_pressure(session_id=get_default_session())
            return p
        except Exception:
            return {"usage_ratio": 0, "level": "green",
                    "history_tokens": 0, "history_budget": 4000,
                    "context_tokens": 0, "context_budget": 3500}

    def ask(self, text: str) -> dict:
        from app.agent.session import get_default_session, load_messages, append_exchange
        from app.agent.graphs.orchestrator import run_orchestrator
        sid = get_default_session()
        history = load_messages(sid)
        self._last_input = text
        for attempt in range(2):   # 失败重试一次（Windows 下偶发瞬时 IO/事件循环错误）
            try:
                r = run_orchestrator(input_text=text, thread_id=sid, conversation=history)
                if r.get("success"):
                    result, route = r.get("result", ""), r.get("route", "?")
                    if result:
                        append_exchange(sid, text, result)
                    try:
                        from app.agent.trace import get_latest_trace
                        self._last_trace = get_latest_trace()
                    except Exception:
                        pass
                    return {"success": True, "result": result, "route": route}
                # 瞬时错误（网络/IO）→ 重试一次
                err = r.get("error", "处理失败")
                if attempt == 0 and err and ("Errno" in err or "Connection" in err
                                             or "timed out" in err or "reset" in err.lower()):
                    continue
                return {"success": False, "error": err}
            except Exception as exc:
                if attempt == 0:
                    continue   # 重试
                import traceback as _tb
                _tb.print_exc()
                return {"success": False, "error": f"{exc}\n{traceback.format_exc()[-300:]}"}

    def send_feedback(self, failure_type: str) -> str:
        try:
            from app.feedback import new_feedback, save_feedback
            if not self._last_trace:
                return "无 trace"
            fb = new_feedback(
                trace_id=self._last_trace.get("trace_id", "?"),
                failure_type=failure_type,
                input_text=self._last_input,
                trace_data=self._last_trace,
            )
            return save_feedback(fb)
        except Exception:
            return "反馈失败"


class FloatBallApp:
    """tkinter 悬浮球 UI。"""

    def __init__(self) -> None:
        self.agent = BallAgent()

        self.root = tk.Tk()
        self.root.overrideredirect(True)          # 无边框
        self.root.attributes("-topmost", True)     # 置顶
        self.root.configure(bg=BALL_BG)
        self.root.geometry(f"{BALL_SIZE}x{BALL_SIZE}+200+200")

        self._dragging = False
        self._dx = 0
        self._dy = 0

        # 悬浮球（Canvas 画立体球）
        self.ball = tk.Canvas(self.root, width=BALL_SIZE, height=BALL_SIZE,
                              bg=BALL_BG, highlightthickness=0)
        self.ball.pack()
        self._draw_ball()

        # 拖动 + 单击展开（位移 <5px 视为点击）
        self._press_pos = (0, 0)
        self.ball.bind("<Button-1>", self._on_press)
        self.ball.bind("<B1-Motion>", self._on_drag)
        self.ball.bind("<ButtonRelease-1>", self._on_release)

        # 对话框（初始隐藏）
        self.panel = None
        self.chat_box = None
        self.entry = None
        self.light = None
        self.ctx_label = None
        self.model_var = None

    # ── 立体悬浮球 ──

    def _draw_ball(self) -> None:
        """画立体悬浮球：外圈阴影 → 球体渐变 → 高光 → 表情。"""
        self.ball.delete("all")
        s = BALL_SIZE
        # 1. 外圈深色阴影（立体感底座）
        self.ball.create_oval(2, 3, s - 2, s - 1, fill="#14142a", outline="#14142a")
        # 2. 球体主体（深蓝紫）
        self.ball.create_oval(4, 3, s - 4, s - 3, fill="#2a2a50", outline="")
        # 3. 左上高光层（模拟径向渐变亮部）
        self.ball.create_oval(s * 0.16, s * 0.10, s * 0.58, s * 0.46,
                              fill="#6a6ab5", outline="")
        # 4. 右下暗部（渐变暗区）
        self.ball.create_oval(s * 0.42, s * 0.52, s * 0.94, s * 0.96,
                              fill="#181838", outline="")
        # 5. 镜面高光小点（左上）
        self.ball.create_oval(s * 0.26, s * 0.18, s * 0.40, s * 0.32,
                              fill="#d0d0ff", outline="")
        # 6. 紫色光晕描边
        self.ball.create_oval(4, 3, s - 4, s - 3, outline=ACCENT, width=2)
        # 7. 表情（带轻微投影）
        self.ball.create_text(s // 2 + 1, s // 2 + 2,
                              text="🤖", font=("Segoe UI Emoji", 20),
                              fill="#000000")  # 投影（简化：黑色偏移层）
        self.ball.create_text(s // 2, s // 2,
                              text="🤖", font=("Segoe UI Emoji", 20))

    # ── 悬浮球拖动/点击 ──

    def _on_press(self, e) -> None:
        self._dragging = True
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()
        self._press_pos = (e.x_root, e.y_root)

    def _on_drag(self, e) -> None:
        if self._dragging:
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _on_release(self, e) -> None:
        self._dragging = False
        # 位移 < 5px = 点击 → 展开（拖动则不动）
        dx = abs(e.x_root - self._press_pos[0])
        dy = abs(e.y_root - self._press_pos[1])
        if dx < 5 and dy < 5:
            self.expand()

    # ── 展开对话框 ──

    def expand(self) -> None:
        self.root.geometry(f"{PANEL_W}x{PANEL_H}")
        self.root.configure(bg=PANEL_BG)

        # 清掉悬浮球
        self.ball.pack_forget()

        # 顶部栏
        top = tk.Frame(self.root, bg=PANEL2, height=36)
        top.pack(fill=tk.X)
        top.pack_propagate(False)
        tk.Label(top, text="🤖 Agent", bg=PANEL2, fg=TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT, padx=10)
        tk.Button(top, text="—", bg="#44445e", fg=TEXT, relief=tk.FLAT,
                  font=("Microsoft YaHei", 10), command=self.collapse,
                  width=3).pack(side=tk.RIGHT, padx=6, pady=4)

        # 对话区（Text + Scrollbar）
        frame = tk.Frame(self.root, bg=PANEL_BG)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        self.chat_box = tk.Text(frame, bg="#232338", fg=TEXT, relief=tk.FLAT,
                                font=("Microsoft YaHei", 10), wrap=tk.WORD,
                                state=tk.DISABLED, padx=8, pady=8,
                                cursor="arrow")
        sb = tk.Scrollbar(frame, command=self.chat_box.yview)
        self.chat_box.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 输入区
        inp = tk.Frame(self.root, bg=PANEL2)
        inp.pack(fill=tk.X, pady=(0, 8), padx=8)
        self.entry = tk.Entry(inp, bg="#33334a", fg=TEXT, relief=tk.FLAT,
                              font=("Microsoft YaHei", 10),
                              insertbackground=TEXT)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 6))
        self.entry.bind("<Return>", lambda _e: self.send())
        tk.Button(inp, text="发送", bg=ACCENT, fg="#101018", relief=tk.FLAT,
                  font=("Microsoft YaHei", 10, "bold"), command=self.send,
                  width=6).pack(side=tk.RIGHT)

        # 右侧控制条（模型切换 + 红绿灯）
        side = tk.Frame(self.root, bg=PANEL2)
        side.place(relx=1.0, rely=0.5, anchor="e", y=0)

        self.light = tk.Canvas(side, width=16, height=16, bg=PANEL2, highlightthickness=0)
        self.light.pack(pady=(10, 2))
        self.ctx_label = tk.Label(side, text="–", bg=PANEL2, fg=DIM,
                                  font=("Microsoft YaHei", 7))
        self.ctx_label.pack(pady=(0, 8))

        # 模型下拉（竖排 → 正常下拉，窄）
        self.model_var = tk.StringVar()
        models = self.agent.get_models()
        if models:
            self.model_var.set(next((m["id"] for m in models if m.get("current")), models[0]["id"]))
        self.model_menu = tk.OptionMenu(side, self.model_var,
                                        *[m["id"] for m in models],
                                        command=self._switch_model)
        self.model_menu.config(bg="#33334a", fg=TEXT, relief=tk.FLAT,
                               highlightthickness=0, font=("Microsoft YaHei", 8),
                               width=10)
        self.model_menu.pack(pady=(0, 10))

        self.refresh_light()
        self.entry.focus_set()

    def _switch_model(self, mid: str) -> None:
        try:
            self.agent.switch_model(mid)
            self._append("[已切换模型]\n", DIM)
        except Exception as exc:
            self._append(f"[切换失败: {exc}]\n", RED)

    # ── 收起 ──

    def collapse(self) -> None:
        if self.panel is not None:
            self.panel.destroy()
            self.panel = None
        # 清掉所有子组件，回到悬浮球
        for w in self.root.winfo_children():
            w.destroy()
        self.root.configure(bg=BALL_BG)
        self.root.geometry(f"{BALL_SIZE}x{BALL_SIZE}")
        self.ball = tk.Canvas(self.root, width=BALL_SIZE, height=BALL_SIZE,
                              bg=BALL_BG, highlightthickness=0)
        self.ball.pack()
        self._draw_ball()
        self.ball.bind("<Button-1>", self._on_press)
        self.ball.bind("<B1-Motion>", self._on_drag)
        self.ball.bind("<ButtonRelease-1>", self._on_release)

    # ── 对话 ──

    def _append(self, text: str, color: str = TEXT) -> None:
        if self.chat_box is None:
            return
        self.chat_box.configure(state=tk.NORMAL)
        self.chat_box.insert(tk.END, text, ("c",))
        self.chat_box.tag_config("c", foreground=color)
        self.chat_box.configure(state=tk.DISABLED)
        self.chat_box.see(tk.END)

    def send(self) -> None:
        if self.entry is None:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, tk.END)
        self._append(f"你: {text}\n", ACCENT)
        self._append("…思考中\n", DIM)

        # 后台线程调用（不卡 UI）
        def work():
            r = self.agent.ask(text)
            self.root.after(0, lambda: self._show_result(r))

        threading.Thread(target=work, daemon=True).start()

    def _show_result(self, r: dict) -> None:
        if r.get("success"):
            route = r.get("route", "?")
            self._append(f"[→ {route}]\n", DIM)
            self._append(f"{r.get('result', '')}\n\n", TEXT)
        else:
            self._append(f"❌ {r.get('error', '处理失败')}\n\n", RED)
        self.refresh_light()

    def refresh_light(self) -> None:
        if self.light is None:
            return
        try:
            ctx = self.agent.get_context_status()
            level = ctx.get("level", "green")
            color = {"green": GREEN, "yellow": YELLOW, "red": RED}.get(level, GREEN)
            self.light.delete("all")
            self.light.create_oval(2, 2, 14, 14, fill=color, outline=color)
            pct = int(ctx.get("usage_ratio", 0) * 100)
            self.ctx_label.configure(text=f"{pct}%")
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if not _acquire_single_instance():
        print("⚠️ 悬浮球已在运行（单实例锁）。如需重启请先关闭现有窗口。")
        return
    try:
        app = FloatBallApp()
        app.run()
    finally:
        _release_single_instance()


if __name__ == "__main__":
    main()
