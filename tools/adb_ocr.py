#!/usr/bin/env python3
"""
adb_ocr.py — Deterministic Android UI interaction via screenshot + pixel scanning.

KOReader renders everything on a custom canvas (not Android accessibility),
so uiautomator returns only 4 nodes. This tool instead:
1. Takes a screencap
2. Scans for text regions by dark-pixel density
3. Maps text labels to Y positions by scanning row brightness
4. Taps the center of found rows

Usage:
    python3 adb_ocr.py tap "Test connection"
    python3 adb_ocr.py tap "Now Reading"
    python3 adb_ocr.py find "X-Ray"        # print coords without tapping
    python3 adb_ocr.py screenshot           # take screenshot to /tmp/adb_screen.png
"""

import subprocess
import sys
import time
from pathlib import Path

try:
    from PIL import Image
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

SCREEN_PATH = "/tmp/adb_screen.png"


def adb(*args) -> str:
    r = subprocess.run(["adb", *args], capture_output=True, text=True)
    return r.stdout


def screencap(path=SCREEN_PATH) -> "Image":
    adb("shell", "screencap", "-p", "/sdcard/_s.png")
    adb("pull", "/sdcard/_s.png", path)
    return Image.open(path)


def find_text_rows(img, min_dark=15, margin=30) -> list[tuple[int, int, int]]:
    """
    Scan the image vertically. Return list of (y_start, y_end, y_center)
    for rows that contain significant dark pixels (= text lines).
    Groups consecutive rows with dark pixels into single text-row bands.
    """
    arr = np.array(img)[:, margin:-margin, :3]  # crop sides
    # Count very-dark pixels per row
    dark_counts = (arr < 80).all(axis=2).sum(axis=1)

    rows = []
    in_band = False
    band_start = 0

    for y, count in enumerate(dark_counts):
        if count >= min_dark:
            if not in_band:
                in_band = True
                band_start = y
        else:
            if in_band:
                in_band = False
                y_end = y - 1
                y_center = (band_start + y_end) // 2
                rows.append((band_start, y_end, y_center))

    if in_band:
        y_end = len(dark_counts) - 1
        rows.append((band_start, y_end, (band_start + y_end) // 2))

    return rows


def tap(x: int, y: int):
    adb("shell", "input", "tap", str(x), str(y))


def find_and_tap(label: str, x_offset: int = 400, dry_run: bool = False,
                 retries: int = 3) -> tuple[int, int] | None:
    """
    Take a screenshot, find the row most likely containing `label`,
    tap its center at x=x_offset.

    Since KOReader's text isn't accessible via AX, we use OCR via
    tesseract if available, or fall back to row ordering.
    """
    try:
        import pytesseract
        has_tess = True
    except ImportError:
        has_tess = False

    for attempt in range(retries):
        img = screencap()
        w, h = img.size

        if has_tess:
            # Full OCR - find text bounding boxes
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            for i, text in enumerate(data["text"]):
                if label.lower() in (text or "").lower():
                    x = data["left"][i] + data["width"][i] // 2
                    y = data["top"][i] + data["height"][i] // 2
                    print(f"OCR found '{label}' at ({x}, {y})")
                    if not dry_run:
                        tap(x, y)
                    return x, y
        else:
            # Pixel-scan fallback: return text row coordinates
            rows = find_text_rows(img)
            print(f"Found {len(rows)} text rows in screenshot")
            for i, (y_start, y_end, y_center) in enumerate(rows[:20]):
                print(f"  Row {i+1}: y={y_start}-{y_end} center={y_center}")
            print("(No tesseract — cannot match by text. Use row index.)")
            return None

        if attempt < retries - 1:
            time.sleep(0.5)

    print(f"Could not find '{label}' on screen")
    return None


def tap_row(row_index: int, x: int = 400):
    """Tap a menu row by index (1-based) in the current screenshot."""
    img = screencap()
    rows = find_text_rows(img)
    if row_index < 1 or row_index > len(rows):
        print(f"Row {row_index} not found (have {len(rows)} rows)")
        return
    _, _, y_center = rows[row_index - 1]
    print(f"Tapping row {row_index} at ({x}, {y_center})")
    tap(x, y_center)


def list_rows():
    """List all visible text rows and their physical y centers."""
    img = screencap()
    rows = find_text_rows(img)
    print(f"{'Row':>4}  {'y_center':>8}  {'y_range'}")
    print("-" * 35)
    for i, (y_start, y_end, y_center) in enumerate(rows, 1):
        print(f"{i:>4}  {y_center:>8}  {y_start}-{y_end}")
    return rows


if __name__ == "__main__":
    if not HAS_NUMPY:
        print("pip3 install Pillow numpy")
        sys.exit(1)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""

    if cmd == "screenshot":
        img = screencap()
        print(f"Screenshot: {img.size} → {SCREEN_PATH}")
    elif cmd == "list":
        list_rows()
    elif cmd == "tap-row":
        tap_row(int(arg))
    elif cmd == "find":
        find_and_tap(arg, dry_run=True)
    elif cmd == "tap":
        find_and_tap(arg)
    else:
        print(__doc__)
