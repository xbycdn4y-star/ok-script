import sys

IS_WINDOWS = sys.platform == "win32"


class _DynamicConstants:
    def __init__(self, **values):
        self.__dict__.update(values)

    def __getattr__(self, name):
        return 0


class _Win32ApiStub:
    def SetConsoleCtrlHandler(self, *_args, **_kwargs):
        return False

    def GetCursorPos(self):
        return 0, 0

    def SetCursorPos(self, _pos):
        return None

    def GetSystemMetrics(self, _index):
        return 0

    def MapVirtualKey(self, _code, _map_type):
        return 0

    def VkKeyScan(self, key):
        if isinstance(key, str) and key:
            return ord(key[0])
        return 0

    def MAKELONG(self, low, high):
        low = int(low) & 0xFFFF
        high = int(high) & 0xFFFF
        return (high << 16) | low

    def EnumDisplayMonitors(self):
        return []

    def GetMonitorInfo(self, _monitor):
        return {"Monitor": (0, 0, 0, 0)}

    def MonitorFromWindow(self, _hwnd, _flag=0):
        return 0

    def GetUserName(self):
        return ""


class _Win32GuiStub:
    def IsWindow(self, _hwnd):
        return False

    def IsWindowEnabled(self, _hwnd):
        return False

    def IsWindowVisible(self, _hwnd):
        return False

    def GetWindowText(self, _hwnd):
        return ""

    def GetClassName(self, _hwnd):
        return ""

    def GetForegroundWindow(self):
        return 0

    def GetWindowRect(self, _hwnd):
        return 0, 0, 0, 0

    def GetClientRect(self, _hwnd):
        return 0, 0, 0, 0

    def ClientToScreen(self, _hwnd, point):
        return point

    def ScreenToClient(self, _hwnd, point):
        return point

    def PostMessage(self, *_args, **_kwargs):
        return 0

    def EnumChildWindows(self, _hwnd, _callback, _extra):
        return None

    def EnumWindows(self, _callback, _extra):
        return None

    def GetParent(self, _hwnd):
        return 0

    def ReleaseDC(self, *_args, **_kwargs):
        return 0

    def DeleteObject(self, *_args, **_kwargs):
        return 0

    def GetWindowLong(self, *_args, **_kwargs):
        return 0

    def SetWindowLong(self, *_args, **_kwargs):
        return 0

    def SetWindowPos(self, *_args, **_kwargs):
        return 0

    def SetForegroundWindow(self, *_args, **_kwargs):
        return 0

    def GetWindowDC(self, *_args, **_kwargs):
        return 0


class _Win32ProcessStub:
    def GetWindowThreadProcessId(self, _hwnd):
        return 0, 0


class _Win32UIStub:
    class error(Exception):
        pass

    def CreateDCFromHandle(self, *_args, **_kwargs):
        raise self.error("win32ui is unavailable on this platform")


class _Win32SecurityStub:
    OWNER_SECURITY_INFORMATION = 0

    def LookupAccountName(self, *_args, **_kwargs):
        return None, None, None

    def GetFileSecurity(self, *_args, **_kwargs):
        return None

    def SetFileSecurity(self, *_args, **_kwargs):
        return None


if IS_WINDOWS:
    import win32api as win32api
    import win32con as win32con
    import win32gui as win32gui
    import win32process as win32process
    import win32security as win32security
    import win32ui as win32ui
else:
    win32api = _Win32ApiStub()
    win32con = _DynamicConstants(
        WHEEL_DELTA=120,
        MONITOR_DEFAULTTONEAREST=0,
        CTRL_C_EVENT=0,
        CTRL_CLOSE_EVENT=0,
        CTRL_LOGOFF_EVENT=0,
        CTRL_SHUTDOWN_EVENT=0,
    )
    win32gui = _Win32GuiStub()
    win32process = _Win32ProcessStub()
    win32security = _Win32SecurityStub()
    win32ui = _Win32UIStub()
