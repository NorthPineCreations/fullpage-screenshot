#!/usr/bin/env python3
"""Full-page screenshot of the scrollable pane under the mouse pointer.

App-agnostic (Mail, Safari, Chrome, Preview, ...):
1. Find the scrollable pane under the mouse via the Accessibility API
2. Scroll it with synthetic scroll-wheel events (routed by pointer position,
   never by keyboard focus, so the wrong pane can't hijack the capture)
3. Capture only that pane's content region — window chrome/toolbars are
   never part of the frames
4. Stitch frames, deduplicating sticky in-content headers
5. Save to ~/Desktop/fullpage_YYYY-MM-DD_HH-MM-SS.png

Cancel any time:
- press the trigger shortcut again (a second launch stops the running one)
- press Esc
- move the mouse out of the pane being captured
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

import Quartz
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementCopyElementAtPosition,
    AXUIElementCopyAttributeValue,
    AXUIElementSetAttributeValue,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)

LOG_PATH = Path(__file__).resolve().parent / 'debug.log'
logging.basicConfig(
    filename=str(LOG_PATH),
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logging.getLogger('PIL').setLevel(logging.INFO)
log = logging.getLogger('fullpage')

PIDFILE = Path(tempfile.gettempdir()) / 'fullpage-screenshot.pid'
MAX_PAGES = 120
MAX_DURATION = 180          # hard watchdog, seconds
SETTLE_TIME = 0.35          # seconds to wait after each scroll
LAZY_LOAD_WAIT = 1.5        # extra wait when a frame shows no progress
LAZY_RETRIES = 3            # consecutive no-progress frames before stopping
OVERLAP_POINTS = 120        # intended overlap between consecutive frames
SCROLLBAR_INSET = 20        # trim right edge so overlay scrollbars don't smear
ESC_KEYCODE = 53


class Cancelled(Exception):
    pass


_cancel_reason = None


def _on_signal(signum, frame):
    global _cancel_reason
    _cancel_reason = 'stopped by second shortcut press'


def applescript(script):
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    if r.returncode != 0:
        log.warning(f"AppleScript error (rc={r.returncode}): {r.stderr.strip()}")
    return r.stdout.strip()


def notify(title, msg):
    applescript(f'display notification "{msg}" with title "{title}"')


def mouse_pos():
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


def warp_mouse(x, y):
    Quartz.CGWarpMouseCursorPosition((x, y))
    Quartz.CGAssociateMouseAndMouseCursorPosition(True)


def esc_pressed():
    return (
        Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateHIDSystemState, ESC_KEYCODE)
        or Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateCombinedSessionState, ESC_KEYCODE)
    )


def check_cancel(region=None, anchor=None):
    """Raise Cancelled if the user asked us to stop."""
    if _cancel_reason:
        raise Cancelled(_cancel_reason)
    if esc_pressed():
        raise Cancelled('Esc pressed')
    if region and anchor:
        x, y = mouse_pos()
        rx, ry, rw, rh = region
        margin = 10
        if not (rx - margin <= x <= rx + rw + margin and ry - margin <= y <= ry + rh + margin):
            raise Cancelled('mouse moved out of the capture area')


def sleep_cancellable(seconds, region=None, anchor=None):
    end = time.time() + seconds
    while time.time() < end:
        check_cancel(region, anchor)
        time.sleep(min(0.1, max(0, end - time.time())))


# ---------------------------------------------------------------- targeting

def window_under_pointer(x, y):
    """Front-most normal window containing the point. Returns (wid, bounds, pid, owner)."""
    wl = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for w in wl:  # list is front-to-back
        if w.get('kCGWindowLayer', 999) != 0:
            continue
        b = w.get('kCGWindowBounds', {})
        if b.get('X', 0) <= x <= b.get('X', 0) + b.get('Width', 0) and \
           b.get('Y', 0) <= y <= b.get('Y', 0) + b.get('Height', 0):
            return (
                w.get('kCGWindowNumber'),
                dict(b),
                w.get('kCGWindowOwnerPID'),
                w.get('kCGWindowOwnerName', '?'),
            )
    return None, None, None, None


def enable_app_accessibility(pid):
    """Chromium/Electron apps only expose their UI tree when asked.

    Only AXManualAccessibility is safe to set: it is a Chromium-specific
    attribute that other apps ignore. Never set AXEnhancedUserInterface —
    it persists on the app and breaks text-expansion tools (Alfred
    snippets) in that app until it restarts.
    """
    try:
        app = AXUIElementCreateApplication(pid)
        AXUIElementSetAttributeValue(app, 'AXManualAccessibility', True)
    except Exception as e:
        log.debug(f"enable_app_accessibility: {e}")


def _ax_attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def scroll_area_under_pointer(x, y):
    """Walk up from the element under the pointer to the nearest scroll area.

    Returns (x, y, w, h) in global screen points, or None.
    """
    sw = AXUIElementCreateSystemWide()
    err, el = AXUIElementCopyElementAtPosition(sw, x, y, None)
    if err != 0 or el is None:
        log.warning(f"AX hit-test failed (err={err})")
        return None
    for depth in range(30):
        role = _ax_attr(el, 'AXRole')
        if role == 'AXScrollArea':
            posv = _ax_attr(el, 'AXPosition')
            sizev = _ax_attr(el, 'AXSize')
            if posv is None or sizev is None:
                return None
            ok1, pt = AXValueGetValue(posv, kAXValueCGPointType, None)
            ok2, sz = AXValueGetValue(sizev, kAXValueCGSizeType, None)
            if not (ok1 and ok2):
                return None
            log.info(f"Scroll area at depth {depth}: ({pt.x}, {pt.y}) {sz.width}x{sz.height}")
            return (pt.x, pt.y, sz.width, sz.height)
        el = _ax_attr(el, 'AXParent')
        if el is None:
            break
    return None


def clamp_region_to_window(region, wb):
    rx, ry, rw, rh = region
    x1 = max(rx, wb['X'])
    y1 = max(ry, wb['Y'])
    x2 = min(rx + rw, wb['X'] + wb['Width'])
    y2 = min(ry + rh, wb['Y'] + wb['Height'])
    return (x1, y1, max(0, x2 - x1), max(0, y2 - y1))


# ---------------------------------------------------------------- scrolling

def post_scroll(points):
    """Scroll the pane under the pointer by `points` (negative = down)."""
    remaining = points
    step = 160 if points > 0 else -160
    while remaining != 0:
        chunk = step if abs(remaining) >= abs(step) else remaining
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 1, int(chunk)
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        remaining -= chunk
        time.sleep(0.01)


# ---------------------------------------------------------------- capture

def capture_window(wid, path):
    subprocess.run(['screencapture', '-l', str(wid), '-x', '-o', path],
                   capture_output=True)
    if not (Path(path).exists() and Path(path).stat().st_size > 0):
        raise RuntimeError(f"screencapture -l {wid} failed")


def capture_region(wid, win_bounds, region, path):
    """Capture the window and crop to the content region (Retina-aware)."""
    capture_window(wid, path)
    img = Image.open(path)
    scale = img.width / win_bounds['Width']
    rx, ry, rw, rh = region
    left = int((rx - win_bounds['X']) * scale)
    top = int((ry - win_bounds['Y']) * scale)
    right = min(img.width, int((rx + rw - win_bounds['X']) * scale))
    bottom = min(img.height, int((ry + rh - win_bounds['Y']) * scale))
    return img.crop((max(0, left), max(0, top), right, bottom)).convert('RGB'), scale


# ---------------------------------------------------------------- comparing

def images_same(a, b, threshold=0.97):
    w = min(a.width, b.width)
    h = min(a.height, b.height)
    if abs(a.height - b.height) > 2:
        return False
    arr_a = np.array(a.crop((0, 0, w, h)), dtype=np.int16)
    arr_b = np.array(b.crop((0, 0, w, h)), dtype=np.int16)
    close = np.all(np.abs(arr_a - arr_b) <= 3, axis=-1)
    return close.mean() >= threshold


# ---------------------------------------------------------------- stitching

def normalize_frames(images):
    """Force every frame to a single common width (Retina rounding drift)."""
    min_w = min(img.width for img in images)
    out = []
    for img in images:
        if img.width != min_w:
            img = img.crop((0, 0, min_w, img.height))
        out.append(img)
    return out


def find_overlap(prev, curr, expected):
    """How many pixels at the bottom of prev match the top of curr.

    We control the scroll distance, so the true overlap is near `expected`.
    Search a narrow window around it first (immune to repetitive content
    aliasing onto the wrong row), then fall back to a strict full search
    (handles the clamped final scroll at the page bottom), then to
    `expected` itself.
    """
    ds = 2
    prev_g = np.array(prev.resize(
        (max(1, prev.width // ds), max(1, prev.height // ds)), Image.LANCZOS
    ).convert('L'), dtype=np.float32)
    curr_g = np.array(curr.resize(
        (max(1, curr.width // ds), max(1, curr.height // ds)), Image.LANCZOS
    ).convert('L'), dtype=np.float32)

    h_p = prev_g.shape[0]
    h_c = curr_g.shape[0]
    if h_p < 10 or h_c < 10:
        return max(0, int(expected))

    # the template band must fit entirely inside the shared region, so keep
    # it well under the expected overlap
    band_h = max(8, min(80, h_c // 5, int(expected * 0.6) // ds if expected > 0 else 80))
    band_flat = curr_g[:band_h].flatten().astype(np.float64)
    band_flat -= band_flat.mean()
    std = band_flat.std()
    if std < 1:
        return max(0, int(expected))
    band_flat /= std

    def best_in(positions):
        best_ncc, best_pos = -1.0, -1
        for pos in positions:
            region = prev_g[pos:pos + band_h].flatten().astype(np.float64)
            region -= region.mean()
            rs = region.std()
            if rs < 1:
                continue
            region /= rs
            ncc = float(np.mean(band_flat * region))
            if ncc > best_ncc:
                best_ncc, best_pos = ncc, pos
        return best_ncc, best_pos

    def to_overlap(pos):
        overlap = (h_p - pos) * ds
        return overlap if 3 <= overlap <= h_c * ds else 0

    # 1. narrow window around the known scroll amount
    if expected > 0:
        center = h_p - int(expected) // ds
        lo = max(0, center - 30)
        hi = min(h_p - band_h, center + 30)
        if lo <= hi:
            ncc, pos = best_in(range(lo, hi + 1))
            if ncc >= 0.6:
                ov = to_overlap(pos)
                if ov:
                    return ov

    # 2. strict full search (final frame at page bottom scrolls less)
    ncc, pos = best_in(range(0, h_p - band_h + 1))
    if ncc >= 0.85:
        ov = to_overlap(pos)
        if ov:
            log.debug(f"full-search overlap {ov}px (ncc {ncc:.2f})")
            return ov

    log.debug(f"no confident match (best ncc {ncc:.2f}), using expected {int(expected)}px")
    return max(0, int(expected))


def exact_match_score(prev, curr, overlap):
    """Fraction of near-identical pixels when curr overlaps prev by `overlap`."""
    if overlap < 3 or overlap > min(prev.height, curr.height):
        return -1.0
    a = np.array(prev.crop((0, prev.height - overlap, prev.width, prev.height)),
                 dtype=np.int16)[::2]
    b = np.array(curr.crop((0, 0, curr.width, overlap)), dtype=np.int16)[::2]
    if a.shape != b.shape:
        return -1.0
    return float(np.all(np.abs(a - b) <= 4, axis=-1).mean())


def refine_overlap(prev, curr, overlap):
    """Nudge an overlap estimate to the exact pixel alignment."""
    if overlap <= 3:
        return overlap
    best_score, best_ov = -1.0, overlap
    for ov in range(max(3, overlap - 3), overlap + 4):
        score = exact_match_score(prev, curr, ov)
        if score > best_score:
            best_score, best_ov = score, ov
    return best_ov


def find_final_overlap(prev, curr, expected):
    """Overlap for the last seam, where the clamped final scroll makes the
    true overlap anywhere between `expected` and a full frame.

    Try every plausible overlap with exact pixel comparison at full
    resolution. Ties resolve toward the largest overlap, so repetitive or
    blank content near the page bottom never duplicates.
    """
    h = min(prev.height, curr.height)
    lo = max(3, int(expected) - 20)
    if lo > h:
        return max(0, int(expected))
    a_full = np.array(prev, dtype=np.int16)
    b_full = np.array(curr, dtype=np.int16)
    best_score, best_ov = -1.0, max(0, int(expected))
    for ov in range(lo, h + 1):
        a = a_full[prev.height - ov:prev.height][::2]
        b = b_full[:ov][::2]
        if a.shape != b.shape:
            continue
        score = float(np.all(np.abs(a - b) <= 4, axis=-1).mean())
        if score >= best_score:
            best_score, best_ov = score, ov
    log.debug(f"final seam: best overlap {best_ov}px (score {best_score:.3f})")
    if best_score < 0.9:
        log.debug("final seam unverified, using expected")
        return max(0, int(expected))
    return best_ov


def detect_sticky_header(images, tolerance=5):
    """Height of a sticky in-content header, from two scrolled frames."""
    if len(images) < 3:
        return 0
    a = np.array(images[1], dtype=np.int16)
    b = np.array(images[2], dtype=np.int16)
    h = min(a.shape[0], b.shape[0]) // 2
    fixed = 0
    misses = 0
    for i in range(h):
        if np.max(np.abs(a[i] - b[i])) <= tolerance:
            fixed = i + 1
            misses = 0
        else:
            misses += 1
            if misses > 3:
                break
    return fixed if fixed >= 20 else 0


def stitch(images, expected_overlap):
    """Stitch overlapping frames; sticky headers appear once, at the top."""
    if len(images) == 1:
        return images[0]

    header_h = detect_sticky_header(images)
    log.info(f"Sticky header: {header_h}px")

    frames = [images[0]]
    if header_h > 0:
        frames += [img.crop((0, header_h, img.width, img.height)) for img in images[1:]]
    else:
        frames += images[1:]

    # a sticky header hides its own height of the shared region in each frame
    expected = max(20, int(expected_overlap) - header_h)

    strips = [frames[0]]
    for i in range(1, len(frames)):
        if i == len(frames) - 1:
            overlap = find_final_overlap(frames[i - 1], frames[i], expected)
        else:
            overlap = find_overlap(frames[i - 1], frames[i], expected)
        overlap = refine_overlap(frames[i - 1], frames[i], overlap)
        log.debug(f"Frame {i}: overlap {overlap}px")
        if 0 < overlap < frames[i].height:
            strips.append(frames[i].crop((0, overlap, frames[i].width, frames[i].height)))
        else:
            strips.append(frames[i])

    total_h = sum(s.height for s in strips)
    result = Image.new('RGB', (images[0].width, total_h))
    y = 0
    for s in strips:
        result.paste(s, (0, y))
        y += s.height
    return result


# ---------------------------------------------------------------- clipboard

def copy_to_clipboard(path):
    """Put the finished PNG on the clipboard. The file on disk stays the
    source of truth; a clipboard failure is never fatal."""
    r = subprocess.run(
        ['osascript', '-e',
         f'set the clipboard to (read (POSIX file "{path}") as «class PNGf»)'],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log.warning(f"clipboard copy failed: {r.stderr.strip()}")
        return False
    return True


# ---------------------------------------------------------------- pidfile

def stop_running_instance():
    """If another capture is running, stop it and exit. Else claim the pidfile."""
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, 0)  # raises if not alive
            os.kill(pid, signal.SIGTERM)
            log.info(f"Stopped running capture (pid {pid})")
            sys.exit(0)
        except (ValueError, ProcessLookupError, PermissionError):
            PIDFILE.unlink(missing_ok=True)
    PIDFILE.write_text(str(os.getpid()))


# ---------------------------------------------------------------- main

def scroll_to_top(wid, win_bounds, region, anchor, tmpdir):
    rx, ry, rw, rh = region
    prev, _ = capture_region(wid, win_bounds, region, f'{tmpdir}/top_probe_a.png')
    for _ in range(30):
        check_cancel(region, anchor)
        warp_mouse(*anchor)
        post_scroll(int(rh * 6))  # positive = up
        sleep_cancellable(SETTLE_TIME, region, anchor)
        curr, _ = capture_region(wid, win_bounds, region, f'{tmpdir}/top_probe_b.png')
        if images_same(prev, curr):
            break
        prev = curr
    log.info("At top")


def main():
    stop_running_instance()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    start_time = time.time()
    px, py = mouse_pos()
    log.info(f"Pointer: ({px:.0f}, {py:.0f})")

    wid, wb, pid, owner = window_under_pointer(px, py)
    if not wid:
        notify('Screenshot Failed', 'No window under the mouse pointer')
        sys.exit(1)
    log.info(f"Window {wid} of {owner} (pid {pid}) at {wb}")

    enable_app_accessibility(pid)
    region = scroll_area_under_pointer(px, py)
    if region and region[2] >= 50 and region[3] >= 50:
        region = clamp_region_to_window(region, wb)
        # trim the right edge so the overlay scrollbar never smears the stitch
        region = (region[0], region[1], max(50, region[2] - SCROLLBAR_INSET), region[3])
    else:
        log.warning("No scroll area found, falling back to full window")
        region = (wb['X'], wb['Y'], wb['Width'] - SCROLLBAR_INSET, wb['Height'])

    rx, ry, rw, rh = region
    anchor = (rx + rw / 2, ry + rh / 2)
    log.info(f"Capture region: ({rx:.0f}, {ry:.0f}) {rw:.0f}x{rh:.0f}")

    notify('Screenshot', f'Capturing {owner}. Esc, shortcut again, or move mouse away to cancel.')
    warp_mouse(*anchor)
    time.sleep(0.15)

    tmpdir = tempfile.mkdtemp()
    try:
        scroll_to_top(wid, wb, region, anchor, tmpdir)

        frames = []
        scale = 2.0
        scroll_step = int(rh - OVERLAP_POINTS)
        prev = None
        stale = 0

        for i in range(MAX_PAGES):
            check_cancel(region, anchor)
            if time.time() - start_time > MAX_DURATION:
                log.warning("Watchdog: hit time limit, stitching what we have")
                notify('Screenshot', 'Hit the time limit, saving what was captured')
                break

            curr, scale = capture_region(wid, wb, region, f'{tmpdir}/shot_{i:03d}.png')

            if prev is not None and images_same(prev, curr):
                stale += 1
                log.info(f"Frame {i}: same (no progress {stale}/{LAZY_RETRIES})")
                if stale >= LAZY_RETRIES:
                    log.info("Reached bottom")
                    break
                sleep_cancellable(LAZY_LOAD_WAIT, region, anchor)
                # re-post the scroll in case the app dropped the event
                warp_mouse(*anchor)
                post_scroll(-scroll_step)
                sleep_cancellable(SETTLE_TIME, region, anchor)
                continue

            stale = 0
            frames.append(curr)
            prev = curr
            log.info(f"Frame {i}: captured ({len(frames)} total)")

            warp_mouse(*anchor)
            post_scroll(-scroll_step)
            sleep_cancellable(SETTLE_TIME, region, anchor)

        if not frames:
            notify('Screenshot Failed', 'Nothing captured')
            sys.exit(1)

        log.info(f"Total frames: {len(frames)}")
        images = normalize_frames(frames)
        expected_overlap = OVERLAP_POINTS * scale
        result = stitch(images, expected_overlap)

        ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        output = Path.home() / 'Desktop' / f'fullpage_{ts}.png'
        result.save(str(output), optimize=True)
        log.info(f"Saved: {output} ({result.width}x{result.height})")
        copied = copy_to_clipboard(output)
        # a stale crash report from an old failure only causes confusion
        (Path.home() / 'Desktop' / 'fullpage_error.txt').unlink(missing_ok=True)
        notify('Screenshot Saved',
               f'{output.name} — copied to clipboard' if copied else output.name)
        print(f"Saved: {output}", file=sys.stderr)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    try:
        main()
    except Cancelled as c:
        log.info(f"Cancelled: {c}")
        notify('Screenshot Cancelled', str(c))
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        import traceback
        err = traceback.format_exc()
        log.error(err)
        notify('Screenshot Failed', err.splitlines()[-1][:80])
        Path(Path.home() / 'Desktop' / 'fullpage_error.txt').write_text(err)
        sys.exit(1)
    finally:
        try:
            if PIDFILE.exists() and PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except Exception:
            pass
