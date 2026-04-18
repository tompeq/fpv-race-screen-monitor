from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agent import TIME_PATTERN, TimerOCR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check top timer OCR region on a saved image.")
    parser.add_argument(
        "--image",
        default=str(Path(__file__).resolve().parent / "input_from_clipboard.png"),
        help="Path to image for OCR check.",
    )
    parser.add_argument("--x", type=int, default=1060)
    parser.add_argument("--y", type=int, default=85)
    parser.add_argument("--w", type=int, default=360)
    parser.add_argument("--h", type=int, default=120)
    parser.add_argument(
        "--expected",
        default="0:27.569",
        help="Expected timer text for this test image.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of repeated OCR runs for stability check.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image_path = Path(args.image).resolve()
    if not image_path.exists():
        print(f"Image not found: {image_path}")
        return 1

    image = Image.open(image_path).convert("RGB")
    ocr = TimerOCR()
    if not ocr.available:
        print("OCR unavailable: install numpy and opencv-python.")
        return 1

    region = (args.x, args.y, args.w, args.h)
    print(f"Image: {image_path}")
    print(f"Size: {image.size[0]}x{image.size[1]}")
    print(f"Region: x={args.x}, y={args.y}, w={args.w}, h={args.h}")
    print(f"Expected: {args.expected}")

    ok_count = 0
    for index in range(1, args.runs + 1):
        text, status = ocr.read_time(image, region)
        valid = bool(text and TIME_PATTERN.match(text))
        exact = text == args.expected
        if valid and exact:
            ok_count += 1
        print(f"Run {index}: text={text!r}, status={status!r}, valid={valid}, exact={exact}")

    if ok_count == args.runs:
        print("PASS: OCR is stable for this region.")
        return 0

    print(f"FAIL: only {ok_count}/{args.runs} runs matched expected text.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
