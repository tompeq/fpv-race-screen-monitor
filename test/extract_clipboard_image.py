from __future__ import annotations

from pathlib import Path

from PIL import ImageGrab


def main() -> int:
    out_path = Path(__file__).resolve().parent / "input_from_clipboard.png"
    img = ImageGrab.grabclipboard()
    if img is None or not hasattr(img, "save"):
        print("Clipboard image not found.")
        return 1

    img.save(out_path)
    print(f"Saved: {out_path}")
    print(f"Size: {img.size[0]}x{img.size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
