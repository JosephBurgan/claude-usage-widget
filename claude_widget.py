"""Claude Usage Widget — floating always-on-top window showing Claude.ai plan usage.

Reads OAuth tokens from ~/.claude/.credentials.json (created by the Claude Code
CLI), polls https://api.anthropic.com/api/oauth/usage every 30 seconds, and
auto-refreshes the access token when it's near expiry.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path

import requests
from requests.exceptions import HTTPError

# ── Paths ────────────────────────────────────────────────────────────────────

HOME          = Path.home()
CREDS_FILE    = HOME / ".claude" / ".credentials.json"
SETTINGS_FILE = HOME / ".claude_widget_settings.json"
REPO_DIR      = Path(__file__).resolve().parent


# ── Atomic JSON I/O ──────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON via temp-file + os.replace so a crash mid-write can't corrupt the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup; original file is untouched.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Settings ─────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    try:
        return _read_json(SETTINGS_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(s: dict) -> None:
    _write_json_atomic(SETTINGS_FILE, s)


# Rows hidden by default on first launch (user can re-enable from settings).
DEFAULT_HIDDEN = ["7-day Sonnet", "Extra credits"]


# ── OAuth / API ──────────────────────────────────────────────────────────────

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
API_HDRS  = {"anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}

# Refresh the access token when it's within this many ms of expiry.
TOKEN_REFRESH_MARGIN_MS = 5 * 60 * 1000


def _load_creds() -> dict:
    return _read_json(CREDS_FILE)["claudeAiOauth"]


def _save_creds_update(oauth_patch: dict) -> None:
    root = _read_json(CREDS_FILE)
    root["claudeAiOauth"].update(oauth_patch)
    _write_json_atomic(CREDS_FILE, root)


def get_fresh_token() -> str:
    """Return a valid access token, refreshing it if it's near expiry."""
    creds = _load_creds()
    if creds["expiresAt"] - time.time() * 1000 > TOKEN_REFRESH_MARGIN_MS:
        return creds["accessToken"]

    r = requests.post(
        TOKEN_URL,
        json={
            "grant_type":    "refresh_token",
            "client_id":     CLIENT_ID,
            "refresh_token": creds["refreshToken"],
            "scope":         " ".join(creds.get("scopes", [])),
        },
        headers={"Content-Type": "application/json", **API_HDRS},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()

    _save_creds_update({
        "accessToken":  data["access_token"],
        "refreshToken": data.get("refresh_token", creds["refreshToken"]),
        "expiresAt":    int(time.time() * 1000) + data["expires_in"] * 1000,
    })
    return data["access_token"]


def fetch_usage() -> dict:
    token = get_fresh_token()
    r = requests.get(
        USAGE_URL,
        headers={"Authorization": f"Bearer {token}", **API_HDRS},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def is_auth_error(exc: BaseException) -> bool:
    """True if the exception represents an auth/credentials problem (vs network/server)."""
    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, HTTPError) and exc.response is not None:
        return exc.response.status_code in (400, 401, 403)
    return False


# ── Self-update via git ──────────────────────────────────────────────────────

def _git(*args: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run `git` in the repo directory, capturing stdout/stderr as text."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        # Hide the console window that would otherwise flash on Windows
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def is_git_checkout() -> bool:
    return (REPO_DIR / ".git").exists()


def current_version() -> str:
    """Human-readable version: the latest tag (e.g. `v1.0.0`), or `<tag>+N` if
    the working tree is N commits past it, or a short SHA if no tags exist."""
    if not is_git_checkout():
        return "unknown"
    try:
        r = _git("describe", "--tags", "--always", "--abbrev=7")
        if r.returncode != 0 or not r.stdout.strip():
            return "unknown"
        desc = r.stdout.strip()
        # `git describe --tags` returns either an exact tag (e.g. `v1.0.0`),
        # `<tag>-<n>-g<sha>` (post-tag commit), or a bare short SHA.
        # Rewrite `<tag>-<n>-g<sha>` to `<tag>+<n>` for a cleaner display.
        parts = desc.rsplit("-", 2)
        if len(parts) == 3 and parts[2].startswith("g"):
            return f"{parts[0]}+{parts[1]}"
        return desc
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def check_for_update() -> tuple[str, str]:
    """Return (status, detail). status ∈ {'available', 'current', 'error'}."""
    if not is_git_checkout():
        return "error", "Not a git checkout — manual update only"
    try:
        fetch = _git("fetch", "--quiet", "origin")
        if fetch.returncode != 0:
            return "error", (fetch.stderr or "git fetch failed").strip()
        local  = _git("rev-parse", "HEAD").stdout.strip()
        remote = _git("rev-parse", "origin/main").stdout.strip()
        if not remote:
            return "error", "Could not resolve origin/main"
        if local == remote:
            return "current", ""
        short = _git("rev-parse", "--short", "origin/main").stdout.strip()
        return "available", short
    except FileNotFoundError:
        return "error", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return "error", "git timed out"


def install_update() -> str | None:
    """Run `git pull --ff-only` then `pip install -r requirements.txt`.

    Returns None on success, error string otherwise.
    """
    try:
        r = _git("pull", "--ff-only", "origin", "main", timeout=30)
    except FileNotFoundError:
        return "git not found on PATH"
    except subprocess.TimeoutExpired:
        return "git pull timed out"
    if r.returncode != 0:
        return (r.stderr or r.stdout or "git pull failed").strip()

    # Sync Python dependencies in case requirements.txt changed.
    req = REPO_DIR / "requirements.txt"
    if req.exists():
        # Use python.exe rather than pythonw.exe so pip has stdio.
        py = Path(sys.executable).with_name("python.exe")
        if not py.exists():
            py = Path(sys.executable)
        try:
            pip = subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet", "--user",
                 "-r", str(req)],
                cwd=REPO_DIR,
                capture_output=True, text=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if pip.returncode != 0:
                err = (pip.stderr or pip.stdout or "?").strip()
                return f"pip install failed: {err[:200]}"
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return f"pip install error: {exc}"
    return None


def startup_shortcut_path() -> Path:
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup"
            / "Claude Usage Widget.lnk")


def is_startup_enabled() -> bool:
    return startup_shortcut_path().exists()


def set_startup_enabled(enabled: bool) -> str | None:
    """Create or remove the Startup shortcut. None on success, error string otherwise."""
    path = startup_shortcut_path()
    if not enabled:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            return str(exc)
        return None

    vbs  = REPO_DIR / "claude_widget.vbs"
    icon = REPO_DIR / "claude_widget.ico"
    if not vbs.exists():
        return f"Launcher not found: {vbs}"
    ps = (
        f'$sh = New-Object -ComObject WScript.Shell;'
        f'$lnk = $sh.CreateShortcut("{path}");'
        f'$lnk.TargetPath = "wscript.exe";'
        f"$lnk.Arguments = '\"{vbs}\"';"
        f'$lnk.WorkingDirectory = "{REPO_DIR}";'
        + (f'$lnk.IconLocation = "{icon}";' if icon.exists() else "")
        + '$lnk.Description = "Floating Claude usage widget";'
          '$lnk.Save()'
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if r.returncode != 0:
        return (r.stderr or "Failed to create shortcut").strip()
    return None


def relaunch_widget() -> None:
    """Spawn a fresh widget process via the VBS launcher, then exit this one."""
    vbs = REPO_DIR / "claude_widget.vbs"
    if vbs.exists():
        subprocess.Popen(["wscript", str(vbs)], close_fds=True)
    else:
        # Fall back to pythonw on the script directly
        pyw = Path(sys.executable).with_name("pythonw.exe")
        cmd = [str(pyw if pyw.exists() else sys.executable), str(REPO_DIR / "claude_widget.py")]
        subprocess.Popen(cmd, close_fds=True)
    os._exit(0)


def retry_after_seconds(exc: BaseException) -> int | None:
    """If exc is a 429, return how many seconds to wait before retrying.

    Honors the Retry-After header when it's a positive integer; falls back
    otherwise (some servers send an invalid `Retry-After: 0`).
    """
    if not (isinstance(exc, HTTPError) and exc.response is not None
            and exc.response.status_code == 429):
        return None
    raw = exc.response.headers.get("Retry-After", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return RATE_LIMIT_FALLBACK_S


# ── Display helpers ───────────────────────────────────────────────────────────

TIER_LABELS = {
    "five_hour":            "5-hour",
    "seven_day":            "7-day",
    "seven_day_opus":       "7-day Opus",
    "seven_day_sonnet":     "7-day Sonnet",
    "seven_day_oauth_apps": "7-day Apps",
    "seven_day_cowork":     "7-day Cowork",
    "seven_day_omelette":   "7-day Pro",
}

# Window duration per tier label, in seconds. Used to render the time-elapsed
# bar under each row. Labels not in this map omit the time bar.
TIER_WINDOW_S = {
    "5-hour":        5 * 3600,
    "7-day":         7 * 86400,
    "7-day Opus":    7 * 86400,
    "7-day Sonnet":  7 * 86400,
    "7-day Apps":    7 * 86400,
    "7-day Cowork":  7 * 86400,
    "7-day Pro":     7 * 86400,
}


def _time_progress(resets_at_iso: str, window_s: int | None) -> float | None:
    """Return 0..1 = fraction of the window elapsed; None if window unknown."""
    if not window_s or not resets_at_iso:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    remaining = (dt - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return 1.0
    if remaining >= window_s:
        return 0.0
    return 1.0 - (remaining / window_s)


def _fmt_countdown(resets_at_iso: str) -> str:
    from datetime import datetime, timezone
    try:
        dt   = datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
        diff = int((dt - datetime.now(timezone.utc)).total_seconds())
    except ValueError:
        return ""
    if diff <= 0:
        return "resetting…"
    h, rem = divmod(diff, 3600)
    m = rem // 60
    return f"resets in {h}h {m}m" if h else f"resets in {m}m"


Row = tuple[str, float, str, float | None]
# (label, utilization 0..1, sublabel, time_progress 0..1 or None)


def parse_usage(data: dict) -> list[Row]:
    rows: list[Row] = []

    for key, label in TIER_LABELS.items():
        tier = data.get(key)
        if not tier or tier.get("utilization") is None:
            continue
        resets_at = tier.get("resets_at") or ""
        util      = float(tier["utilization"]) / 100.0
        countdown = _fmt_countdown(resets_at)
        elapsed   = _time_progress(resets_at, TIER_WINDOW_S.get(label))
        rows.append((label, util, countdown, elapsed))

    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used  = float(extra.get("used_credits") or 0)
        limit = float(extra.get("monthly_limit") or 0)
        util  = used / limit if limit > 0 else 0.0
        cur   = extra.get("currency", "USD")
        rows.append(("Extra credits", util, f"{cur} {used:.2f} / {limit:.2f}", None))

    return rows


# ── Widget ────────────────────────────────────────────────────────────────────

BG           = "#1a1a1a"
FG           = "#e0e0e0"
FG_DIM       = "#888888"
BAR_BG       = "#333333"
BAR_LOW      = "#4caf50"
BAR_MED      = "#ff9800"
BAR_HIGH     = "#f44336"
ACCENT       = "#7c7c7c"
ACCENT_OFF   = "#444444"
SEPARATOR    = "#333333"
BTN_BG       = "#2a2a2a"
BTN_BG_HOVER = "#3a3a3a"
TIME_BAR_BG  = "#262626"   # slightly darker than BAR_BG
TIME_BAR_FG  = "#999999"   # quiet light gray
BAR_H        = 6
TIME_BAR_H   = 2            # slim accent under the usage bar
WIDTH        = 220

# Discrete refresh-timer presets, in minutes.
MINUTE_PRESETS      = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 25, 30, 45, 60]
REFRESH_DEFAULT_MIN = 5
RATE_LIMIT_FALLBACK_S = 120  # Used when 429 response omits Retry-After.


def bar_color(util: float) -> str:
    if util >= 0.9:  return BAR_HIGH
    if util >= 0.6:  return BAR_MED
    return BAR_LOW


class UsageWidget(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.93)
        self.configure(bg=BG)
        self.resizable(False, False)

        self._on_top      = True
        self._drag_x      = 0
        self._drag_y      = 0
        self._settings    = load_settings()
        if "hidden" not in self._settings:
            # First launch: seed with sensible defaults so the user can re-enable
            # any of them from the settings panel.
            self._settings["hidden"] = list(DEFAULT_HIDDEN)
            save_settings(self._settings)
        self._hidden: set[str] = set(self._settings["hidden"])
        self._refresh_min = self._init_refresh_minutes()
        self._manage_mode = False
        self._last_rows: list[Row] = []
        self._destroyed   = False
        self._fetch_in_flight = False
        self._next_after_id: str | None = None

        # Self-update state
        self._version            = current_version()
        self._update_state       = "idle"   # idle, checking, current, available, installing, error
        self._update_detail      = ""        # SHA when available, error msg when error
        self._update_frame: tk.Frame | None = None

        # Settings popup (created on demand when ≡ pressed)
        self._settings_win: tk.Toplevel | None = None

        # Tray state
        self._tray = None  # set by _setup_tray if pystray is available
        self._deps_missing      = not self._has_tray_deps()
        self._deps_install_state = "idle"   # idle, installing, error
        self._deps_install_error = ""

        self._build_ui()
        self._apply_icon()
        self.update_idletasks()
        self._place_initial()
        self.lift()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_tray()
        self._schedule_refresh()

        # Auto-check for updates a few seconds after startup so we don't
        # delay first paint.
        self.after(5000, self._auto_check_for_update)

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        hdr = tk.Frame(self, bg=BG, cursor="fleur")
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        self._bind_drag(hdr)

        title = tk.Label(hdr, text="Claude Usage", bg=BG, fg=FG,
                         font=("Segoe UI", 9, "bold"), cursor="fleur")
        title.pack(side="left")
        self._bind_drag(title)

        # Header buttons (right-to-left order in the layout).
        close = self._make_icon_btn(hdr, "✕", self._on_close, size=11)
        close.pack(side="right", padx=(0, 4))

        self._gear_btn = self._make_icon_btn(hdr, "≡", self._toggle_manage, size=12)
        self._gear_btn.pack(side="right", padx=(0, 6))

        self._pin_btn = self._make_icon_btn(hdr, "📌", self._toggle_topmost, size=9)
        self._pin_btn.pack(side="right", padx=(12, 6))
        # Pin starts pinned (white) since the window initializes topmost
        self._pin_btn.config(fg="white")

        tk.Frame(self, bg=SEPARATOR, height=1).pack(fill="x", padx=6, pady=(0, 4))

        self._body = tk.Frame(self, bg=BG)
        self._body.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(self._body, text="Loading…", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w")

        self._make_refresh_button(self).pack(fill="x", padx=8, pady=(4, 2))

        self._ts_lbl = tk.Label(self, bg=BG, fg=FG_DIM, font=("Segoe UI", 7))
        self._ts_lbl.pack(anchor="e", padx=8, pady=(0, 4))

    def _make_icon_btn(self, parent: tk.Misc, text: str, cb, size: int = 11) -> tk.Label:
        lbl = tk.Label(parent, text=text, bg=BG, fg=ACCENT,
                       font=("Segoe UI", size), cursor="hand2",
                       bd=0, padx=0, pady=0)
        lbl.bind("<Button-1>", lambda _e: cb())
        return lbl

    def _bind_drag(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>",   self._drag_start)
        widget.bind("<B1-Motion>",       self._drag_move)
        widget.bind("<ButtonRelease-1>", lambda _e: self._save_position())

    def _apply_icon(self) -> None:
        icon = REPO_DIR / "claude_widget.ico"
        if not icon.exists():
            return
        try:
            self.iconbitmap(default=str(icon))
        except tk.TclError:
            pass

    def _place_bottom_right(self) -> None:
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{sw - WIDTH - 20}+{sh - 220}")

    def _place_initial(self) -> None:
        """Restore last-known position if the user opted in, else bottom-right."""
        if self._settings.get("remember_position", True):
            x = self._settings.get("window_x")
            y = self._settings.get("window_y")
            if isinstance(x, int) and isinstance(y, int):
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
                # Sanity-check: window must remain visible (allow small overflow).
                if -100 < x < sw - 50 and -100 < y < sh - 50:
                    self.geometry(f"+{x}+{y}")
                    return
        self._place_bottom_right()

    def _save_position(self) -> None:
        if not self._settings.get("remember_position", True):
            return
        try:
            self._settings["window_x"] = int(self.winfo_x())
            self._settings["window_y"] = int(self.winfo_y())
            save_settings(self._settings)
        except (OSError, tk.TclError):
            pass

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        """Close button. If the user opted in (and the tray is up), hide; else quit."""
        if self._tray is not None and self._settings.get("minimize_on_close", False):
            self.withdraw()
        else:
            self._quit_app()

    def _quit_app(self) -> None:
        """Fully exit: stop tray loop, destroy window."""
        self._save_position()
        self._destroyed = True
        self._close_settings_window()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _show_and_raise(self) -> None:
        """Reveal the widget and bring it above other windows."""
        if self.state() == "withdrawn":
            self.deiconify()
        self.lift()
        # Force topmost briefly so we surface over other apps. If the user
        # hasn't pinned us, drop the topmost flag again after a moment so
        # the window doesn't permanently stick.
        self.attributes("-topmost", True)
        if not self._on_top:
            self.after(150, lambda: self.attributes("-topmost", False))

    def _hide_window(self) -> None:
        self._close_settings_window()
        if self._manage_mode:
            self._manage_mode = False
            self._gear_btn.config(fg=ACCENT)
        self.withdraw()

    # ── tray ──────────────────────────────────────────────────────────────────

    def _setup_tray(self) -> None:
        """Create a system-tray icon. No-op if pystray or Pillow isn't installed."""
        try:
            import pystray
            from PIL import Image
        except ImportError:
            return

        png = REPO_DIR / "claude_widget.png"
        if not png.exists():
            return
        try:
            image = Image.open(png)
        except Exception:
            return

        # All tray-thread callbacks marshal back to the UI thread via after().
        def show(icon=None, item=None):
            self._after_safe(0, self._show_and_raise)

        def hide(icon=None, item=None):
            self._after_safe(0, self._hide_window)

        def refresh_now(icon=None, item=None):
            self._after_safe(0, self._manual_refresh)

        def quit_app(icon=None, item=None):
            self._after_safe(0, self._quit_app)

        menu = pystray.Menu(
            pystray.MenuItem("Show", show, default=True),
            pystray.MenuItem("Hide", hide),
            pystray.MenuItem("Refresh now", refresh_now),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        )
        self._tray = pystray.Icon("claude-usage-widget", image,
                                  "Claude Usage", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _after_safe(self, *args, remember_id: bool = False, **kwargs) -> None:
        """`self.after` that silently no-ops if the window has been destroyed.

        If remember_id=True, stores the returned after-id so it can be cancelled
        (used for the next-poll timer so a manual refresh can supersede it).
        """
        if self._destroyed:
            return
        try:
            aid = self.after(*args, **kwargs)
        except tk.TclError:
            return
        if remember_id:
            self._next_after_id = aid

    # ── drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, e: tk.Event) -> None:
        self._drag_x = e.x_root - self.winfo_x()
        self._drag_y = e.y_root - self.winfo_y()

    def _drag_move(self, e: tk.Event) -> None:
        self.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    # ── pin ───────────────────────────────────────────────────────────────────

    def _toggle_topmost(self) -> None:
        self._on_top = not self._on_top
        self.attributes("-topmost", self._on_top)
        self._pin_btn.config(fg="white" if self._on_top else ACCENT)

    # ── refresh loop ──────────────────────────────────────────────────────────

    def _schedule_refresh(self) -> None:
        """Kick off a fetch in a worker thread, dropping any pending timer."""
        if self._destroyed or self._fetch_in_flight:
            return
        self._next_after_id = None
        self._fetch_in_flight = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        next_delay_s = self._refresh_min * 60
        try:
            data = fetch_usage()
            rows = parse_usage(data)
            self._after_safe(0, self._on_fetch_success, rows)
        except Exception as exc:  # noqa: BLE001 — top-level handler for polling loop
            if is_auth_error(exc):
                self._after_safe(0, self._show_login)
            else:
                self._after_safe(0, self._show_error, str(exc))
            backoff = retry_after_seconds(exc)
            if backoff is not None:
                next_delay_s = max(next_delay_s, backoff)
        finally:
            self._fetch_in_flight = False
        self._after_safe(next_delay_s * 1000, self._schedule_refresh,
                         remember_id=True)

    def _on_fetch_success(self, rows: list[Row]) -> None:
        self._render(rows)
        self._ts_lbl.config(text=time.strftime("Updated %H:%M:%S"))

    def _manual_refresh(self) -> None:
        """User-initiated refresh from the settings panel; debounced via in-flight flag."""
        if self._fetch_in_flight:
            return
        if self._next_after_id is not None:
            try:
                self.after_cancel(self._next_after_id)
            except tk.TclError:
                pass
            self._next_after_id = None
        self._schedule_refresh()

    # ── render ────────────────────────────────────────────────────────────────

    def _render(self, rows: list[Row]) -> None:
        self._last_rows = rows
        for w in self._body.winfo_children():
            w.destroy()

        if self._manage_mode:
            # Settings live in a separate popup; main body just shows every row
            # with its hide-checkbox so the user can toggle visibility.
            visible = rows
        else:
            if self._update_state == "available":
                self._add_update_banner()
            elif self._deps_missing:
                self._add_deps_banner()
            visible = [r for r in rows if r[0] not in self._hidden]

        if not visible and not self._manage_mode:
            tk.Label(self._body, text="All rows hidden — click ≡ to show some",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 8),
                     wraplength=WIDTH - 20).pack(anchor="w")
        else:
            for row in visible:
                self._add_row(*row)

        self.update_idletasks()

    def _add_update_banner(self) -> None:
        btn = tk.Label(
            self._body, text="Download Update",
            bg=BTN_BG, fg="white",
            font=("Segoe UI", 8, "bold"),
            cursor="hand2", pady=4,
            bd=0, padx=0,
        )
        btn.pack(fill="x", pady=(0, 6))
        btn.bind("<Button-1>", lambda _e: self._on_install_update())
        btn.bind("<Enter>",   lambda _e: btn.config(bg=BTN_BG_HOVER))
        btn.bind("<Leave>",   lambda _e: btn.config(bg=BTN_BG))

    @staticmethod
    def _has_tray_deps() -> bool:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
            return True
        except ImportError:
            return False

    def _add_deps_banner(self) -> None:
        s = self._deps_install_state
        if s == "installing":
            text, clickable = "Installing…", False
        elif s == "error":
            text, clickable = "Install failed — Click to retry", True
        else:
            text, clickable = "Install missing tray icon", True

        btn = tk.Label(
            self._body, text=text,
            bg=BTN_BG, fg="white",
            font=("Segoe UI", 8, "bold"),
            cursor="hand2" if clickable else "watch",
            pady=4, bd=0, padx=0,
        )
        btn.pack(fill="x", pady=(0, 6))
        if clickable:
            btn.bind("<Button-1>", lambda _e: self._on_install_deps())
            btn.bind("<Enter>",   lambda _e: btn.config(bg=BTN_BG_HOVER))
            btn.bind("<Leave>",   lambda _e: btn.config(bg=BTN_BG))

        if s == "error" and self._deps_install_error:
            tk.Label(self._body, text=self._deps_install_error,
                     bg=BG, fg=BAR_HIGH, font=("Segoe UI", 7),
                     wraplength=WIDTH - 20, justify="left",
                     bd=0, padx=0, pady=0).pack(anchor="w", pady=(0, 4))

    def _on_install_deps(self) -> None:
        if self._deps_install_state == "installing":
            return
        self._deps_install_state = "installing"
        self._deps_install_error = ""
        if self._last_rows:
            self._render(self._last_rows)
        threading.Thread(target=self._do_install_deps, daemon=True).start()

    def _do_install_deps(self) -> None:
        req = REPO_DIR / "requirements.txt"
        py  = Path(sys.executable).with_name("python.exe")
        if not py.exists():
            py = Path(sys.executable)
        try:
            r = subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet", "--user",
                 "-r", str(req)],
                cwd=REPO_DIR,
                capture_output=True, text=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._after_safe(0, self._deps_install_done, str(exc))
            return
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "?").strip()[:200]
            self._after_safe(0, self._deps_install_done, err)
            return
        # Success — relaunch so the new modules can be imported.
        self._after_safe(0, self._do_relaunch)

    def _deps_install_done(self, error: str) -> None:
        self._deps_install_state = "error"
        self._deps_install_error = error
        if self._last_rows:
            self._render(self._last_rows)

    def _populate_settings(self, parent: tk.Misc) -> None:
        panel = tk.Frame(parent, bg=BG)
        panel.pack(fill="x", pady=(0, 4))

        self._update_frame = tk.Frame(panel, bg=BG)
        self._update_frame.pack(fill="x", pady=(0, 8))
        self._render_update_section()

        self._refresh_lbl = tk.Label(
            panel, text=self._fmt_refresh_label(self._refresh_min),
            bg=BG, fg=FG, font=("Segoe UI", 8),
            bd=0, padx=0, pady=0)
        self._refresh_lbl.pack(anchor="w")

        scale = tk.Scale(
            panel, from_=0, to=len(MINUTE_PRESETS) - 1, resolution=1,
            orient="horizontal", showvalue=False,
            bg=BG, fg=FG, troughcolor=BAR_BG, highlightthickness=0,
            activebackground=ACCENT, sliderrelief="flat", borderwidth=0,
            command=lambda v: self._set_refresh_index(int(float(v))))
        scale.set(MINUTE_PRESETS.index(self._refresh_min))
        scale.pack(fill="x")

        # ── boolean options ──
        opts = tk.Frame(panel, bg=BG)
        opts.pack(fill="x", pady=(6, 0))

        self._make_checkbox(
            opts, "Launch on startup",
            get_state=is_startup_enabled,
            set_state=set_startup_enabled,
        )
        self._make_checkbox(
            opts, "Keep minimized on close",
            get_state=lambda: self._settings.get("minimize_on_close", False),
            set_state=self._set_minimize_on_close,
        )
        self._make_checkbox(
            opts, "Re-open widget in same position",
            get_state=lambda: self._settings.get("remember_position", True),
            set_state=self._set_remember_position,
        )

    def _set_minimize_on_close(self, enabled: bool) -> str | None:
        self._settings["minimize_on_close"] = bool(enabled)
        try:
            save_settings(self._settings)
        except OSError as exc:
            return str(exc)
        return None

    def _set_remember_position(self, enabled: bool) -> str | None:
        self._settings["remember_position"] = bool(enabled)
        if enabled:
            # Capture the current position right now so a later restart
            # uses it even if the user never drags the window again.
            self._settings["window_x"] = int(self.winfo_x())
            self._settings["window_y"] = int(self.winfo_y())
        try:
            save_settings(self._settings)
        except OSError as exc:
            return str(exc)
        return None

    def _make_checkbox(self, parent: tk.Misc, label: str,
                        get_state, set_state) -> tk.Frame:
        """Settings-row checkbox styled to match the row hide checkboxes.

        get_state: () -> bool
        set_state: (new_value: bool) -> str | None   (returns error or None)
        """
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(2, 0))

        state = {"on": bool(get_state())}

        box = tk.Label(row, text="☑" if state["on"] else "☐",
                       bg=BG, fg=FG, font=("Segoe UI", 9),
                       cursor="hand2", bd=0, padx=0, pady=0)
        box.pack(side="left", padx=(0, 4))

        lbl = tk.Label(row, text=label, bg=BG, fg=FG,
                       font=("Segoe UI", 8), cursor="hand2",
                       bd=0, padx=0, pady=0)
        lbl.pack(side="left")

        err_lbl: list[tk.Label] = []

        def toggle(_e=None):
            new = not state["on"]
            err = set_state(new)
            if err:
                if not err_lbl:
                    e = tk.Label(row, text=err, bg=BG, fg=BAR_HIGH,
                                 font=("Segoe UI", 7),
                                 wraplength=WIDTH - 30, justify="left",
                                 bd=0, padx=0, pady=0)
                    e.pack(anchor="w")
                    err_lbl.append(e)
                else:
                    err_lbl[0].config(text=err)
                return
            state["on"] = new
            box.config(text="☑" if new else "☐")
            if err_lbl:
                err_lbl[0].destroy()
                err_lbl.clear()

        box.bind("<Button-1>", toggle)
        lbl.bind("<Button-1>", toggle)
        return row

    def _make_action_button(self, parent: tk.Misc, text: str, cb) -> tk.Label:
        btn = tk.Label(parent, text=text,
                       bg=BTN_BG, fg=FG, font=("Segoe UI", 8),
                       cursor="hand2", pady=3,
                       bd=0, padx=0)
        btn.bind("<Button-1>", lambda _e: cb())
        btn.bind("<Enter>",   lambda _e: btn.config(bg=BTN_BG_HOVER))
        btn.bind("<Leave>",   lambda _e: btn.config(bg=BTN_BG))
        return btn

    def _make_refresh_button(self, parent: tk.Misc) -> tk.Label:
        return self._make_action_button(parent, "↻  Refresh", self._manual_refresh)

    def _toggle_manage(self) -> None:
        self._manage_mode = not self._manage_mode
        self._gear_btn.config(fg="white" if self._manage_mode else ACCENT)
        if self._manage_mode:
            self._open_settings_window()
        else:
            self._close_settings_window()
        if self._last_rows:
            self._render(self._last_rows)

    def _open_settings_window(self) -> None:
        """Pop a separate Toplevel with the settings UI, clamped on-screen."""
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return

        pop = tk.Toplevel(self)
        pop.overrideredirect(True)
        pop.configure(bg=BG)
        pop.attributes("-topmost", True)
        pop.attributes("-alpha", 0.93)

        hdr = tk.Frame(pop, bg=BG, cursor="fleur")
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        title = tk.Label(hdr, text="Settings", bg=BG, fg=FG,
                         font=("Segoe UI", 9, "bold"), cursor="fleur")
        title.pack(side="left")
        close = self._make_icon_btn(hdr, "✕", self._toggle_manage, size=11)
        close.pack(side="right", padx=(0, 4))

        drag = {"x": 0, "y": 0}
        def _ds(e: tk.Event) -> None:
            drag["x"] = e.x_root - pop.winfo_x()
            drag["y"] = e.y_root - pop.winfo_y()
        def _dm(e: tk.Event) -> None:
            pop.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")
        for w in (hdr, title):
            w.bind("<ButtonPress-1>", _ds)
            w.bind("<B1-Motion>", _dm)

        tk.Frame(pop, bg=SEPARATOR, height=1).pack(fill="x", padx=6, pady=(0, 4))

        body = tk.Frame(pop, bg=BG)
        body.pack(fill="x", padx=8, pady=(0, 8))

        self._settings_win = pop
        self._populate_settings(body)

        pop.update_idletasks()
        pop.geometry(f"{WIDTH}x{pop.winfo_reqheight()}")
        pop.update_idletasks()
        self._position_settings_window()

    def _position_settings_window(self) -> None:
        """Place popup adjacent to widget; fall back through 4 sides, then clamp."""
        pop = self._settings_win
        if pop is None or not pop.winfo_exists():
            return
        pop.update_idletasks()
        pw = pop.winfo_width()  or pop.winfo_reqwidth()
        ph = pop.winfo_height() or pop.winfo_reqheight()
        sw = pop.winfo_screenwidth()
        sh = pop.winfo_screenheight()
        wx, wy = self.winfo_x(), self.winfo_y()
        ww, wh = self.winfo_width(), self.winfo_height()
        gap = 4

        candidates = [
            (wx - pw - gap, wy),           # left of widget (widget is usually bottom-right)
            (wx + ww + gap, wy),           # right of widget
            (wx, wy - ph - gap),           # above widget
            (wx, wy + wh + gap),           # below widget
        ]
        for x, y in candidates:
            if x >= 0 and y >= 0 and x + pw <= sw and y + ph <= sh:
                pop.geometry(f"+{x}+{y}")
                return
        # No side fit cleanly — clamp the left-of placement.
        x, y = candidates[0]
        x = max(0, min(x, sw - pw))
        y = max(0, min(y, sh - ph))
        pop.geometry(f"+{x}+{y}")

    def _close_settings_window(self) -> None:
        pop = self._settings_win
        self._settings_win = None
        if pop is not None:
            try:
                pop.destroy()
            except tk.TclError:
                pass

    def _toggle_hidden(self, label: str) -> None:
        self._hidden ^= {label}
        self._settings["hidden"] = sorted(self._hidden)
        save_settings(self._settings)
        if self._last_rows:
            self._render(self._last_rows)

    def _init_refresh_minutes(self) -> int:
        """Load refresh interval (minutes), migrating legacy seconds key if present."""
        raw = self._settings.get("refresh_minutes")
        if raw is None and "refresh_seconds" in self._settings:
            try:
                raw = max(1, round(int(self._settings["refresh_seconds"]) / 60))
            except (TypeError, ValueError):
                raw = REFRESH_DEFAULT_MIN
            del self._settings["refresh_seconds"]
        minutes = self._snap_to_preset(raw if raw is not None else REFRESH_DEFAULT_MIN)
        if self._settings.get("refresh_minutes") != minutes:
            self._settings["refresh_minutes"] = minutes
            save_settings(self._settings)
        return minutes

    @staticmethod
    def _snap_to_preset(value: object) -> int:
        """Round any number to the nearest MINUTE_PRESETS entry."""
        try:
            n = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return REFRESH_DEFAULT_MIN
        return min(MINUTE_PRESETS, key=lambda m: abs(m - n))

    def _set_refresh_index(self, idx: int) -> None:
        if not 0 <= idx < len(MINUTE_PRESETS):
            return
        minutes = MINUTE_PRESETS[idx]
        if minutes == self._refresh_min:
            return
        self._refresh_min = minutes
        self._settings["refresh_minutes"] = minutes
        save_settings(self._settings)
        if hasattr(self, "_refresh_lbl"):
            try:
                self._refresh_lbl.config(text=self._fmt_refresh_label(minutes))
            except tk.TclError:
                pass

    # ── self-update UI ──

    def _render_update_section(self) -> None:
        frame = self._update_frame
        if frame is None or not frame.winfo_exists():
            return
        for w in frame.winfo_children():
            w.destroy()

        tk.Label(frame, text=f"Version: {self._version}",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 7),
                 bd=0, padx=0, pady=0).pack(anchor="w")

        state, detail = self._update_state, self._update_detail

        # Button: label and action change based on state. Always present.
        if state in ("checking", "installing"):
            label  = "Checking…" if state == "checking" else "Installing…"
            action = None
        elif state == "available":
            label  = "Download Update"
            action = self._on_install_update
        else:  # idle / current / error
            label  = "Check for updates"
            action = self._on_check_update

        btn = self._make_action_button(frame, label, action or (lambda: None))
        if action is None:
            # Disabled-looking button while a worker is running.
            btn.config(fg=FG_DIM, cursor="arrow")
            btn.unbind("<Button-1>")
            btn.unbind("<Enter>")
            btn.unbind("<Leave>")
        btn.pack(fill="x", pady=(2, 0))

        # Status text below the button.
        status_text, status_fg, status_font_size = "", FG_DIM, 8
        if state == "current":
            status_text, status_fg = "✓ Up to date", BAR_LOW
        elif state == "error":
            status_text, status_fg, status_font_size = detail, BAR_HIGH, 7

        if status_text:
            tk.Label(frame, text=status_text, bg=BG, fg=status_fg,
                     font=("Segoe UI", status_font_size),
                     wraplength=WIDTH - 20, justify="left",
                     bd=0, padx=0, pady=0).pack(anchor="w", pady=(2, 0))

    def _set_update_state(self, state: str, detail: str = "") -> None:
        self._update_state  = state
        self._update_detail = detail
        self._render_update_section()

    def _auto_check_for_update(self) -> None:
        """Background update check fired once at startup; silent unless an update is found."""
        if self._update_state != "idle":
            return
        threading.Thread(target=self._do_auto_check, daemon=True).start()

    def _do_auto_check(self) -> None:
        status, detail = check_for_update()
        if status != "available":
            return   # stay silent on error/up-to-date
        def apply():
            self._update_state  = "available"
            self._update_detail = detail
            self._render_update_section()    # in case settings panel is open
            if self._last_rows:               # re-render body so banner shows
                self._render(self._last_rows)
        self._after_safe(0, apply)

    def _on_check_update(self) -> None:
        self._set_update_state("checking")
        threading.Thread(target=self._do_check_update, daemon=True).start()

    def _do_check_update(self) -> None:
        status, detail = check_for_update()
        self._after_safe(0, self._set_update_state, status, detail)

    def _on_install_update(self) -> None:
        self._set_update_state("installing")
        threading.Thread(target=self._do_install_update, daemon=True).start()

    def _do_install_update(self) -> None:
        err = install_update()
        if err:
            self._after_safe(0, self._set_update_state, "error", err)
            return
        # Pull succeeded — relaunch so the new code takes effect.
        self._after_safe(0, self._do_relaunch)

    def _do_relaunch(self) -> None:
        self._destroyed = True
        self._close_settings_window()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        try:
            self.destroy()
        except tk.TclError:
            pass
        relaunch_widget()

    @staticmethod
    def _fmt_refresh_label(minutes: int) -> str:
        if minutes >= 60:
            hours = minutes // 60
            return f"Refresh timer: {hours} hr" if hours == 1 else f"Refresh timer: {hours} hrs"
        unit = "min" if minutes == 1 else "min"
        return f"Refresh timer: {minutes} {unit}"

    def _add_row(self, label: str, util: float, sub: str,
                 time_progress: float | None) -> None:
        pct        = min(int(util * 100), 100)
        hidden     = label in self._hidden
        active_fg  = FG_DIM if hidden else FG
        active_bar = FG_DIM if hidden else bar_color(util)
        time_fg    = FG_DIM if hidden else TIME_BAR_FG

        row = tk.Frame(self._body, bg=BG)
        row.pack(fill="x", pady=(4, 0))

        top = tk.Frame(row, bg=BG)
        top.pack(fill="x")

        if self._manage_mode:
            box = tk.Label(top, text="☐" if hidden else "☑", bg=BG, fg=active_fg,
                           font=("Segoe UI", 9), cursor="hand2",
                           bd=0, padx=0, pady=0)
            box.pack(side="left", padx=(0, 4))
            box.bind("<Button-1>", lambda _e, l=label: self._toggle_hidden(l))

        tk.Label(top, text=label, bg=BG, fg=active_fg,
                 font=("Segoe UI", 8), bd=0, padx=0, pady=0).pack(side="left")
        tk.Label(top, text=f"{pct}%", bg=BG, fg=active_bar,
                 font=("Segoe UI", 8, "bold"),
                 bd=0, padx=0, pady=0).pack(side="right")

        # Usage bar
        usage_bar = tk.Frame(row, bg=BAR_BG, height=BAR_H)
        usage_bar.pack(fill="x", pady=(2, 0))
        usage_bar.pack_propagate(False)
        tk.Frame(usage_bar, bg=active_bar, height=BAR_H).place(
            relx=0, rely=0, relwidth=min(max(util, 0.0), 1.0), relheight=1.0)

        # Time-elapsed bar (flush under the usage bar, no spacing)
        if time_progress is not None:
            time_bar = tk.Frame(row, bg=TIME_BAR_BG, height=TIME_BAR_H)
            time_bar.pack(fill="x", pady=0)
            time_bar.pack_propagate(False)
            tk.Frame(time_bar, bg=time_fg, height=TIME_BAR_H).place(
                relx=0, rely=0,
                relwidth=min(max(time_progress, 0.0), 1.0),
                relheight=1.0)

        if sub:
            tk.Label(row, text=sub, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 7),
                     bd=0, padx=0, pady=0).pack(anchor="w")

    def _reset_body(self) -> None:
        for w in self._body.winfo_children():
            w.destroy()

    def _show_login(self) -> None:
        self._reset_body()
        tk.Label(self._body, text="Not logged in", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w")
        tk.Label(self._body, text="Run 'claude' in a terminal\nto refresh credentials.",
                 bg=BG, fg=FG, font=("Segoe UI", 8), justify="left",
                 wraplength=WIDTH - 20).pack(anchor="w", pady=(4, 0))
        self._ts_lbl.config(text="")

    def _show_error(self, msg: str) -> None:
        self._reset_body()
        tk.Label(self._body, text=f"Error: {msg}", bg=BG, fg=BAR_HIGH,
                 font=("Segoe UI", 7), wraplength=WIDTH - 20,
                 justify="left").pack(anchor="w")
        self._ts_lbl.config(text=time.strftime("Failed %H:%M:%S"))


def main() -> None:
    UsageWidget().mainloop()


if __name__ == "__main__":
    main()
