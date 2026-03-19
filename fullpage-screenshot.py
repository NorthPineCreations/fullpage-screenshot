#!/usr/bin/env python3
"""Full-page screenshot via keyboard scrolling + screen capture.

1. Detect the frontmost app
2. Scroll to top (Cmd+Up)
3. Capture the window, press Page Down, repeat until nothing changes
4. Stitch captures together (removing overlapping regions)
5. Save to ~/Desktop/fullpage_YYYY-MM-DD_HH-MM-SS.png
"""

import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image


MAX_PAGES = 80
SETTLE_TIME = 0.4  # seconds to wait after each scroll


def applescript(script):
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    return r.stdout.strip()


def notify(title, msg):
    applescript(f'display notification "{msg}" with title "{title}"')


def get_frontmost_app():
    return applescript(
        'tell application "System Events" to get name of first application '
        'process whose frontmost is true'
    )


def activate(app):
    applescript(f'tell application "{app}" to activate')
    time.sleep(0.5)


def get_window_bounds(app):
    """Get window position and size via System Events. Returns (x, y, w, h)."""
    pos = applescript(
        f'tell application "System Events" to tell process "{app}" '
        'to get position of front window'
    )
    size = applescript(
        f'tell application "System Events" to tell process "{app}" '
        'to get size of front window'
    )
    x, y = [int(v.strip()) for v in pos.split(',')]
    w, h = [int(v.strip()) for v in size.split(',')]
    return x, y, w, h


def capture_window(app, path):
    """Capture the frontmost window of app to a file using screencapture -l."""
    # Get window ID
    wid = applescript(
        f'tell application "System Events" to tell process "{app}" '
        'to get id of front window'
    )
    if wid:
        subprocess.run(['screencapture', '-l', wid, '-x', '-o', path],
                       capture_output=True)
        if Path(path).exists() and Path(path).stat().st_size > 0:
            return
    # Fallback: capture by region
    x, y, w, h = get_window_bounds(app)
    subprocess.run(['screencapture', '-R', f'{x},{y},{w},{h}', '-x', path],
                   check=True)


def key_press(code, modifiers=None):
    """Press a key by key code, optionally with modifiers."""
    if modifiers:
        mod_str = ', '.join(f'{m} down' for m in modifiers)
        applescript(
            f'tell application "System Events" to key code {code} using {{{mod_str}}}'
        )
    else:
        applescript(f'tell application "System Events" to key code {code}')


def scroll_to_top():
    """Cmd+Up Arrow to go to the very top."""
    # key code 126 = Up Arrow, command = Cmd
    key_press(126, ['command'])
    time.sleep(0.3)
    key_press(126, ['command'])
    time.sleep(SETTLE_TIME)


def page_down():
    """Press Page Down (key code 121)."""
    key_press(121)
    time.sleep(SETTLE_TIME)


def images_same(a, b, threshold=0.97):
    """Check if two images are nearly identical."""
    if a.size != b.size:
        return False
    arr_a = np.array(a, dtype=np.int16)
    arr_b = np.array(b, dtype=np.int16)
    close = np.all(np.abs(arr_a - arr_b) <= 3, axis=-1)
    return close.mean() >= threshold


def find_overlap(prev, curr):
    """Find how many pixels at the bottom of prev match the top of curr.

    Slides a horizontal band from the top of curr down through prev,
    looking for the best match using normalized cross-correlation.
    Returns overlap in pixels, or 0 if no good match found.
    """
    # Work on smaller grayscale versions for speed
    ds = 4
    prev_g = np.array(prev.resize(
        (max(1, prev.width // ds), max(1, prev.height // ds)), Image.LANCZOS
    ).convert('L'), dtype=np.float32)
    curr_g = np.array(curr.resize(
        (max(1, curr.width // ds), max(1, curr.height // ds)), Image.LANCZOS
    ).convert('L'), dtype=np.float32)

    h_p, w = prev_g.shape
    h_c = curr_g.shape[0]
    if h_p < 10 or h_c < 10:
        return 0

    # Use a band from the top of curr as the search template
    band_h = min(20, h_c // 5)
    band = curr_g[:band_h]
    band_flat = band.flatten().astype(np.float64)
    band_flat -= band_flat.mean()
    std = band_flat.std()
    if std < 1:
        return 0
    band_flat /= std

    best_ncc = -1.0
    best_pos = -1
    # Only search the bottom half of prev (overlap can't be more than that)
    start = max(0, h_p // 2)
    for pos in range(start, h_p - band_h + 1):
        region = prev_g[pos:pos + band_h].flatten().astype(np.float64)
        region -= region.mean()
        rs = region.std()
        if rs < 1:
            continue
        region /= rs
        ncc = float(np.mean(band_flat * region))
        if ncc > best_ncc:
            best_ncc = ncc
            best_pos = pos

    if best_ncc < 0.7 or best_pos < 0:
        return 0

    overlap = h_p - best_pos
    if overlap < 3 or overlap > h_c:
        return 0

    return overlap * ds


def stitch(images):
    """Stitch a list of overlapping images into one tall image."""
    if len(images) == 1:
        return images[0]

    strips = [images[0]]
    total_h = images[0].height

    for i in range(1, len(images)):
        overlap = find_overlap(images[i - 1], images[i])
        if overlap > 0 and overlap < images[i].height:
            cropped = images[i].crop((0, overlap, images[i].width, images[i].height))
        else:
            cropped = images[i]
        strips.append(cropped)
        total_h += cropped.height

    result = Image.new('RGB', (images[0].width, total_h))
    y = 0
    for s in strips:
        result.paste(s, (0, y))
        y += s.height

    return result


def main():
    app = get_frontmost_app()
    if not app:
        notify('Screenshot Failed', 'No frontmost app')
        sys.exit(1)

    notify('Screenshot', f'Capturing {app}...')
    activate(app)

    tmpdir = tempfile.mkdtemp()

    # Scroll to top
    scroll_to_top()

    # Capture first frame
    f0 = f'{tmpdir}/shot_000.png'
    capture_window(app, f0)
    shots = [f0]
    prev = Image.open(f0)

    # Scroll and capture
    for i in range(1, MAX_PAGES):
        page_down()

        fname = f'{tmpdir}/shot_{i:03d}.png'
        capture_window(app, fname)
        curr = Image.open(fname)

        if images_same(prev, curr):
            Path(fname).unlink()
            break

        shots.append(fname)
        prev = curr

    # Scroll back to top
    scroll_to_top()

    # Stitch
    images = [Image.open(f) for f in shots]
    result = stitch(images)

    # Save
    ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    output = Path.home() / 'Desktop' / f'fullpage_{ts}.png'
    result.save(str(output), optimize=True)

    # Cleanup
    import shutil
    shutil.rmtree(tmpdir)

    notify('Screenshot Saved', output.name)
    print(f"Saved: {output}", file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        err = traceback.format_exc()
        notify('Screenshot Failed', err.splitlines()[-1][:80])
        Path(Path.home() / 'Desktop' / 'fullpage_error.txt').write_text(err)
        sys.exit(1)
