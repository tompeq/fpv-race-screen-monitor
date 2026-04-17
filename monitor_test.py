"""
Тестовая версия монитора на базе актуального monitor.py.

Вместо сети она симулирует агентов, которые:
1. отправляют синтетические кадры,
2. периодически показывают OCR-время в формате m:ss.mmm,
3. иногда завершают круг и отправляют result_time.

Использование:
    python monitor_test.py --columns 3 --fps 30 --agents 12
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from monitor import ScreenMonitor


DEFAULT_AGENT_COUNT = 12
FRAME_SIZE = (1920, 1080)
UI = {
    "root": "#05080c",
    "top": "#0b1016",
    "panel": "#0d141c",
    "card": "#0a1016",
    "card_header": "#101923",
    "card_media": "#030608",
    "line": "#17212d",
    "text": "#eef4fb",
    "muted": "#7f90a3",
    "soft": "#cbd7e3",
    "cyan": "#6be8ff",
    "green": "#67f0b1",
    "amber": "#ffc857",
    "red": "#ff7a7a",
}


def build_agent_names(count: int) -> list[str]:
    return [f"DRONE-{index:02d}" for index in range(1, count + 1)]


def format_time_ms(value_ms: int) -> str:
    minutes = value_ms // 60_000
    seconds = (value_ms % 60_000) // 1_000
    millis = value_ms % 1_000
    return f"{minutes}:{seconds:02d}.{millis:03d}"


def load_font(size: int, *, bold: bool = False, mono: bool = False, condensed: bool = False) -> ImageFont.ImageFont:
    font_dir = r"C:\Windows\Fonts"
    candidates: list[str] = []
    if mono and bold:
        candidates.extend(["consolab.ttf", "lucon.ttf"])
    elif mono:
        candidates.extend(["consola.ttf", "lucon.ttf"])
    elif condensed and bold:
        candidates.extend(["bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf"])
    elif condensed:
        candidates.extend(["bahnschrift.ttf", "segoeui.ttf", "arial.ttf"])
    elif bold:
        candidates.extend(["segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf"])
    else:
        candidates.extend(["segoeui.ttf", "arial.ttf", "tahoma.ttf"])

    for candidate in candidates:
        path = os.path.join(font_dir, candidate)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def mix_colors(color_a: tuple[int, int, int], color_b: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    factor = max(0.0, min(1.0, factor))
    return tuple(int(round(a * (1.0 - factor) + b * factor)) for a, b in zip(color_a, color_b))


def rgb_to_hex(color: tuple[int, int, int]) -> str:
    return f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"


@dataclass
class SimulatedAgentState:
    name: str
    accent: tuple[int, int, int]
    next_lap_at: float
    lap_index: int = 0
    lap_start_at: float = 0.0
    lap_duration: float = 0.0
    target_time_ms: int = 0
    hold_until: float = 0.0
    current_time_text: str = ""
    last_result_text: str = ""
    pending_result_text: str = ""


class TestScreenMonitor(ScreenMonitor):
    def __init__(self, columns: int = 3, fps: int = 30, agents: int = DEFAULT_AGENT_COUNT):
        self.simulated_names = build_agent_names(max(1, agents))
        self.simulation_states: dict[str, SimulatedAgentState] = {}
        self.agent_headers: dict[str, tk.Frame] = {}
        self._simulation_cursor = 0

        super().__init__(port=0, columns=columns, fps=fps)

        self.root.title("FPV Race Control (TEST)")
        self.root.geometry("1500x920")
        self.root.minsize(1100, 760)
        self._capture_static_widgets()
        self._apply_static_theme()

    def _capture_static_widgets(self):
        root_children = self.root.winfo_children()
        self.top_frame = root_children[0]
        self.separator_frame = root_children[1]
        self.container_frame = root_children[2]

        top_children = self.top_frame.winfo_children()
        self.top_row = top_children[0]
        self.controls_row = top_children[1]

        top_row_children = self.top_row.winfo_children()
        self.left_bar = top_row_children[0]
        self.right_bar = top_row_children[2]

        self.title_label = self.left_bar.winfo_children()[0]

        right_bar_children = self.right_bar.winfo_children()
        self.zoom_out_button = right_bar_children[2]
        self.zoom_in_button = right_bar_children[4]

        controls_children = self.controls_row.winfo_children()
        self.left_controls = controls_children[0]
        self.right_controls = controls_children[1]

        left_controls_children = self.left_controls.winfo_children()
        self.round_caption_label = left_controls_children[0]
        self.duration_hint_label = left_controls_children[2]
        self.start_button = left_controls_children[3]
        self.stop_button = left_controls_children[4]
        self.reset_button = left_controls_children[5]

    def _apply_static_theme(self):
        self.root.configure(bg=UI["root"])
        self.top_frame.configure(bg=UI["top"])
        self.top_row.configure(bg=UI["top"])
        self.controls_row.configure(bg=UI["top"])
        self.left_bar.configure(bg=UI["top"])
        self.right_bar.configure(bg=UI["top"])
        self.left_controls.configure(bg=UI["top"])
        self.right_controls.configure(bg=UI["top"])
        self.separator_frame.configure(bg=UI["line"])
        self.container_frame.configure(bg=UI["root"])
        self.canvas.configure(bg=UI["root"])
        self.grid_frame.configure(bg=UI["root"])

        self.title_label.configure(
            text="FPV RACE CONTROL",
            bg=UI["top"],
            fg=UI["text"],
            font=("Bahnschrift SemiCondensed", 15, "bold"),
        )
        self.status_label.configure(bg=UI["top"], fg=UI["cyan"], font=("Bahnschrift SemiCondensed", 10, "bold"))
        self.best_label.configure(bg=UI["top"], fg=UI["amber"], font=("Bahnschrift SemiCondensed", 13, "bold"))
        self.fps_label.configure(bg=UI["top"], fg=UI["muted"], font=("Consolas", 9))
        self.zoom_label.configure(bg=UI["top"], fg=UI["soft"], font=("Consolas", 10, "bold"))

        self.round_caption_label.configure(bg=UI["top"], fg=UI["soft"], font=("Bahnschrift SemiCondensed", 10, "bold"))
        self.duration_hint_label.configure(bg=UI["top"], fg=UI["muted"], font=("Segoe UI", 8))
        self.round_entry.configure(
            bg="#09131a",
            fg=UI["text"],
            insertbackground=UI["cyan"],
            highlightbackground="#16303c",
            highlightcolor=UI["cyan"],
            font=("Consolas", 12, "bold"),
            justify="center",
            relief=tk.FLAT,
        )
        self.round_label.configure(bg=UI["top"], fg=UI["muted"], font=("Bahnschrift SemiCondensed", 15, "bold"))
        self.accept_label.configure(bg=UI["top"], fg=UI["muted"], font=("Segoe UI", 8))

        self._style_button(self.start_button, "#0f241c", "#87f7c1", "#153326")
        self._style_button(self.stop_button, "#2a2110", "#ffd17d", "#3a2d15")
        self._style_button(self.reset_button, "#2b1518", "#ff9a9a", "#3a1a1d")
        self._style_button(self.zoom_in_button, "#121a23", UI["soft"], "#182430")
        self._style_button(self.zoom_out_button, "#121a23", UI["soft"], "#182430")

    def _style_button(self, button: tk.Button, bg: str, fg: str, active_bg: str):
        button.configure(
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=UI["text"],
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            highlightbackground="#1a2833",
            cursor="hand2",
            font=("Bahnschrift SemiCondensed", 10, "bold"),
            padx=10,
            pady=4,
        )

    def start_server(self):
        now = time.time()
        for index, name in enumerate(self.simulated_names):
            state = SimulatedAgentState(
                name=name,
                accent=self._accent_for_index(index),
                next_lap_at=now + 0.45 * index,
            )
            self.simulation_states[name] = state
            frame = self._render_frame(state)
            self.update_frame(name, frame, {"time_text": "", "result_time": ""})

        threading.Thread(target=self._simulation_loop, daemon=True).start()

    def _simulation_loop(self):
        while self.running:
            if not self.simulated_names:
                time.sleep(0.1)
                continue

            name = self.simulated_names[self._simulation_cursor % len(self.simulated_names)]
            self._simulation_cursor += 1

            state = self.simulation_states[name]
            metadata = self._step_state(state)
            frame = self._render_frame(state)
            self.update_frame(name, frame, metadata)

            time.sleep(1.0 / max(len(self.simulated_names) * 2, 1))

    def _step_state(self, state: SimulatedAgentState) -> dict:
        now = time.time()

        if state.lap_duration <= 0 and now >= state.next_lap_at:
            state.lap_index += 1
            state.lap_start_at = now
            state.lap_duration = self._lap_duration_seconds(state)
            state.target_time_ms = self._target_time_ms(state)
            state.current_time_text = format_time_ms(0)
            state.pending_result_text = ""

        if state.lap_duration > 0:
            progress = min(1.0, (now - state.lap_start_at) / state.lap_duration)
            elapsed_ms = int(round(state.target_time_ms * progress))
            state.current_time_text = format_time_ms(elapsed_ms)

            if progress >= 1.0:
                state.current_time_text = format_time_ms(state.target_time_ms)
                state.last_result_text = state.current_time_text
                state.pending_result_text = state.current_time_text
                state.hold_until = now + 1.3
                state.lap_duration = 0.0
                state.next_lap_at = now + 2.2 + ((state.lap_index + len(state.name)) % 4) * 0.45

        elif state.current_time_text and now >= state.hold_until:
            state.current_time_text = ""

        metadata = {
            "time_text": state.current_time_text,
            "result_time": state.pending_result_text,
        }
        state.pending_result_text = ""
        return metadata

    def _target_time_ms(self, state: SimulatedAgentState) -> int:
        name_index = self.simulated_names.index(state.name)
        seed = state.lap_index * 1319 + name_index * 983
        return 21_500 + (seed % 26_000)

    def _lap_duration_seconds(self, state: SimulatedAgentState) -> float:
        name_index = self.simulated_names.index(state.name)
        return 3.0 + ((state.lap_index + name_index) % 7) * 0.32

    def _accent_for_index(self, index: int) -> tuple[int, int, int]:
        palette = [
            (0, 209, 255),
            (255, 200, 87),
            (114, 245, 175),
            (255, 122, 122),
            (111, 118, 255),
            (255, 132, 230),
            (255, 153, 92),
            (99, 230, 190),
            (123, 179, 255),
            (180, 146, 255),
            (255, 210, 117),
            (88, 227, 255),
        ]
        return palette[index % len(palette)]

    def _render_frame(self, state: SimulatedAgentState) -> bytes:
        width, height = FRAME_SIZE
        accent = state.accent
        top_tone = mix_colors((8, 12, 18), accent, 0.12)
        bottom_tone = mix_colors((4, 6, 10), accent, 0.06)

        img = Image.new("RGB", (width, height), top_tone)
        draw = ImageDraw.Draw(img)

        for y in range(0, height, 6):
            blend = y / max(height - 1, 1)
            row_color = mix_colors(top_tone, bottom_tone, blend)
            draw.rectangle((0, y, width, min(y + 6, height)), fill=row_color)

        grid_color = mix_colors((18, 26, 34), accent, 0.10)
        for y in range(150, height, 120):
            draw.line((0, y, width, y), fill=grid_color, width=1)
        for x in range(180, width, 180):
            draw.line((x, 110, x, height), fill=grid_color, width=1)

        top_bar_h = 110
        draw.rectangle((0, 0, width, top_bar_h), fill=(8, 11, 16))
        draw.rectangle((0, top_bar_h - 4, width, top_bar_h), fill=accent)
        draw.rectangle((0, 0, 10, height), fill=accent)
        draw.rectangle((width - 10, 0, width, height), fill=accent)

        title_font = load_font(40, bold=True, condensed=True)
        body_font = load_font(24, condensed=True)
        mono_font = load_font(96, bold=True, mono=True)
        small_mono_font = load_font(28, mono=True)
        small_font = load_font(18, condensed=True)

        draw.text((34, 18), state.name, font=title_font, fill=(245, 248, 252))
        draw.text((36, 70), "SIMULATED 1920 x 1080 FEED", font=body_font, fill=(127, 145, 164))

        frame_box = (120, 170, width - 120, height - 120)
        draw.rounded_rectangle(
            frame_box,
            radius=32,
            outline=mix_colors(accent, (255, 255, 255), 0.15),
            width=2,
        )
        draw.rectangle((150, 200, width - 150, height - 150), outline=mix_colors((45, 58, 70), accent, 0.20), width=1)

        timer_card = (120, height - 330, 930, height - 150)
        draw.rounded_rectangle(
            timer_card,
            radius=26,
            fill=(7, 10, 14),
            outline=mix_colors(accent, (255, 255, 255), 0.15),
            width=2,
        )

        time_text = state.current_time_text or "--:--.---"
        time_color = (245, 248, 252) if state.current_time_text else (111, 126, 141)
        draw.text((160, height - 300), time_text, font=mono_font, fill=time_color)

        chip_y = height - 356
        self._draw_chip(draw, 160, chip_y, "LIVE" if state.current_time_text else "READY", accent if state.current_time_text else (67, 82, 96))
        self._draw_chip(draw, 292, chip_y, f"LAP {state.lap_index:02d}", (70, 96, 118))

        lap_status = "Круг активен" if state.current_time_text else "Ожидание следующего пролёта"
        draw.text((160, height - 182), lap_status, font=body_font, fill=(214, 223, 232))
        draw.text((840, height - 84), f"LAST RESULT  {state.last_result_text or '--:--.---'}", font=small_mono_font, fill=(255, 216, 119))
        draw.text((width - 340, height - 84), "SAFE FRAME 16:9", font=small_font, fill=(133, 149, 164))

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=80)
        return buffer.getvalue()

    def _draw_chip(self, draw: ImageDraw.ImageDraw, x: int, y: int, text: str, color: tuple[int, int, int]):
        chip_font = load_font(18, bold=True, condensed=True)
        bbox = draw.textbbox((0, 0), text, font=chip_font)
        width = bbox[2] - bbox[0] + 26
        height = 28
        draw.rounded_rectangle((x, y, x + width, y + height), radius=14, fill=mix_colors(color, (10, 12, 16), 0.55))
        draw.text((x + 13, y + 4), text, font=chip_font, fill=(248, 250, 252))

    def _create_agent_widget(self, name: str) -> tuple:
        state = self.simulation_states.get(name)
        accent = state.accent if state else (90, 170, 255)
        accent_hex = rgb_to_hex(accent)

        frame = tk.Frame(
            self.grid_frame,
            bg=UI["card"],
            bd=0,
            relief=tk.FLAT,
            highlightbackground="#1a2632",
            highlightthickness=1,
        )

        accent_bar = tk.Frame(frame, bg=accent_hex, height=4)
        accent_bar.pack(fill=tk.X)

        header = tk.Frame(frame, bg=UI["card_header"], height=58)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        name_label = tk.Label(
            header,
            text=f" {name}",
            font=("Bahnschrift SemiCondensed", 11, "bold"),
            fg=UI["text"],
            bg=UI["card_header"],
            anchor="w",
        )
        name_label.pack(fill=tk.X, padx=12, pady=(8, 0))

        meta_label = tk.Label(
            header,
            text="Готов к следующему пролёту",
            font=("Segoe UI", 8),
            fg=UI["muted"],
            bg=UI["card_header"],
            anchor="w",
        )
        meta_label.pack(fill=tk.X, padx=12, pady=(1, 8))

        img_label = tk.Label(frame, bg=UI["card_media"], anchor="center")
        img_label.pack(fill=tk.BOTH, expand=True)

        for widget in (img_label, frame, header):
            widget.bind("<Button-1>", lambda event, n=name: self._on_drag_start(event, n))
            widget.bind("<B1-Motion>", lambda event, n=name: self._on_drag_motion(event, n))

        for widget in (header, name_label):
            widget.bind("<Button-3>", lambda event, n=name: self._show_rename_menu(event, n))
            widget.bind("<Double-Button-1>", lambda event, n=name: self._do_rename(n))

        self.agent_headers[name] = header
        return frame, name_label, meta_label, img_label

    def _build_meta_text(self, name: str, live_times: dict[str, str], accepted_times: dict[str, str]) -> tuple[str, str]:
        live_time = live_times.get(name, "")
        accepted_time = accepted_times.get(name, "")

        if live_time and accepted_time and live_time != accepted_time:
            return f"LIVE {live_time}  |  зачёт {accepted_time}", UI["green"]
        if accepted_time:
            return f"Зачётный круг: {accepted_time}", UI["amber"]
        if live_time:
            return f"Таймер в кадре: {live_time}", UI["cyan"]
        return "Готов к следующему пролёту", UI["muted"]

    def _refresh_header_copy(self):
        count = len(self.frames)
        if self.round_active and time.time() < self.round_end_at:
            self.status_label.configure(text=f"HEAT ACTIVE  |  {count} пилотов", fg=UI["cyan"])
        else:
            self.status_label.configure(text=f"TEST GRID  |  {count} пилотов", fg=UI["soft"])

        if self.best_result_time and self.best_result_name:
            display_name = self.display_names.get(self.best_result_name, self.best_result_name)
            self.best_label.configure(
                text=f"ЛУЧШИЙ КРУГ  {self.best_result_time}  //  {display_name}",
                fg=UI["amber"],
            )
        else:
            self.best_label.configure(text="ЛУЧШИЙ КРУГ  //  ожидание результата", fg=UI["muted"])

    def _refresh_agent_card_styles(self):
        if self.drag_active:
            return

        for name, (frame, name_label, meta_label, _) in self.agent_widgets.items():
            state = self.simulation_states.get(name)
            accent = state.accent if state else (90, 170, 255)
            accent_hex = rgb_to_hex(accent)
            header = self.agent_headers.get(name)

            live = bool(self.agent_live_times.get(name))
            accepted = bool(self.agent_last_results.get(name))

            if live:
                border = accent_hex
                header_bg = rgb_to_hex(mix_colors((16, 25, 35), accent, 0.18))
            elif accepted:
                border = UI["amber"]
                header_bg = "#17140f"
            else:
                border = "#1a2632"
                header_bg = UI["card_header"]

            frame.configure(bg=UI["card"], highlightbackground=border)
            if header is not None:
                header.configure(bg=header_bg)
            name_label.configure(bg=header_bg, fg=UI["text"])
            meta_label.configure(bg=header_bg)

    def refresh_gui(self):
        if not self.running:
            return

        super().refresh_gui()
        self._refresh_header_copy()
        self._refresh_agent_card_styles()


def main():
    parser = argparse.ArgumentParser(description="Screen Monitor Viewer (TEST)")
    parser.add_argument("--columns", type=int, default=3, help="Столбцов в сетке (по умолчанию 3)")
    parser.add_argument("--fps", type=int, default=30, help="Макс. частота обновления GUI (по умолчанию 30)")
    parser.add_argument("--agents", type=int, default=DEFAULT_AGENT_COUNT, help="Количество симулируемых ПК")
    args = parser.parse_args()

    monitor = TestScreenMonitor(columns=args.columns, fps=args.fps, agents=args.agents)
    monitor.run()


if __name__ == "__main__":
    main()
