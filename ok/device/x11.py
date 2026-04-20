import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from PySide6.QtGui import QGuiApplication, QImage

from ok.device.capture import BaseCaptureMethod
from ok.device.intercation import BaseInteraction
from ok.gui.Communicate import communicate
from ok.task.exceptions import CaptureException
from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

try:
    from Xlib import X, XK, display
    from Xlib.error import DisplayConnectionError, XError
    from Xlib.ext import xtest
    from Xlib.protocol import event

    XLIB_AVAILABLE = True
except Exception as exc:
    logger.warning(f"python-xlib unavailable: {exc}")
    X = XK = display = xtest = event = None
    DisplayConnectionError = XError = Exception
    XLIB_AVAILABLE = False

try:
    import mss

    MSS_AVAILABLE = True
except Exception as exc:
    logger.warning(f"mss unavailable: {exc}")
    mss = None
    MSS_AVAILABLE = False


@dataclass
class X11WindowInfo:
    window_id: int
    title: str
    wm_class: str
    x: int
    y: int
    width: int
    height: int
    focused: bool
    pid: int = 0


def x11_available() -> bool:
    return XLIB_AVAILABLE and bool(os.environ.get("DISPLAY"))


def _match_pattern(value, pattern) -> bool:
    if pattern in (None, ""):
        return True
    value = value or ""
    if isinstance(pattern, str):
        return pattern.lower() in value.lower()
    return re.search(pattern, value) is not None


def _open_display():
    if not x11_available():
        return None
    try:
        return display.Display()
    except DisplayConnectionError as exc:
        logger.error(f"Unable to open X11 display: {exc}")
        return None


def _window_name(disp, window):
    utf8_atom = disp.intern_atom("UTF8_STRING")
    net_wm_name = disp.intern_atom("_NET_WM_NAME")
    name = window.get_full_property(net_wm_name, utf8_atom)
    if name and getattr(name, "value", None):
        value = name.value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)
    wm_name = window.get_wm_name()
    if isinstance(wm_name, bytes):
        return wm_name.decode("utf-8", errors="ignore")
    return wm_name or ""


def _window_class(window) -> str:
    wm_class = window.get_wm_class()
    if not wm_class:
        return ""
    return ".".join([part for part in wm_class if part])


def _window_pid(disp, window) -> int:
    try:
        atom = disp.intern_atom("_NET_WM_PID")
        value = window.get_full_property(atom, X.AnyPropertyType)
        if value and getattr(value, "value", None):
            return int(value.value[0])
    except Exception:
        return 0
    return 0


def _active_window_id(disp) -> int:
    root = disp.screen().root
    atom = disp.intern_atom("_NET_ACTIVE_WINDOW")
    prop = root.get_full_property(atom, X.AnyPropertyType)
    if prop and getattr(prop, "value", None):
        return int(prop.value[0])
    return 0


def _window_info_from_id(disp, window_id: int) -> Optional[X11WindowInfo]:
    if not disp or not window_id:
        return None
    try:
        window = disp.create_resource_object("window", window_id)
        attrs = window.get_attributes()
        if attrs.map_state != X.IsViewable:
            return None
        geom = window.get_geometry()
        if geom.width <= 10 or geom.height <= 10:
            return None
        translated = window.translate_coords(disp.screen().root, 0, 0)
        return X11WindowInfo(
            window_id=window_id,
            title=_window_name(disp, window).strip(),
            wm_class=_window_class(window),
            x=translated.x,
            y=translated.y,
            width=geom.width,
            height=geom.height,
            focused=_active_window_id(disp) == window_id,
            pid=_window_pid(disp, window),
        )
    except XError:
        return None
    except Exception as exc:
        logger.debug(f"Failed to read X11 window {window_id}: {exc}")
        return None


def find_x11_windows(title=None, wm_class=None, selected_window: int = 0) -> list[X11WindowInfo]:
    disp = _open_display()
    if disp is None:
        return []
    try:
        root = disp.screen().root
        atoms = [
            disp.intern_atom("_NET_CLIENT_LIST_STACKING"),
            disp.intern_atom("_NET_CLIENT_LIST"),
        ]
        window_ids = []
        for atom in atoms:
            prop = root.get_full_property(atom, X.AnyPropertyType)
            if prop and getattr(prop, "value", None):
                window_ids = [int(value) for value in prop.value]
                if window_ids:
                    break

        windows = []
        for window_id in window_ids:
            info = _window_info_from_id(disp, window_id)
            if info is None:
                continue
            if not info.title and not info.wm_class:
                continue
            if not _match_pattern(info.title, title) or not _match_pattern(info.wm_class, wm_class):
                continue
            windows.append(info)

        windows.sort(key=lambda item: ((item.window_id == selected_window), item.focused, item.width * item.height),
                     reverse=True)
        return windows
    finally:
        try:
            disp.close()
        except Exception:
            pass


def get_x11_window(window_id: int) -> Optional[X11WindowInfo]:
    disp = _open_display()
    if disp is None:
        return None
    try:
        return _window_info_from_id(disp, window_id)
    finally:
        try:
            disp.close()
        except Exception:
            pass


def activate_x11_window(window_id: int) -> bool:
    disp = _open_display()
    if disp is None or not window_id:
        return False
    try:
        root = disp.screen().root
        window = disp.create_resource_object("window", window_id)
        active_atom = disp.intern_atom("_NET_ACTIVE_WINDOW")
        client_message = event.ClientMessage(
            window=window,
            client_type=active_atom,
            data=(32, [1, X.CurrentTime, window_id, 0, 0]),
        )
        root.send_event(
            client_message,
            event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
        )
        try:
            window.map()
        except Exception:
            pass
        try:
            window.configure(stack_mode=X.Above)
        except Exception:
            pass
        try:
            window.set_input_focus(X.RevertToParent, X.CurrentTime)
        except Exception:
            pass
        disp.sync()
        if is_x11_foreground(window_id):
            return True
    except Exception as exc:
        logger.error(f"Failed to activate X11 window {window_id}: {exc}")
    finally:
        try:
            disp.close()
        except Exception:
            pass

    # Fallback for WMs/XWayland setups that ignore the native client message.
    try:
        result = subprocess.run(
            ["xdotool", "windowactivate", "--sync", str(window_id)],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return True
        logger.warning(f"xdotool windowactivate failed for {window_id}: {result.stderr.strip()}")
    except Exception as exc:
        logger.warning(f"xdotool windowactivate unavailable for {window_id}: {exc}")

    return False


def is_x11_foreground(window_id: int) -> bool:
    disp = _open_display()
    if disp is None or not window_id:
        return False
    try:
        return _active_window_id(disp) == window_id
    finally:
        try:
            disp.close()
        except Exception:
            pass


class X11Window:
    def __init__(self, exit_event, device_manager=None, title=None, wm_class=None):
        self.app_exit_event = exit_event
        self.device_manager = device_manager
        self.title = title
        self.wm_class = wm_class
        self.stop_event = threading.Event()
        self.visible = False
        self.window_width = 0
        self.window_height = 0
        self.x = 0
        self.y = 0
        self.width = 0
        self.height = 0
        self.hwnd = 0
        self.exists = False
        self.scaling = 1.0
        self.frame_width = 0
        self.frame_height = 0
        self.frame_aspect_ratio = 0
        self.real_width = 0
        self.real_height = 0
        self.real_x_offset = 0
        self.real_y_offset = 0
        self.top_hwnd = 0
        self.top_offset_x = 0
        self.top_offset_y = 0
        self.pos_valid = False
        self.visible_monitors = []
        self.to_handle_mute = False
        self._hwnd_title = ""
        self.thread = threading.Thread(target=self.update_window_size, name="update_x11_window_size", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def update_window(self, title=None, wm_class=None):
        self.title = title
        self.wm_class = wm_class

    def update_frame_size(self, width, height):
        self.frame_width = width
        self.frame_height = height
        self.frame_aspect_ratio = width / height if width and height else 0

    def bring_to_front(self):
        if self.hwnd:
            activate_x11_window(self.hwnd)

    def try_resize_to(self, _resize_to):
        return False

    def get_abs_cords(self, x, y):
        return self.x + x, self.y + y

    def get_top_window_cords(self, x, y):
        return x, y

    def is_foreground(self):
        return is_x11_foreground(self.hwnd)

    @property
    def hwnd_title(self):
        return self._hwnd_title

    def update_window_size(self):
        while not self.app_exit_event.is_set() and not self.stop_event.is_set():
            self.do_update_window_size()
            time.sleep(0.2)

    def do_update_window_size(self):
        selected = 0
        if self.device_manager is not None:
            selected = int(self.device_manager.config.get("selected_x11_window") or 0)

        info = get_x11_window(selected) if selected else None
        if info is None and (self.title or self.wm_class):
            windows = find_x11_windows(title=self.title, wm_class=self.wm_class, selected_window=selected)
            info = windows[0] if windows else None

        exists = info is not None
        visible = bool(info and info.focused)
        changed = (
            exists != self.exists
            or visible != self.visible
            or (info and (
                info.window_id != self.hwnd
                or info.x != self.x
                or info.y != self.y
                or info.width != self.width
                or info.height != self.height
                or info.title != self._hwnd_title
            ))
        )

        if info:
            self.hwnd = info.window_id
            self.top_hwnd = info.window_id
            self.x = info.x
            self.y = info.y
            self.width = info.width
            self.height = info.height
            self.window_width = info.width
            self.window_height = info.height
            self.real_width = info.width
            self.real_height = info.height
            self.real_x_offset = 0
            self.real_y_offset = 0
            self._hwnd_title = info.title or info.wm_class or f"X11-{info.window_id}"
            self.pos_valid = info.width > 0 and info.height > 0 and info.x >= 0 and info.y >= 0
        else:
            self.hwnd = 0
            self.top_hwnd = 0
            self.width = self.height = 0
            self.window_width = self.window_height = 0
            self.real_width = self.real_height = 0
            self.pos_valid = False

        self.exists = exists
        self.visible = visible

        if changed:
            for monitor in self.visible_monitors:
                try:
                    monitor.on_visible(self.visible)
                except Exception:
                    pass
            if self.device_manager:
                device = self.device_manager.get_preferred_device()
                if device and device.get("device") == "linux_x11":
                    device["connected"] = exists
                    device["width"] = self.width
                    device["height"] = self.height
                    if self.width and self.height:
                        device["resolution"] = f"{self.width}x{self.height}"
                    communicate.adb_devices.emit(True)
            communicate.window.emit(
                self.visible,
                self.x,
                self.y,
                self.window_width,
                self.window_height,
                self.width,
                self.height,
                self.scaling,
            )


class X11RegionCaptureMethod(BaseCaptureMethod):
    name = "X11Region"
    description = "Foreground X11 region capture"

    def __init__(self, x11_window: X11Window):
        super().__init__()
        self.hwnd_window = x11_window
        self._mss_instances = {}
        self._mss_lock = threading.Lock()
        self._frame_cache_lock = threading.Lock()
        self._cached_frame = None
        self._cached_frame_at = 0.0
        self._cached_window_state = None

    def close(self):
        with self._mss_lock:
            for instance in self._mss_instances.values():
                try:
                    instance.close()
                except Exception:
                    pass
            self._mss_instances.clear()
        with self._frame_cache_lock:
            self._cached_frame = None
            self._cached_frame_at = 0.0
            self._cached_window_state = None

    def connected(self):
        return self.hwnd_window is not None and self.hwnd_window.exists and self.hwnd_window.hwnd > 0

    def clickable(self):
        return self.hwnd_window is not None and self.hwnd_window.visible

    def get_abs_cords(self, x, y):
        return self.hwnd_window.get_abs_cords(x, y)

    def _capture_max_fps(self) -> int:
        device_manager = getattr(self.hwnd_window, "device_manager", None)
        global_config = getattr(device_manager, "global_config", None)
        if global_config is None:
            return 0
        try:
            value = global_config.get_config('Basic Options').get('Capture Max FPS', 0)
            return max(0, int(value or 0))
        except Exception:
            return 0

    def _window_state(self, left, top, width, height):
        return int(self.hwnd_window.hwnd), left, top, width, height

    def _cached_frame_if_fresh(self, window_state):
        max_fps = self._capture_max_fps()
        if max_fps <= 0:
            return None
        min_interval = 1.0 / max_fps
        now = time.time()
        with self._frame_cache_lock:
            if self._cached_frame is None:
                return None
            if self._cached_window_state != window_state:
                return None
            if now - self._cached_frame_at >= min_interval:
                return None
            return self._cached_frame.copy()

    def _update_frame_cache(self, frame, window_state):
        with self._frame_cache_lock:
            self._cached_frame = frame.copy()
            self._cached_frame_at = time.time()
            self._cached_window_state = window_state

    def do_get_frame(self):
        if not self.connected():
            return None
        left = int(self.hwnd_window.x)
        top = int(self.hwnd_window.y)
        width = int(self.hwnd_window.width)
        height = int(self.hwnd_window.height)
        if width <= 0 or height <= 0:
            return None
        window_state = self._window_state(left, top, width, height)
        cached = self._cached_frame_if_fresh(window_state)
        if cached is not None:
            return cached
        if not MSS_AVAILABLE:
            frame = self._grab_with_qt()
            if frame is not None:
                self._update_frame_cache(frame, window_state)
                return frame
            raise CaptureException("mss is not installed")
        monitor = {"left": left, "top": top, "width": width, "height": height}
        try:
            frame = np.asarray(self._get_mss().grab(monitor))
            if frame is not None:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                self._update_frame_cache(frame, window_state)
                return frame
        except Exception as exc:
            logger.warning(f"mss X11 capture failed, fallback to Qt grabWindow: {exc}")

        frame = self._grab_with_qt()
        if frame is not None:
            self._update_frame_cache(frame, window_state)
            return frame
        raise CaptureException("X11 capture failed")

    def _grab_with_qt(self):
        app = QGuiApplication.instance()
        if app is None:
            logger.warning("Qt fallback capture skipped: no QGuiApplication instance")
            return None

        screen = app.screenAt(self._qt_point()) or app.primaryScreen()
        if screen is None:
            logger.warning("Qt fallback capture skipped: no screen available")
            return None

        width = int(self.hwnd_window.width)
        height = int(self.hwnd_window.height)
        if width <= 0 or height <= 0:
            return None

        pixmap = screen.grabWindow(int(self.hwnd_window.hwnd), 0, 0, width, height)
        if pixmap.isNull():
            pixmap = screen.grabWindow(0, int(self.hwnd_window.x), int(self.hwnd_window.y), width, height)
        if pixmap.isNull():
            logger.warning("Qt fallback capture returned null pixmap")
            return None

        image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        ptr = image.bits()
        frame = np.frombuffer(ptr, np.uint8, image.width() * image.height() * 4).reshape(
            image.height(), image.width(), 4
        )
        frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        return frame

    def _qt_point(self):
        from PySide6.QtCore import QPoint

        return QPoint(int(self.hwnd_window.x + 1), int(self.hwnd_window.y + 1))

    def _get_mss(self):
        thread_id = threading.get_ident()
        with self._mss_lock:
            instance = self._mss_instances.get(thread_id)
            if instance is None:
                instance = mss.mss()
                self._mss_instances[thread_id] = instance
            return instance


class X11ForegroundInteraction(BaseInteraction):
    KEY_MAP = {
        "lshift": "Shift_L",
        "rshift": "Shift_R",
        "shift": "Shift_L",
        "lctrl": "Control_L",
        "rctrl": "Control_R",
        "ctrl": "Control_L",
        "control": "Control_L",
        "lalt": "Alt_L",
        "ralt": "Alt_R",
        "alt": "Alt_L",
        "return": "Return",
        "enter": "Return",
        "space": "space",
        "tab": "Tab",
        "backspace": "BackSpace",
        "esc": "Escape",
        "pageup": "Page_Up",
        "pagedown": "Page_Down",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "meta": "Super_L",
        "windows": "Super_L",
        "command": "Super_L",
    }

    def __init__(self, capture: X11RegionCaptureMethod, x11_window: X11Window):
        super().__init__(capture)
        self.hwnd_window = x11_window
        self.display = _open_display()

    def on_destroy(self):
        if self.display is not None:
            try:
                self.display.close()
            except Exception:
                pass
            self.display = None

    def activate(self):
        self.hwnd_window.bring_to_front()

    def clickable(self):
        return self.hwnd_window is not None and self.hwnd_window.is_foreground()

    def should_capture(self):
        return self.clickable()

    def on_run(self):
        self.activate()

    def _ensure_display(self):
        if self.display is None:
            self.display = _open_display()
        return self.display

    def _keycode(self, key):
        disp = self._ensure_display()
        if disp is None:
            return 0
        key_name = self.KEY_MAP.get(str(key).lower(), str(key))
        keysym = XK.string_to_keysym(key_name)
        if keysym == 0 and len(str(key)) == 1:
            keysym = XK.string_to_keysym(str(key))
        return disp.keysym_to_keycode(keysym)

    def _flush(self):
        disp = self._ensure_display()
        if disp is not None:
            disp.sync()

    def send_key(self, key, down_time=0.02):
        if not self.clickable():
            return
        keycode = self._keycode(key)
        disp = self._ensure_display()
        if not keycode or disp is None:
            return
        xtest.fake_input(disp, X.KeyPress, keycode)
        self._flush()
        time.sleep(down_time)
        xtest.fake_input(disp, X.KeyRelease, keycode)
        self._flush()

    def send_key_down(self, key):
        if not self.clickable():
            return
        keycode = self._keycode(key)
        disp = self._ensure_display()
        if not keycode or disp is None:
            return
        xtest.fake_input(disp, X.KeyPress, keycode)
        self._flush()

    def send_key_up(self, key):
        keycode = self._keycode(key)
        disp = self._ensure_display()
        if not keycode or disp is None:
            return
        xtest.fake_input(disp, X.KeyRelease, keycode)
        self._flush()

    def move(self, x, y):
        if not self.clickable():
            return
        disp = self._ensure_display()
        if disp is None:
            return
        abs_x, abs_y = self.capture.get_abs_cords(x, y)
        xtest.fake_input(disp, X.MotionNotify, x=int(abs_x), y=int(abs_y))
        self._flush()

    def _button_number(self, key):
        if key == "right":
            return 3
        if key == "middle":
            return 2
        return 1

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.05, move=True, key="left"):
        if not self.clickable():
            return
        disp = self._ensure_display()
        if disp is None:
            return
        if x != -1 and y != -1:
            self.move(x, y)
            time.sleep(0.01)
        button = self._button_number(key)
        xtest.fake_input(disp, X.ButtonPress, button)
        self._flush()
        time.sleep(down_time)
        xtest.fake_input(disp, X.ButtonRelease, button)
        self._flush()

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        if not self.clickable():
            return
        disp = self._ensure_display()
        if disp is None:
            return
        if x != -1 and y != -1:
            self.move(x, y)
            time.sleep(0.01)
        xtest.fake_input(disp, X.ButtonPress, self._button_number(key))
        self._flush()

    def mouse_up(self, key="left"):
        disp = self._ensure_display()
        if disp is None:
            return
        xtest.fake_input(disp, X.ButtonRelease, self._button_number(key))
        self._flush()

    def scroll(self, x, y, scroll_amount):
        if not self.clickable():
            return
        disp = self._ensure_display()
        if disp is None:
            return
        if x != -1 and y != -1:
            self.move(x, y)
        button = 4 if scroll_amount > 0 else 5
        for _ in range(abs(scroll_amount)):
            xtest.fake_input(disp, X.ButtonPress, button)
            xtest.fake_input(disp, X.ButtonRelease, button)
        self._flush()

    def swipe(self, from_x, from_y, to_x, to_y, duration, settle_time=0, after_sleep=0.1):
        if not self.clickable():
            return
        disp = self._ensure_display()
        if disp is None:
            return
        self.move(from_x, from_y)
        time.sleep(0.02)
        xtest.fake_input(disp, X.ButtonPress, 1)
        steps = max(int(duration / 16), 5)
        for i in range(steps + 1):
            progress = i / steps
            x = round(from_x + (to_x - from_x) * progress)
            y = round(from_y + (to_y - from_y) * progress)
            self.move(x, y)
            time.sleep(max(duration / 1000 / steps, 0.005))
        if settle_time > 0:
            time.sleep(settle_time)
        xtest.fake_input(disp, X.ButtonRelease, 1)
        self._flush()
        if after_sleep > 0:
            time.sleep(after_sleep)
