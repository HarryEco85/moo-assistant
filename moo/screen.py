"""'Look at this': a snapshot of the window you're working in, for Moo to read.

Captures the window you were using (not Moo's own window), shrinks it to a
size Claude reads well, and returns it as an image block.
"""
import base64
import io
import logging

log = logging.getLogger("moo")
MAX_SIDE = 1568  # Claude's sweet spot for detail vs cost


def _windows_api():
    import ctypes
    from ctypes import wintypes
    return ctypes, wintypes, ctypes.windll.user32


def _title(user32, ctypes, hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _target_window(skip_titles):
    """The window you're looking at: the foreground one, or the next one down if that's Moo."""
    ctypes, wintypes, user32 = _windows_api()
    hwnd = user32.GetForegroundWindow()
    for _ in range(200):
        if not hwnd:
            return None, ""
        title = _title(user32, ctypes, hwnd)
        visible = user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd)
        if visible and title and not any(title.startswith(t) for t in skip_titles if t):
            return hwnd, title
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT: next window down the stack
    return None, ""


def _rect(hwnd):
    ctypes, wintypes, user32 = _windows_api()
    rect = wintypes.RECT()
    try:  # the visible frame, without the invisible resize border
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    except Exception:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def capture(skip_titles=("Moo",)):
    """Returns (PIL image, window title)."""
    from PIL import ImageGrab
    ctypes, _, user32 = _windows_api()
    old = None
    try:  # real pixel coordinates, so the crop lines up on scaled (125%, 150%) screens
        old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    try:
        hwnd, title = _target_window(skip_titles)
        if hwnd:
            box = _rect(hwnd)
            if box[2] - box[0] > 50 and box[3] - box[1] > 50:
                return ImageGrab.grab(bbox=box, all_screens=True), title
        return ImageGrab.grab(), "the whole screen"
    finally:
        if old:
            try:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
            except Exception:
                pass


def to_block(img, quality=82) -> dict:
    """PIL image -> Claude image content block (JPEG, sensibly sized)."""
    img = img.convert("RGB")
    scale = MAX_SIDE / max(img.size)
    if scale < 1:
        from PIL import Image
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def bytes_to_block(data: bytes) -> dict:
    from PIL import Image
    return to_block(Image.open(io.BytesIO(data)))
