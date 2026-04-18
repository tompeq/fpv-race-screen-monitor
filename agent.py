"""
Screen Monitor Agent - запускается на каждом компьютере в сети.
Периодически захватывает скриншот, отправляет его на сервер-монитор
и раз в секунду пытается распознать время в заранее заданной области.

Зависимости:
    pip install Pillow mss numpy opencv-python

Использование:
    python agent.py
"""

from __future__ import annotations

import io
import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from typing import Callable

try:
    import mss
except ImportError:
    print("Установите mss: pip install mss")
    sys.exit(1)

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Установите Pillow: pip install Pillow")
    sys.exit(1)


TIME_PATTERN = re.compile(r"^\d:\d{2}\.\d{3}$")
TIME_POSITIONS = [
    list("0123456789"),
    [":"],
    list("0123456789"),
    list("0123456789"),
    ["."],
    list("0123456789"),
    list("0123456789"),
    list("0123456789"),
]


def clamp(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(value, max_value))


class TimerOCR:
    """Простой OCR под формат m:ss.mmm без внешнего движка OCR."""

    def __init__(self):
        self.available = cv2 is not None and np is not None
        self.template_size = (32, 48)
        self.templates: dict[str, list[np.ndarray]] = {}
        if self.available:
            self.templates = self._build_templates()
            self.available = any(self.templates.values())

    def _build_templates(self) -> dict[str, list[np.ndarray]]:
        templates = {char: [] for char in "0123456789:."}

        font_dir = r"C:\Windows\Fonts"
        font_files = [
            "arial.ttf",
            "arialbd.ttf",
            "ariali.ttf",
            "arialbi.ttf",
            "consola.ttf",
            "consolab.ttf",
            "consolai.ttf",
            "segoeui.ttf",
            "segoeuib.ttf",
            "segoeuii.ttf",
            "tahoma.ttf",
            "verdanab.ttf",
            "verdanai.ttf",
        ]
        font_sizes = [56, 64, 72, 84]

        for font_name in font_files:
            font_path = os.path.join(font_dir, font_name)
            if not os.path.exists(font_path):
                continue
            for size in font_sizes:
                try:
                    font = ImageFont.truetype(font_path, size=size)
                except Exception:
                    continue
                for char in templates:
                    img = Image.new("L", (160, 160), 0)
                    draw = ImageDraw.Draw(img)
                    bbox = draw.textbbox((0, 0), char, font=font)
                    width = bbox[2] - bbox[0]
                    height = bbox[3] - bbox[1]
                    x = (160 - width) // 2 - bbox[0]
                    y = (160 - height) // 2 - bbox[1]
                    draw.text((x, y), char, fill=255, font=font)
                    binary = self._threshold(np.array(img))
                    templates[char].append(self._normalize_char(binary))

        hershey_fonts = [
            cv2.FONT_HERSHEY_SIMPLEX,
            cv2.FONT_HERSHEY_DUPLEX,
            cv2.FONT_HERSHEY_TRIPLEX,
            cv2.FONT_HERSHEY_DUPLEX | cv2.FONT_ITALIC,
            cv2.FONT_HERSHEY_TRIPLEX | cv2.FONT_ITALIC,
        ]
        for font_face in hershey_fonts:
            for scale in [1.5, 1.8, 2.2]:
                for thickness in [2, 3, 4]:
                    for char in templates:
                        img = np.zeros((160, 160), dtype=np.uint8)
                        (width, height), _ = cv2.getTextSize(char, font_face, scale, thickness)
                        x = (160 - width) // 2
                        y = (160 + height) // 2
                        cv2.putText(
                            img,
                            char,
                            (x, y),
                            font_face,
                            scale,
                            255,
                            thickness,
                            cv2.LINE_AA,
                        )
                        binary = self._threshold(img)
                        templates[char].append(self._normalize_char(binary))

        return templates

    def _threshold(self, gray: "np.ndarray") -> "np.ndarray":
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return binary

    def _normalize_char(self, binary: "np.ndarray") -> "np.ndarray":
        ys, xs = np.where(binary > 0)
        target_w, target_h = self.template_size
        if len(xs) == 0:
            return np.zeros((target_h, target_w), dtype=np.uint8)

        x1, x2 = xs.min(), xs.max() + 1
        y1, y2 = ys.min(), ys.max() + 1
        cropped = binary[y1:y2, x1:x2]

        inner_w = target_w - 6
        inner_h = target_h - 6
        src_h, src_w = cropped.shape
        scale = min(inner_w / max(src_w, 1), inner_h / max(src_h, 1))
        new_w = max(1, int(round(src_w * scale)))
        new_h = max(1, int(round(src_h * scale)))

        resized = cv2.resize(cropped, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((target_h, target_w), dtype=np.uint8)
        offset_x = (target_w - new_w) // 2
        offset_y = (target_h - new_h) // 2
        canvas[offset_y:offset_y + new_h, offset_x:offset_x + new_w] = resized
        return canvas

    def _trim_binary(self, binary: "np.ndarray") -> "np.ndarray | None":
        ys, xs = np.where(binary > 0)
        if len(xs) == 0:
            return None
        return binary[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    def _column_groups(self, binary: "np.ndarray") -> list[tuple[int, int]]:
        projection = (binary > 0).sum(axis=0)
        threshold = max(1, int(binary.shape[0] * 0.04))

        groups: list[tuple[int, int]] = []
        start = None
        for index, value in enumerate(projection):
            if value > threshold and start is None:
                start = index
            elif value <= threshold and start is not None:
                groups.append((start, index))
                start = None
        if start is not None:
            groups.append((start, len(projection)))
        return groups

    def _row_groups(self, binary: "np.ndarray") -> list[tuple[int, int]]:
        projection = (binary > 0).sum(axis=1)
        threshold = max(1, int(binary.shape[1] * 0.02))

        groups: list[tuple[int, int]] = []
        start = None
        for index, value in enumerate(projection):
            if value > threshold and start is None:
                start = index
            elif value <= threshold and start is not None:
                groups.append((start, index))
                start = None
        if start is not None:
            groups.append((start, len(projection)))
        return groups

    def _group_metrics(self, binary: "np.ndarray", groups: list[tuple[int, int]]) -> list[dict[str, int]]:
        metrics: list[dict[str, int]] = []
        for x1, x2 in groups:
            char_img = binary[:, x1:x2]
            ys, xs = np.where(char_img > 0)
            if len(xs) == 0:
                continue
            metrics.append(
                {
                    "x1": int(x1),
                    "x2": int(x2),
                    "width": int(x2 - x1),
                    "height": int(ys.max() - ys.min() + 1),
                }
            )
        return metrics

    def _iter_row_slices(self, row_binary: "np.ndarray") -> list["np.ndarray"]:
        trimmed = self._trim_binary(row_binary)
        if trimmed is None:
            return []

        slices = [trimmed]
        groups = self._column_groups(trimmed)
        metrics = self._group_metrics(trimmed, groups)
        if not metrics:
            return slices

        max_height = max(metric["height"] for metric in metrics)
        tall_indexes = [index for index, metric in enumerate(metrics) if metric["height"] >= max_height * 0.78]
        if not tall_indexes:
            return slices

        cluster_start = tall_indexes[0]
        cluster_end = tall_indexes[0]
        clusters: list[tuple[int, int]] = []
        for index in tall_indexes[1:]:
            if index - cluster_end <= 2:
                cluster_end = index
            else:
                clusters.append((cluster_start, cluster_end))
                cluster_start = index
                cluster_end = index
        clusters.append((cluster_start, cluster_end))

        for start, end in clusters:
            left_index = max(0, start - 1)
            right_index = min(len(groups) - 1, end + 1)
            x1 = max(0, groups[left_index][0] - 14)
            x2 = min(trimmed.shape[1], groups[right_index][1] + 14)
            focused = self._trim_binary(trimmed[:, x1:x2])
            if focused is None:
                continue
            if any(focused.shape == existing.shape and np.array_equal(focused, existing) for existing in slices):
                continue
            slices.insert(0, focused)

        return slices

    def _find_split_point(
        self,
        binary: "np.ndarray",
        group: tuple[int, int],
        median_width: int,
    ) -> int | None:
        x1, x2 = group
        width = x2 - x1
        min_width = max(6, int(median_width * 0.35))
        if width < max(20, int(median_width * 1.25)) or width < min_width * 2:
            return None

        projection = (binary[:, x1:x2] > 0).sum(axis=0).astype(np.float32)
        if len(projection) < min_width * 2:
            return None

        kernel = np.array([1, 2, 3, 2, 1], dtype=np.float32)
        kernel /= kernel.sum()
        smooth = np.convolve(projection, kernel, mode="same")

        best_pos = None
        best_value = None
        for pos in range(min_width, len(smooth) - min_width):
            valley = float(smooth[max(0, pos - 1):min(len(smooth), pos + 2)].mean())
            if best_value is None or valley < best_value:
                best_pos = pos
                best_value = valley

        if best_pos is None or best_value is None:
            return None
        if best_value > max(4.0, float(smooth.max()) * 0.78):
            return None
        return x1 + best_pos

    def _expand_groups(
        self,
        binary: "np.ndarray",
        groups: list[tuple[int, int]],
        target_count: int = 8,
    ) -> list[tuple[int, int]]:
        refined = [(int(x1), int(x2)) for x1, x2 in groups if x2 > x1]

        while len(refined) < target_count:
            widths = sorted(x2 - x1 for x1, x2 in refined)
            if not widths:
                break
            median_width = widths[len(widths) // 2]

            split_done = False
            for index, group in sorted(
                enumerate(refined),
                key=lambda item: item[1][1] - item[1][0],
                reverse=True,
            ):
                split_at = self._find_split_point(binary, group, median_width)
                if split_at is None:
                    continue
                refined = refined[:index] + [(group[0], split_at), (split_at, group[1])] + refined[index + 1:]
                split_done = True
                break

            if not split_done:
                break

        return refined

    def _evaluate_groups(self, binary: "np.ndarray", groups: list[tuple[int, int]]) -> tuple[str | None, float, float]:
        chars: list[str] = []
        scores: list[float] = []
        heights: list[float] = []

        for index, (x1, x2) in enumerate(groups):
            char_img = binary[:, x1:x2]
            cys, cxs = np.where(char_img > 0)
            if len(cxs) == 0:
                return None, -1.0, 0.0

            heights.append(float(cys.max() - cys.min() + 1))
            char_img = char_img[cys.min():cys.max() + 1, cxs.min():cxs.max() + 1]
            char_img = self._normalize_char(char_img)

            char, score = self._classify_char(char_img, TIME_POSITIONS[index])
            if not char:
                return None, -1.0, 0.0
            chars.append(char)
            scores.append(score)

        text = "".join(chars)
        if not TIME_PATTERN.match(text):
            return None, -1.0, 0.0

        sorted_heights = sorted(heights)
        median_height = sorted_heights[len(sorted_heights) // 2]
        return text, sum(scores) / len(scores), median_height

    def _classify_char(self, char_img: "np.ndarray", allowed_chars: list[str]) -> tuple[str, float]:
        best_char = ""
        best_score = -1.0
        for char in allowed_chars:
            for template in self.templates.get(char, []):
                score = float(cv2.matchTemplate(char_img, template, cv2.TM_CCOEFF_NORMED)[0][0])
                if score > best_score:
                    best_char = char
                    best_score = score
        return best_char, best_score

    def _decode_fast_variant(self, binary: "np.ndarray") -> tuple[str | None, float]:
        """
        Fast path for a narrow timer-only region where symbols are expected to be
        segmented as exactly 8 characters.
        """
        best_text = None
        best_score = -1.0

        work = self._trim_binary(binary)
        if work is None:
            return None, -1.0

        variants = [work]
        kernel = np.ones((1, 2), dtype=np.uint8)

        eroded = self._trim_binary(cv2.erode(work, kernel, iterations=1))
        if eroded is not None:
            variants.append(eroded)

        dilated = self._trim_binary(cv2.dilate(work, kernel, iterations=1))
        if dilated is not None:
            variants.append(dilated)

        for variant in variants:
            groups = self._column_groups(variant)
            if len(groups) != 8:
                continue

            text, score, _ = self._evaluate_groups(variant, groups)
            if text and score > best_score:
                best_text = text
                best_score = score

            if best_score >= 0.94:
                break

        return best_text, best_score

    def _decode_variant(self, binary: "np.ndarray") -> tuple[str | None, float]:
        best_text = None
        best_score = -1.0
        best_rank = -1.0
        work = self._trim_binary(binary)
        if work is None:
            return None, -1.0

        variants = [work]
        for size in [2, 3]:
            kernel = np.ones((1, size), dtype=np.uint8)

            eroded = self._trim_binary(cv2.erode(work, kernel, iterations=1))
            if eroded is not None:
                variants.append(eroded)

            dilated = self._trim_binary(cv2.dilate(work, kernel, iterations=1))
            if dilated is not None:
                variants.append(dilated)

        for variant in variants:
            row_groups = self._row_groups(variant)
            if not row_groups:
                row_groups = [(0, variant.shape[0])]

            for row_index, (y1, y2) in enumerate(row_groups):
                row_img = self._trim_binary(variant[y1:y2, :])
                if row_img is None:
                    continue

                for slice_img in self._iter_row_slices(row_img):
                    groups = self._column_groups(slice_img)
                    if not groups:
                        continue

                    if len(groups) < 8:
                        groups = self._expand_groups(slice_img, groups, 8)

                    if len(groups) < 8:
                        continue

                    candidates = [groups]
                    if len(groups) > 8:
                        candidates = [groups[start:start + 8] for start in range(len(groups) - 7)]

                    for candidate in candidates:
                        text, score, median_height = self._evaluate_groups(slice_img, candidate)
                        if not text:
                            continue

                        rank = score + min(median_height, 160.0) / 500.0 - row_index * 0.01
                        if rank > best_rank or (abs(rank - best_rank) < 1e-6 and score > best_score):
                            best_text = text
                            best_score = score
                            best_rank = rank
                            if best_score >= 0.95:
                                return best_text, best_score

        return best_text, best_score

    def read_time(self, image: Image.Image, region: tuple[int, int, int, int]) -> tuple[str | None, str]:
        if not self.available:
            return None, "OCR недоступен: нужны numpy и opencv-python"

        x, y, width, height = region
        if width <= 0 or height <= 0:
            return None, "Задайте область таймера"

        img_w, img_h = image.size
        x = clamp(x, 0, max(img_w - 1, 0))
        y = clamp(y, 0, max(img_h - 1, 0))
        width = clamp(width, 1, img_w - x)
        height = clamp(height, 1, img_h - y)

        crop = image.crop((x, y, x + width, y + height)).convert("L")
        gray = np.array(crop)
        scale = max(2, int(round(180 / max(gray.shape[0], 1))))
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

        variants: list[np.ndarray] = []
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        _, otsu_inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        adaptive = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            8,
        )
        adaptive_inv = cv2.bitwise_not(adaptive)
        variants.extend([otsu, otsu_inv, adaptive, adaptive_inv])

        best_text = None
        best_score = -1.0
        for variant in variants:
            text, score = self._decode_fast_variant(variant)
            if text and score >= 0.78:
                return text, f"OCR: {score:.2f}"

            text, score = self._decode_variant(variant)
            if text and score > best_score:
                best_text = text
                best_score = score
                if best_score >= 0.86:
                    return best_text, f"OCR: {best_score:.2f}"

        if best_text and best_score >= 0.72:
            return best_text, f"OCR: {best_score:.2f}"
        return None, "Число не найдено"


class ScreenAgent:
    def __init__(
        self,
        server_host: str,
        server_port: int,
        name: str,
        fps: float = 30,
        quality: int = 50,
        max_width: int = 960,
        timer_region: tuple[int, int, int, int] = (0, 0, 0, 0),
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.name = name
        self.name_bytes = name.encode("utf-8")
        self.interval = 1.0 / fps
        self.quality = quality
        self.max_width = max_width
        self.timer_region = timer_region
        self.running = False
        self.ocr_interval = 1.0

        self.timer_ocr = TimerOCR()
        self.time_callback: Callable[[str, str], None] | None = None
        self.current_time_text = ""
        self.current_time_status = "Ожидание OCR"
        self.last_seen_text = ""
        self.stable_count = 0
        self.result_armed = True
        self.no_text_streak = 0
        self.next_ocr_check_at = 0.0
        self.pending_result_time = ""
        self.repeat_result_interval = 2.0
        self.next_repeat_emit_at = 0.0
        self.last_emitted_result_text = ""

        self._ocr_jobs: queue.Queue[Image.Image | None] = queue.Queue(maxsize=1)
        self._ocr_results: queue.Queue[tuple[str | None, str]] = queue.Queue(maxsize=1)
        self._ocr_stop_event = threading.Event()
        self._ocr_thread = threading.Thread(target=self._ocr_worker_loop, daemon=True)
        self._ocr_thread.start()

    def set_time_callback(self, callback: Callable[[str, str], None]):
        self.time_callback = callback

    def _notify_time_update(self):
        if self.time_callback:
            self.time_callback(self.current_time_text, self.current_time_status)

    def _sanitize_timer_region(self, image_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
        x, y, width, height = self.timer_region
        if width <= 0 or height <= 0:
            return None

        img_w, img_h = image_size
        x = clamp(x, 0, max(img_w - 1, 0))
        y = clamp(y, 0, max(img_h - 1, 0))
        width = clamp(width, 1, img_w - x)
        height = clamp(height, 1, img_h - y)
        return x, y, width, height

    def _submit_ocr_job(self, full_img: Image.Image):
        region = self._sanitize_timer_region(full_img.size)
        if region is None:
            return

        x, y, width, height = region
        crop = full_img.crop((x, y, x + width, y + height))

        # Keep only the freshest pending OCR job to avoid backlog.
        if self._ocr_jobs.full():
            try:
                self._ocr_jobs.get_nowait()
            except queue.Empty:
                pass

        try:
            self._ocr_jobs.put_nowait(crop)
        except queue.Full:
            pass

    def _ocr_worker_loop(self):
        while not self._ocr_stop_event.is_set():
            try:
                job = self._ocr_jobs.get(timeout=0.3)
            except queue.Empty:
                continue

            if job is None:
                continue

            try:
                time_text, status = self.timer_ocr.read_time(job, (0, 0, job.width, job.height))
            except Exception:
                time_text, status = None, "OCR error"

            if self._ocr_results.full():
                try:
                    self._ocr_results.get_nowait()
                except queue.Empty:
                    pass

            try:
                self._ocr_results.put_nowait((time_text, status))
            except queue.Full:
                pass

    def _apply_ocr_result(self, time_text: str | None, status: str):
        if time_text:
            self.current_time_text = time_text
            self.current_time_status = status
            self.no_text_streak = 0
            now = time.time()

            if time_text == self.last_seen_text:
                self.stable_count += 1
            else:
                self.last_seen_text = time_text
                self.stable_count = 1

            if self.result_armed and self.stable_count >= 1:
                self.pending_result_time = time_text
                self.result_armed = False
                self.last_emitted_result_text = time_text
                self.next_repeat_emit_at = now + self.repeat_result_interval
                self.current_time_status = f"Результат отправлен: {time_text}"
            elif (
                not self.result_armed
                and time_text == self.last_emitted_result_text
                and now >= self.next_repeat_emit_at
            ):
                self.pending_result_time = time_text
                self.next_repeat_emit_at = now + self.repeat_result_interval
        else:
            self.current_time_text = ""
            self.current_time_status = status
            self.last_seen_text = ""
            self.stable_count = 0
            self.no_text_streak += 1
            if self.no_text_streak >= 2:
                self.result_armed = True
                self.last_emitted_result_text = ""
                self.next_repeat_emit_at = 0.0

        self._notify_time_update()

    def _drain_ocr_results(self):
        latest: tuple[str | None, str] | None = None
        while True:
            try:
                latest = self._ocr_results.get_nowait()
            except queue.Empty:
                break

        if latest is None:
            return

        time_text, status = latest
        self._apply_ocr_result(time_text, status)

    def capture_screen(self, sct) -> tuple[bytes, Image.Image]:
        """Захватывает экран и возвращает JPEG-байты и исходное изображение."""
        monitor = sct.monitors[1]
        screenshot = sct.grab(monitor)
        full_img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

        frame_img = full_img
        if frame_img.width > self.max_width:
            ratio = self.max_width / frame_img.width
            new_size = (self.max_width, int(frame_img.height * ratio))
            frame_img = frame_img.resize(new_size, Image.BILINEAR)

        buf = io.BytesIO()
        frame_img.save(buf, format="JPEG", quality=self.quality, optimize=False, subsampling=2)
        return buf.getvalue(), full_img

    def prepare_metadata(self, full_img: Image.Image) -> dict:
        self._drain_ocr_results()

        now = time.time()
        if now >= self.next_ocr_check_at:
            self.next_ocr_check_at = now + self.ocr_interval
            self._submit_ocr_job(full_img)

        result_time = self.pending_result_time
        self.pending_result_time = ""
        return {"time_text": self.current_time_text, "result_time": result_time}

    def send_frame(self, sock: socket.socket, frame_data: bytes, metadata: dict):
        """
        Protocol:
            [4 bytes] name length
            [N bytes] name (UTF-8)
            [4 bytes] JSON metadata length
            [K bytes] JSON metadata
            [4 bytes] JPEG length
            [M bytes] JPEG data
        """
        meta_bytes = json.dumps(metadata, ensure_ascii=False).encode("utf-8")

        sock.sendall(struct.pack("!I", len(self.name_bytes)))
        sock.sendall(self.name_bytes)
        sock.sendall(struct.pack("!I", len(meta_bytes)))
        sock.sendall(meta_bytes)
        sock.sendall(struct.pack("!I", len(frame_data)))
        sock.sendall(frame_data)

    def stop(self):
        self.running = False
        self._ocr_stop_event.set()
        try:
            self._ocr_jobs.put_nowait(None)
        except queue.Full:
            pass

        if self._ocr_thread.is_alive():
            self._ocr_thread.join(timeout=0.5)

class AgentGUI:
    def __init__(self):
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_config.json")
        self.settings = self._load_config()

        self.agent: ScreenAgent | None = None
        self.agent_thread: threading.Thread | None = None
        self.connected = False

        self.root = tk.Tk()
        self.root.title("Screen Agent")
        self.root.configure(bg="#0a0a0a")
        self.root.geometry("420x580")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        top = tk.Frame(self.root, bg="#111111", height=40)
        top.pack(fill=tk.X)
        top.pack_propagate(False)

        tk.Label(top, text="AGENT", font=("Segoe UI", 11, "bold"), fg="#ffffff", bg="#111111").pack(
            side=tk.LEFT,
            padx=20,
        )

        self.status_label = tk.Label(
            top,
            text="Отключён",
            font=("Segoe UI", 9),
            fg="#666666",
            bg="#111111",
        )
        self.status_label.pack(side=tk.LEFT, padx=12)

        self.connect_btn = tk.Button(
            top,
            text="Подключить",
            font=("Segoe UI", 9),
            width=14,
            command=self.toggle_connect,
            bg="#222222",
            fg="#999999",
            relief=tk.FLAT,
            cursor="hand2",
            activeforeground="#ffffff",
            activebackground="#333333",
        )
        self.connect_btn.pack(side=tk.RIGHT, padx=20)

        tk.Frame(self.root, height=1, bg="#222222").pack(fill=tk.X)

        body = tk.Frame(self.root, bg="#0a0a0a")
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        fields = [
            ("Сервер", "server_host", self.settings.get("server_host", "10.0.201.82")),
            ("Порт", "server_port", str(self.settings.get("server_port", 9900))),
            ("Имя", "name", self.settings.get("name", socket.gethostname())),
            ("FPS", "fps", str(self.settings.get("fps", 30))),
            ("Качество JPEG", "quality", str(self.settings.get("quality", 50))),
            ("Макс. ширина", "max_width", str(self.settings.get("max_width", 960))),
        ]

        self.entries: dict[str, tk.Entry] = {}
        for label_text, key, default_val in fields:
            row = tk.Frame(body, bg="#0a0a0a")
            row.pack(fill=tk.X, pady=(0, 10))
            tk.Label(
                row,
                text=label_text,
                fg="#888888",
                bg="#0a0a0a",
                font=("Segoe UI", 9),
                width=14,
                anchor="w",
            ).pack(side=tk.LEFT)
            entry = tk.Entry(
                row,
                font=("Segoe UI", 10),
                bg="#161616",
                fg="#ffffff",
                insertbackground="#ffffff",
                relief=tk.FLAT,
                bd=0,
                highlightthickness=1,
                highlightcolor="#444444",
                highlightbackground="#333333",
                width=16,
            )
            entry.insert(0, default_val)
            entry.pack(side=tk.RIGHT, fill=tk.X, expand=True)
            self.entries[key] = entry

        tk.Label(
            body,
            text="Область таймера на экране",
            fg="#aaaaaa",
            bg="#0a0a0a",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill=tk.X, pady=(8, 8))

        region_fields = [
            ("X", "timer_x", str(self.settings.get("timer_x", 0))),
            ("Y", "timer_y", str(self.settings.get("timer_y", 0))),
            ("Ширина", "timer_width", str(self.settings.get("timer_width", 0))),
            ("Высота", "timer_height", str(self.settings.get("timer_height", 0))),
        ]

        for label_text, key, default_val in region_fields:
            row = tk.Frame(body, bg="#0a0a0a")
            row.pack(fill=tk.X, pady=(0, 10))
            tk.Label(
                row,
                text=label_text,
                fg="#888888",
                bg="#0a0a0a",
                font=("Segoe UI", 9),
                width=14,
                anchor="w",
            ).pack(side=tk.LEFT)
            entry = tk.Entry(
                row,
                font=("Segoe UI", 10),
                bg="#161616",
                fg="#ffffff",
                insertbackground="#ffffff",
                relief=tk.FLAT,
                bd=0,
                highlightthickness=1,
                highlightcolor="#444444",
                highlightbackground="#333333",
                width=16,
            )
            entry.insert(0, default_val)
            entry.pack(side=tk.RIGHT, fill=tk.X, expand=True)
            self.entries[key] = entry

        info_card = tk.Frame(body, bg="#111111", highlightbackground="#222222", highlightthickness=1)
        info_card.pack(fill=tk.X, pady=(8, 12))

        tk.Label(
            info_card,
            text="Распознанное время",
            fg="#888888",
            bg="#111111",
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=12, pady=(10, 2))

        self.timer_value_label = tk.Label(
            info_card,
            text="--:--.---",
            fg="#f9e2af",
            bg="#111111",
            font=("Consolas", 20, "bold"),
            anchor="w",
        )
        self.timer_value_label.pack(fill=tk.X, padx=12)

        self.timer_status_label = tk.Label(
            info_card,
            text="Настройте область таймера",
            fg="#666666",
            bg="#111111",
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.timer_status_label.pack(fill=tk.X, padx=12, pady=(2, 10))

        bottom = tk.Frame(self.root, bg="#0a0a0a")
        bottom.pack(fill=tk.X, padx=20, pady=(0, 16))

        tk.Button(
            bottom,
            text="Сохранить",
            command=self.save_settings,
            bg="#222222",
            fg="#cccccc",
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            cursor="hand2",
            activeforeground="#ffffff",
            activebackground="#333333",
        ).pack(side=tk.RIGHT, padx=(8, 0))

        self.fps_display = tk.Label(bottom, text="", font=("Segoe UI", 8), fg="#444444", bg="#0a0a0a")
        self.fps_display.pack(side=tk.LEFT)

    def _load_config(self) -> dict:
        defaults = {
            "server_host": "10.0.201.82",
            "server_port": 9900,
            "name": socket.gethostname(),
            "fps": 30,
            "quality": 50,
            "max_width": 960,
            "timer_x": 1060,
            "timer_y": 85,
            "timer_width": 360,
            "timer_height": 120,
        }
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as file:
                    saved = json.load(file)
                defaults.update(saved)
            except Exception:
                pass

        if defaults.get("timer_width", 0) <= 0 or defaults.get("timer_height", 0) <= 0:
            defaults["timer_x"] = 1060
            defaults["timer_y"] = 85
            defaults["timer_width"] = 360
            defaults["timer_height"] = 120

        return defaults

    def _save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as file:
                json.dump(self.settings, file, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _set_timer_display(self, time_text: str, status_text: str):
        shown_time = time_text or "--:--.---"
        self.timer_value_label.configure(text=shown_time)

        color = "#666666"
        if "Результат отправлен" in status_text or status_text.startswith("Result sent:"):
            color = "#a6e3a1"
        elif status_text.startswith("OCR:"):
            color = "#89b4fa"
        elif "не найдено" in status_text:
            color = "#f9e2af"
        elif "недоступен" in status_text or "Задайте" in status_text:
            color = "#f38ba8"

        self.timer_status_label.configure(text=status_text, fg=color)

    def save_settings(self):
        try:
            self.settings["server_host"] = self.entries["server_host"].get().strip() or "10.0.201.82"
            self.settings["server_port"] = int(self.entries["server_port"].get().strip())
            self.settings["name"] = self.entries["name"].get().strip() or socket.gethostname()
            self.settings["fps"] = max(1, int(self.entries["fps"].get().strip()))
            self.settings["quality"] = max(1, min(100, int(self.entries["quality"].get().strip())))
            self.settings["max_width"] = max(320, int(self.entries["max_width"].get().strip()))
            self.settings["timer_x"] = max(0, int(self.entries["timer_x"].get().strip()))
            self.settings["timer_y"] = max(0, int(self.entries["timer_y"].get().strip()))
            self.settings["timer_width"] = max(0, int(self.entries["timer_width"].get().strip()))
            self.settings["timer_height"] = max(0, int(self.entries["timer_height"].get().strip()))
            self._save_config()
            if self.settings["timer_width"] > 0 and self.settings["timer_height"] > 0:
                self._set_timer_display("", "Область сохранена, ждём OCR")
            else:
                self._set_timer_display("", "Задайте область таймера")
        except ValueError:
            self._set_timer_display("", "Проверьте числовые поля")

    def toggle_connect(self):
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        self.save_settings()
        self.agent = ScreenAgent(
            server_host=self.settings["server_host"],
            server_port=self.settings["server_port"],
            name=self.settings["name"],
            fps=self.settings["fps"],
            quality=self.settings["quality"],
            max_width=self.settings["max_width"],
            timer_region=(
                self.settings["timer_x"],
                self.settings["timer_y"],
                self.settings["timer_width"],
                self.settings["timer_height"],
            ),
        )
        self.agent.set_time_callback(lambda time_text, status: self.root.after(0, self._set_timer_display, time_text, status))
        self.agent.running = True

        self.connected = True
        self.connect_btn.configure(text="Отключить", fg="#f38ba8")
        self.status_label.configure(text="Подключение...", fg="#aaaaaa")
        self.agent_thread = threading.Thread(target=self._agent_loop, daemon=True)
        self.agent_thread.start()

    def disconnect(self):
        if self.agent:
            self.agent.stop()
        self.connected = False
        self.connect_btn.configure(text="Подключить", fg="#999999")
        self.status_label.configure(text="Отключён", fg="#666666")
        self.fps_display.configure(text="")

    def _agent_loop(self):
        frame_count = 0
        fps_timer = time.time()

        while self.connected and self.agent and self.agent.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
                sock.settimeout(5)
                sock.connect((self.agent.server_host, self.agent.server_port))
                sock.settimeout(None)

                self.root.after(0, lambda: self.status_label.configure(text="Подключён", fg="#a6e3a1"))

                with mss.mss() as sct:
                    while self.connected and self.agent and self.agent.running:
                        t0 = time.perf_counter()
                        frame, full_img = self.agent.capture_screen(sct)
                        metadata = self.agent.prepare_metadata(full_img)
                        self.agent.send_frame(sock, frame, metadata)

                        elapsed = time.perf_counter() - t0
                        sleep_time = self.agent.interval - elapsed
                        if sleep_time > 0:
                            time.sleep(sleep_time)

                        frame_count += 1
                        now = time.time()
                        if now - fps_timer >= 1.0:
                            real_fps = frame_count / (now - fps_timer)
                            self.root.after(
                                0,
                                lambda fps=real_fps: self.fps_display.configure(text=f"{fps:.0f} FPS"),
                            )
                            frame_count = 0
                            fps_timer = now

            except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError, OSError):
                self.root.after(0, lambda: self.status_label.configure(text="Переподключение...", fg="#f9e2af"))
                time.sleep(3)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass

        self.root.after(0, lambda: self.status_label.configure(text="Отключён", fg="#666666"))

    def on_close(self):
        self.connected = False
        if self.agent:
            self.agent.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    gui = AgentGUI()
    gui.run()


if __name__ == "__main__":
    main()

