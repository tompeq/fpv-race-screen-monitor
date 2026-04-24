"""
Screen Monitor Viewer - сервер, который принимает скриншоты от агентов
и отображает их в едином окне, а также принимает распознанное время круга.

Зависимости:
    pip install Pillow

Использование:
    python monitor.py --port 9900 --columns 3 --fps 30
"""

from __future__ import annotations

import argparse
import io
import json
import socket
import struct
import threading
import time
import tkinter as tk
from collections import OrderedDict
from tkinter import ttk

from PIL import Image, ImageTk


def parse_time_to_ms(value: str) -> int | None:
    try:
        minutes_part, rest = value.split(":", 1)
        seconds_part, millis_part = rest.split(".", 1)
        minutes = int(minutes_part)
        seconds = int(seconds_part)
        millis = int(millis_part)
    except ValueError:
        return None

    if not (0 <= seconds < 60 and 0 <= millis < 1000 and minutes >= 0):
        return None
    return minutes * 60_000 + seconds * 1_000 + millis


def format_clock(seconds_left: int) -> str:
    minutes, seconds = divmod(max(seconds_left, 0), 60)
    return f"{minutes:02d}:{seconds:02d}"


def parse_duration_input(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None

    if ":" in text:
        try:
            minutes_text, seconds_text = text.split(":", 1)
            minutes = int(minutes_text)
            seconds = int(seconds_text)
        except ValueError:
            return None
        if minutes < 0 or not (0 <= seconds < 60):
            return None
        total = minutes * 60 + seconds
        return total if total > 0 else None

    try:
        total = int(text)
    except ValueError:
        return None
    return total if total > 0 else None


class AgentConnection(threading.Thread):
    """Поток для приёма данных от одного агента."""

    def __init__(self, conn: socket.socket, addr, monitor: "ScreenMonitor"):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.monitor = monitor
        self.running = True
        self.conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)

    def recv_exact(self, size: int) -> bytes:
        parts = []
        remaining = size
        while remaining > 0:
            chunk = self.conn.recv(min(remaining, 262144))
            if not chunk:
                raise ConnectionError("Соединение закрыто")
            parts.append(chunk)
            remaining -= len(chunk)
        return b"".join(parts)

    def run(self):
        try:
            while self.running:
                name_len = struct.unpack("!I", self.recv_exact(4))[0]
                name = self.recv_exact(name_len).decode("utf-8")

                payload_len = struct.unpack("!I", self.recv_exact(4))[0]
                payload = self.recv_exact(payload_len)

                metadata = {}
                img_data = payload

                if payload_len <= 8192:
                    try:
                        candidate = json.loads(payload.decode("utf-8"))
                        if isinstance(candidate, dict):
                            metadata = candidate
                            img_len = struct.unpack("!I", self.recv_exact(4))[0]
                            img_data = self.recv_exact(img_len)
                    except (UnicodeDecodeError, json.JSONDecodeError, struct.error):
                        metadata = {}
                        img_data = payload

                self.monitor.update_frame(name, img_data, metadata)

        except (ConnectionError, struct.error, OSError) as error:
            print(f"[Monitor] Агент {self.addr} отключился: {error}")
        finally:
            self.conn.close()
            self.monitor.remove_agent_by_connection(self)


class ScreenMonitor:
    """Главное окно монитора с сеткой экранов и зачётом времени."""

    def __init__(self, port: int = 9900, columns: int = 3, fps: int = 30):
        self.port = port
        self.columns = columns
        self.refresh_ms = max(1000 // fps, 16)
        self.frames: OrderedDict[str, bytes] = OrderedDict()
        self.dirty: set[str] = set()
        self.photo_images: dict[str, ImageTk.PhotoImage] = {}
        self.connections: list[AgentConnection] = []
        self.lock = threading.Lock()
        self.running = True
        self._last_cell_size = (0, 0)

        self.display_names: dict[str, str] = {}
        self.zoom_factor: float = 1.0

        self.drag_source_name: str | None = None
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.drag_active = False
        self._current_drop_target: str | None = None
        self.drag_threshold = 5

        self.agent_live_times: dict[str, str] = {}
        self.agent_last_results: dict[str, str] = {}
        self.round_start_live_times: dict[str, str] = {}
        self.best_result_name = ""
        self.best_result_time = ""
        self.best_result_ms: int | None = None
        self.round_active = False
        self.round_duration_seconds = 180
        self.round_end_at = 0.0

        self.root = tk.Tk()
        self.root.title("Screen Monitor")
        self.root.configure(bg="#0a0a0a")
        self.root.geometry("1400x900")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.resizable(True, True)

        top = tk.Frame(self.root, bg="#111111")
        top.pack(fill=tk.X)

        top_row = tk.Frame(top, bg="#111111")
        top_row.pack(fill=tk.X, padx=20, pady=(10, 6))

        left_bar = tk.Frame(top_row, bg="#111111")
        left_bar.pack(side=tk.LEFT)

        tk.Label(left_bar, text="MONITOR", font=("Segoe UI", 11, "bold"), fg="#ffffff", bg="#111111").pack(
            side=tk.LEFT
        )

        self.status_label = tk.Label(
            left_bar,
            text="",
            font=("Segoe UI", 9),
            fg="#666666",
            bg="#111111",
        )
        self.status_label.pack(side=tk.LEFT, padx=12)

        self.best_label = tk.Label(
            top_row,
            text="Лучшее время: --",
            font=("Segoe UI", 10, "bold"),
            fg="#f9e2af",
            bg="#111111",
            anchor="center",
        )
        self.best_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=24)

        right_bar = tk.Frame(top_row, bg="#111111")
        right_bar.pack(side=tk.RIGHT)

        self.fps_label = tk.Label(right_bar, text="", font=("Segoe UI", 8), fg="#444444", bg="#111111")
        self.fps_label.pack(side=tk.RIGHT, padx=10)

        tk.Frame(right_bar, width=1, bg="#222222").pack(side=tk.RIGHT, fill=tk.Y, padx=14)

        tk.Button(
            right_bar,
            text="-",
            font=("Segoe UI", 11),
            width=2,
            height=1,
            command=self._zoom_out,
            bg="#222222",
            fg="#999999",
            relief=tk.FLAT,
            cursor="hand2",
            activeforeground="#ffffff",
            activebackground="#333333",
        ).pack(side=tk.RIGHT, padx=(2, 0))

        self.zoom_label = tk.Label(right_bar, text="100%", font=("Segoe UI", 9), fg="#666666", bg="#111111")
        self.zoom_label.pack(side=tk.RIGHT, padx=6)

        tk.Button(
            right_bar,
            text="+",
            font=("Segoe UI", 11),
            width=2,
            height=1,
            command=self._zoom_in,
            bg="#222222",
            fg="#999999",
            relief=tk.FLAT,
            cursor="hand2",
            activeforeground="#ffffff",
            activebackground="#333333",
        ).pack(side=tk.RIGHT)

        controls_row = tk.Frame(top, bg="#111111")
        controls_row.pack(fill=tk.X, padx=20, pady=(0, 10))

        left_controls = tk.Frame(controls_row, bg="#111111")
        left_controls.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(left_controls, text="Раунд", font=("Segoe UI", 9), fg="#aaaaaa", bg="#111111").pack(
            side=tk.LEFT, padx=(0, 8)
        )

        self.round_entry = tk.Entry(
            left_controls,
            width=8,
            font=("Segoe UI", 10),
            bg="#161616",
            fg="#ffffff",
            insertbackground="#ffffff",
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            highlightcolor="#444444",
            highlightbackground="#333333",
        )
        self.round_entry.insert(0, str(self.round_duration_seconds))
        self.round_entry.pack(side=tk.LEFT)
        self.round_entry.bind("<Return>", lambda event: self.start_round())

        tk.Label(
            left_controls,
            text="сек или мм:сс",
            font=("Segoe UI", 8),
            fg="#666666",
            bg="#111111",
        ).pack(side=tk.LEFT, padx=(8, 12))

        tk.Button(
            left_controls,
            text="Старт",
            command=self.start_round,
            bg="#222222",
            fg="#a6e3a1",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            activeforeground="#ffffff",
            activebackground="#333333",
        ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Button(
            left_controls,
            text="Стоп",
            command=self.stop_round,
            bg="#222222",
            fg="#f9e2af",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            activeforeground="#ffffff",
            activebackground="#333333",
        ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Button(
            left_controls,
            text="Сбросить данные",
            command=self.reset_results,
            bg="#222222",
            fg="#f38ba8",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            activeforeground="#ffffff",
            activebackground="#333333",
        ).pack(side=tk.LEFT)

        right_controls = tk.Frame(controls_row, bg="#111111")
        right_controls.pack(side=tk.RIGHT)

        self.round_label = tk.Label(
            right_controls,
            text="Раунд не запущен",
            font=("Consolas", 14, "bold"),
            fg="#666666",
            bg="#111111",
        )
        self.round_label.pack(side=tk.TOP, anchor="e")

        self.accept_label = tk.Label(
            right_controls,
            text="Приём результатов закрыт",
            font=("Segoe UI", 8),
            fg="#666666",
            bg="#111111",
        )
        self.accept_label.pack(side=tk.TOP, anchor="e")

        tk.Frame(self.root, height=1, bg="#222222").pack(fill=tk.X)

        container = tk.Frame(self.root, bg="#0a0a0a")
        container.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(container, bg="#0a0a0a", highlightthickness=0, border=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.grid_frame = tk.Frame(self.canvas, bg="#0a0a0a")
        self.canvas_window = self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")

        self.grid_frame.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        self.grid_frame.bind("<Control-MouseWheel>", self._on_zoom_wheel)

        self.root.bind("<equal>", lambda event: self._zoom_in())
        self.root.bind("<KP_Add>", lambda event: self._zoom_in())
        self.root.bind("<minus>", lambda event: self._zoom_out())
        self.root.bind("<KP_Subtract>", lambda event: self._zoom_out())
        self.root.bind("0", lambda event: self._zoom_reset())
        self.root.bind("<ButtonRelease-1>", self._on_drag_release_global)

        self.agent_widgets: dict[str, tuple] = {}

        self._frame_count = 0
        self._fps_timer = time.time()

    def start_server(self):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.settimeout(1)
        self.server_sock.bind(("0.0.0.0", self.port))
        self.server_sock.listen(50)
        print(f"[Monitor] Сервер на порту {self.port}, обновление GUI каждые {self.refresh_ms} мс")

        def accept_loop():
            while self.running:
                try:
                    conn, addr = self.server_sock.accept()
                    print(f"[Monitor] Подключение: {addr}")
                    handler = AgentConnection(conn, addr, self)
                    with self.lock:
                        self.connections.append(handler)
                    handler.start()
                except socket.timeout:
                    continue
                except OSError:
                    break

        threading.Thread(target=accept_loop, daemon=True).start()

    def _clear_results_locked(self, clear_live_times: bool):
        if clear_live_times:
            self.agent_live_times.clear()
        self.agent_last_results.clear()
        self.round_start_live_times.clear()
        self.best_result_name = ""
        self.best_result_time = ""
        self.best_result_ms = None
        self.dirty.update(self.frames.keys())

    def start_round(self):
        duration = parse_duration_input(self.round_entry.get())
        if duration is None:
            self.round_label.configure(text="Неверная длительность", fg="#f38ba8")
            self.accept_label.configure(text="Используйте секунды или мм:сс", fg="#f38ba8")
            return

        now = time.time()
        with self.lock:
            self._clear_results_locked(clear_live_times=False)
            self.round_start_live_times = {
                name: live_time
                for name, live_time in self.agent_live_times.items()
                if live_time
            }
            self.round_duration_seconds = duration
            self.round_end_at = now + duration
            self.round_active = True

        self.round_label.configure(text=f"Раунд: {format_clock(duration)}", fg="#a6e3a1")
        self.accept_label.configure(text="Приём результатов открыт", fg="#a6e3a1")

    def stop_round(self):
        with self.lock:
            self.round_active = False
            self.round_end_at = 0.0

        self.round_label.configure(text="Раунд остановлен", fg="#f9e2af")
        self.accept_label.configure(text="Приём результатов закрыт", fg="#666666")

    def reset_results(self):
        with self.lock:
            self._clear_results_locked(clear_live_times=True)
            self.round_active = False
            self.round_end_at = 0.0

        self.round_label.configure(text="Данные сброшены", fg="#f38ba8")
        self.accept_label.configure(text="Приём результатов закрыт", fg="#666666")

    def _is_round_open_locked(self) -> bool:
        if not self.round_active:
            return False
        if time.time() >= self.round_end_at:
            self.round_active = False
            self.round_end_at = 0.0
            return False
        return True

    def _register_result_locked(self, name: str, result_time: str):
        if not self._is_round_open_locked():
            return
        if self.round_start_live_times.get(name) == result_time:
            return
        if self.agent_last_results.get(name) == result_time:
            return

        result_ms = parse_time_to_ms(result_time)
        if result_ms is None:
            return

        self.agent_last_results[name] = result_time
        if self.best_result_ms is None or result_ms < self.best_result_ms:
            self.best_result_ms = result_ms
            self.best_result_time = result_time
            self.best_result_name = name

    def update_frame(self, name: str, img_data: bytes, metadata: dict | None = None):
        metadata = metadata or {}
        time_text = str(metadata.get("time_text") or "").strip()
        result_time = str(metadata.get("result_time") or "").strip()

        with self.lock:
            self.frames[name] = img_data
            self.dirty.add(name)
            self.agent_live_times[name] = time_text
            if self.round_start_live_times.get(name) and time_text != self.round_start_live_times[name]:
                self.round_start_live_times.pop(name, None)
            if result_time:
                self._register_result_locked(name, result_time)

    def remove_agent_by_connection(self, handler: AgentConnection):
        with self.lock:
            if handler in self.connections:
                self.connections.remove(handler)

    def _on_canvas_resize(self, event):
        width = event.width
        height = event.height
        if abs(width - getattr(self, "_last_canvas_w", 0)) > 10 or abs(height - getattr(self, "_last_canvas_h", 0)) > 10:
            self._last_canvas_w = width
            self._last_canvas_h = height
            self.canvas.itemconfig(self.canvas_window, width=width)
            with self.lock:
                self.dirty.update(self.frames.keys())

    def _show_rename_menu(self, event, name: str):
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#1a1a1a",
            fg="#cccccc",
            activebackground="#333333",
            activeforeground="#ffffff",
            font=("Segoe UI", 9),
            bd=1,
        )
        menu.add_command(label="Переименовать", command=lambda: self._do_rename(name))
        if name in self.display_names:
            menu.add_command(label="Сбросить имя", command=lambda: self._reset_name(name))
        menu.tk_popup(event.x_root, event.y_root)

    def _do_rename(self, name: str):
        existing = self.display_names.get(name, name)
        dlg = tk.Toplevel(self.root)
        dlg.title("Переименовать")
        dlg.configure(bg="#111111")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("320x110")
        dlg.resizable(False, False)

        x = self.root.winfo_rootx() + self.root.winfo_width() // 2 - 160
        y = self.root.winfo_rooty() + self.root.winfo_height() // 2 - 55
        dlg.geometry(f"320x110+{x}+{y}")

        tk.Label(dlg, text="Новое имя:", fg="#888888", bg="#111111", font=("Segoe UI", 9)).pack(pady=(12, 4))
        entry = tk.Entry(
            dlg,
            font=("Segoe UI", 10),
            bg="#222222",
            fg="#ffffff",
            insertbackground="#ffffff",
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            highlightcolor="#444444",
            highlightbackground="#333333",
        )
        entry.insert(0, existing)
        entry.pack(fill=tk.X, padx=20)
        entry.select_range(0, tk.END)
        entry.focus()

        def apply():
            new_name = entry.get().strip()
            if new_name:
                self.display_names[name] = new_name
            elif name in self.display_names:
                del self.display_names[name]
            dlg.destroy()

        entry.bind("<Return>", lambda event: apply())
        entry.bind("<Escape>", lambda event: dlg.destroy())

    def _reset_name(self, name: str):
        self.display_names.pop(name, None)

    def _on_zoom_wheel(self, event):
        if event.delta > 0:
            self.zoom_factor = min(3.0, self.zoom_factor + 0.25)
        else:
            self.zoom_factor = max(0.5, self.zoom_factor - 0.25)
        self._update_zoom_display()
        with self.lock:
            self.dirty.update(self.frames.keys())
        return "break"

    def _zoom_in(self):
        self.zoom_factor = min(3.0, self.zoom_factor + 0.25)
        self._update_zoom_display()
        with self.lock:
            self.dirty.update(self.frames.keys())

    def _zoom_out(self):
        self.zoom_factor = max(0.5, self.zoom_factor - 0.25)
        self._update_zoom_display()
        with self.lock:
            self.dirty.update(self.frames.keys())

    def _zoom_reset(self):
        self.zoom_factor = 1.0
        self._update_zoom_display()
        with self.lock:
            self.dirty.update(self.frames.keys())

    def _update_zoom_display(self):
        self.zoom_label.configure(text=f"{int(self.zoom_factor * 100)}%")

    def _on_drag_start(self, event, name: str):
        self.drag_source_name = name
        self.drag_start_x = event.x_root
        self.drag_start_y = event.y_root
        self.drag_active = False
        self.root.config(cursor="crosshair")

    def _on_drag_motion(self, event, name: str):
        if not self.drag_source_name:
            return
        dx = event.x_root - self.drag_start_x
        dy = event.y_root - self.drag_start_y
        if not self.drag_active and (abs(dx) > self.drag_threshold or abs(dy) > self.drag_threshold):
            self.drag_active = True
            self._highlight_source()
            return
        if self.drag_active:
            self._highlight_drop_target(event.x_root, event.y_root)

    def _on_drag_release_global(self, event):
        if self.drag_active and self._current_drop_target and self.drag_source_name:
            source = self.drag_source_name
            target = self._current_drop_target
            if source != target:
                self._swap_order(source, target)
        self._clear_drag_state()

    def _highlight_source(self):
        if self.drag_source_name and self.drag_source_name in self.agent_widgets:
            frame, _, _, _ = self.agent_widgets[self.drag_source_name]
            if frame.winfo_exists():
                frame.configure(highlightbackground="#f38ba8", highlightthickness=3, relief=tk.RAISED)
        self.root.config(cursor="fleur")

    def _highlight_drop_target(self, root_x: int, root_y: int):
        target_name = None
        for name, (frame, _, _, _) in self.agent_widgets.items():
            if name == self.drag_source_name:
                continue
            x = frame.winfo_rootx()
            y = frame.winfo_rooty()
            width = frame.winfo_width()
            height = frame.winfo_height()
            if x <= root_x <= x + width and y <= root_y <= y + height:
                target_name = name
                break
        if target_name != self._current_drop_target:
            if self._current_drop_target and self._current_drop_target in self.agent_widgets:
                prev_frame = self.agent_widgets[self._current_drop_target][0]
                if prev_frame.winfo_exists():
                    prev_frame.configure(highlightbackground="#585b70", highlightthickness=1, relief=tk.FLAT)
            self._current_drop_target = target_name
            if target_name and target_name in self.agent_widgets:
                target_frame = self.agent_widgets[target_name][0]
                if target_frame.winfo_exists():
                    target_frame.configure(highlightbackground="#a6e3a1", highlightthickness=3, relief=tk.RAISED)

    def _clear_drag_state(self):
        if self.drag_source_name and self.drag_source_name in self.agent_widgets:
            frame = self.agent_widgets[self.drag_source_name][0]
            if frame.winfo_exists():
                frame.configure(highlightbackground="#585b70", highlightthickness=1, relief=tk.FLAT)
        if self._current_drop_target and self._current_drop_target in self.agent_widgets:
            frame = self.agent_widgets[self._current_drop_target][0]
            if frame.winfo_exists():
                frame.configure(highlightbackground="#585b70", highlightthickness=1, relief=tk.FLAT)
        self.drag_source_name = None
        self.drag_active = False
        self._current_drop_target = None
        self.root.config(cursor="")

    def _swap_order(self, source: str, target: str):
        with self.lock:
            items = list(self.frames.items())
            src_idx = None
            tgt_idx = None
            for index, (name, _) in enumerate(items):
                if name == source:
                    src_idx = index
                elif name == target:
                    tgt_idx = index
            if src_idx is None or tgt_idx is None:
                return
            items[src_idx], items[tgt_idx] = items[tgt_idx], items[src_idx]
            self.frames = OrderedDict(items)
            self.dirty.update(self.frames.keys())

    def _get_cell_size(self) -> tuple[int, int]:
        canvas_width = self.canvas.winfo_width()
        if canvas_width < 100:
            canvas_width = 1400
        padding = 12
        cell_w = (canvas_width - padding * (self.columns + 1)) // self.columns
        cell_w = int(cell_w * self.zoom_factor)
        cell_h = int(cell_w * 9 / 16)
        if cell_w > 1920:
            cell_w = 1920
            cell_h = 1080
        return max(cell_w, 40), max(cell_h, 25)

    def _resize_frame_image(self, img: Image.Image, max_width: int, max_height: int) -> Image.Image:
        if img.width <= 0 or img.height <= 0:
            return img

        scale = min(max_width / img.width, max_height / img.height)
        scale = max(scale, 0.01)
        new_size = (
            max(1, int(round(img.width * scale))),
            max(1, int(round(img.height * scale))),
        )

        if new_size == img.size:
            return img
        return img.resize(new_size, Image.BILINEAR)

    def _create_agent_widget(self, name: str) -> tuple:
        frame = tk.Frame(
            self.grid_frame,
            bg="#161616",
            bd=0,
            relief=tk.FLAT,
            highlightbackground="#2a2a2a",
            highlightthickness=1,
        )
        frame.grid_propagate(False)

        header = tk.Frame(frame, bg="#161616", height=48)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        name_label = tk.Label(
            header,
            text=f" {name}",
            font=("Segoe UI", 9),
            fg="#aaaaaa",
            bg="#161616",
            anchor="w",
        )
        name_label.pack(fill=tk.X, padx=10, pady=(5, 0))

        meta_label = tk.Label(
            header,
            text="Ожидание времени",
            font=("Consolas", 8),
            fg="#666666",
            bg="#161616",
            anchor="w",
        )
        meta_label.pack(fill=tk.X, padx=10, pady=(0, 5))

        img_label = tk.Label(frame, bg="#0a0a0a", anchor="center")
        img_label.pack(fill=tk.BOTH, expand=True)

        img_label.bind("<Button-1>", lambda event, n=name: self._on_drag_start(event, n))
        img_label.bind("<B1-Motion>", lambda event, n=name: self._on_drag_motion(event, n))

        header.bind("<Button-3>", lambda event, n=name: self._show_rename_menu(event, n))
        name_label.bind("<Button-3>", lambda event, n=name: self._show_rename_menu(event, n))
        header.bind("<Double-Button-1>", lambda event, n=name: self._do_rename(n))
        name_label.bind("<Double-Button-1>", lambda event, n=name: self._do_rename(n))

        frame.bind("<Button-1>", lambda event, n=name: self._on_drag_start(event, n))
        frame.bind("<B1-Motion>", lambda event, n=name: self._on_drag_motion(event, n))
        header.bind("<Button-1>", lambda event, n=name: self._on_drag_start(event, n))
        header.bind("<B1-Motion>", lambda event, n=name: self._on_drag_motion(event, n))

        return frame, name_label, meta_label, img_label

    def _build_meta_text(self, name: str, live_times: dict[str, str], accepted_times: dict[str, str]) -> tuple[str, str]:
        live_time = live_times.get(name, "")
        accepted_time = accepted_times.get(name, "")

        if live_time and accepted_time and live_time != accepted_time:
            return f"OCR {live_time} | зачёт {accepted_time}", "#a6e3a1"
        if accepted_time:
            return f"Зачёт: {accepted_time}", "#a6e3a1"
        if live_time:
            return f"OCR: {live_time}", "#f9e2af"
        return "Ожидание времени", "#666666"

    def _refresh_round_state(self):
        with self.lock:
            round_open = self._is_round_open_locked()
            round_end_at = self.round_end_at

        if round_open:
            seconds_left = int(round_end_at - time.time())
            self.round_label.configure(text=f"Раунд: {format_clock(seconds_left)}", fg="#a6e3a1")
            self.accept_label.configure(text="Приём результатов открыт", fg="#a6e3a1")
        else:
            self.accept_label.configure(text="Приём результатов закрыт", fg="#666666")
            current_text = self.round_label.cget("text")
            if current_text.startswith("Раунд:"):
                self.round_label.configure(text="Раунд завершён", fg="#666666")

    def refresh_gui(self):
        if not self.running:
            return

        self._refresh_round_state()

        with self.lock:
            names = list(self.frames.keys())
            dirty_names = set(self.dirty)
            self.dirty.clear()
            dirty_frames = {name: self.frames[name] for name in dirty_names if name in self.frames}
            live_times = dict(self.agent_live_times)
            accepted_times = dict(self.agent_last_results)
            best_name = self.best_result_name
            best_time = self.best_result_time

        cell_w, cell_h = self._get_cell_size()
        current_w, current_h = self._last_cell_size
        size_changed = abs(cell_w - current_w) > 4 or abs(cell_h - current_h) > 4
        self._last_cell_size = (cell_w, cell_h)

        if size_changed:
            with self.lock:
                dirty_frames = dict(self.frames)

        for name in names:
            if name not in self.agent_widgets:
                self.agent_widgets[name] = self._create_agent_widget(name)

        for name in names:
            if name in self.agent_widgets:
                _, name_label, meta_label, _ = self.agent_widgets[name]
                display_name = self.display_names.get(name, name)
                expected = f" {display_name}"
                if name_label.cget("text") != expected:
                    name_label.configure(text=expected)

                meta_text, meta_color = self._build_meta_text(name, live_times, accepted_times)
                if meta_label.cget("text") != meta_text or meta_label.cget("fg") != meta_color:
                    meta_label.configure(text=meta_text, fg=meta_color)

        for index, name in enumerate(names):
            row, col = divmod(index, self.columns)
            frame, _, _, _ = self.agent_widgets[name]
            frame.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            if size_changed:
                frame.configure(width=cell_w, height=cell_h + 48)

        for column in range(self.columns):
            self.grid_frame.columnconfigure(column, weight=1)

        if not self.drag_active:
            for name, img_data in dirty_frames.items():
                if name not in self.agent_widgets:
                    continue
                try:
                    img = Image.open(io.BytesIO(img_data))
                    img = self._resize_frame_image(img, max(cell_w - 8, 1), max(cell_h - 8, 1))
                    photo = ImageTk.PhotoImage(img)
                    self.photo_images[name] = photo
                    _, _, _, img_label = self.agent_widgets[name]
                    img_label.configure(image=photo)
                except Exception:
                    pass

        self._frame_count += 1
        now = time.time()
        elapsed = now - self._fps_timer
        if elapsed >= 1.0:
            real_fps = self._frame_count / elapsed
            self.fps_label.configure(text=f"{real_fps:.0f} FPS")
            self._frame_count = 0
            self._fps_timer = now

        count = len(names)
        status = f"Подключено: {count} ПК" if count else "Ожидание подключений..."
        self.status_label.configure(text=status)

        if best_time and best_name:
            display_name = self.display_names.get(best_name, best_name)
            self.best_label.configure(text=f"Лучшее время: {best_time}  |  {display_name}", fg="#f9e2af")
        else:
            self.best_label.configure(text="Лучшее время: --", fg="#666666")

        self.root.after(self.refresh_ms, self.refresh_gui)

    def on_close(self):
        self.running = False
        try:
            self.server_sock.close()
        except Exception:
            pass
        for handler in self.connections:
            handler.running = False
        self.root.destroy()

    def run(self):
        self.start_server()
        self.root.after(100, self.refresh_gui)
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Screen Monitor Viewer")
    parser.add_argument("--port", type=int, default=9900, help="Порт (по умолчанию 9900)")
    parser.add_argument("--columns", type=int, default=3, help="Столбцов в сетке (по умолчанию 3)")
    parser.add_argument("--fps", type=int, default=30, help="Макс. частота обновления GUI (по умолчанию 30)")
    args = parser.parse_args()

    monitor = ScreenMonitor(port=args.port, columns=args.columns, fps=args.fps)
    monitor.run()


if __name__ == "__main__":
    main()
