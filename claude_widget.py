"""Claude Usage Widget — floating always-on-top window showing Claude.ai plan usage.

Reads OAuth tokens from ~/.claude/.credentials.json (created by the Claude Code
CLI), polls https://api.anthropic.com/api/oauth/usage every 30 seconds, and
auto-refreshes the access token when it's near expiry.
"""

from __future__ import annotations

import json
import os
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


def retry_after_seconds(exc: BaseException) -> int | None:
    """If exc is a 429 with a Retry-After header, return that delay in seconds."""
    if not (isinstance(exc, HTTPError) and exc.response is not None
            and exc.response.status_code == 429):
        return None
    raw = exc.response.headers.get("Retry-After", "").strip()
    if raw.isdigit():
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


def parse_usage(data: dict) -> list[tuple[str, float, str]]:
    """Convert the API response into [(label, utilization 0..1, sublabel), ...]."""
    rows: list[tuple[str, float, str]] = []

    for key, label in TIER_LABELS.items():
        tier = data.get(key)
        if not tier or tier.get("utilization") is None:
            continue
        util      = float(tier["utilization"]) / 100.0
        countdown = _fmt_countdown(tier.get("resets_at") or "")
        rows.append((label, util, countdown))

    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used  = float(extra.get("used_credits") or 0)
        limit = float(extra.get("monthly_limit") or 0)
        util  = used / limit if limit > 0 else 0.0
        cur   = extra.get("currency", "USD")
        rows.append(("Extra credits", util, f"{cur} {used:.2f} / {limit:.2f}"))

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
WIDTH        = 220

REFRESH_MIN_S     = 5
REFRESH_MAX_S     = 600
REFRESH_DEFAULT_S = 30
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
        self._hidden: set[str] = set(self._settings.get("hidden", []))
        self._refresh_s   = self._clamp_refresh(
            self._settings.get("refresh_seconds", REFRESH_DEFAULT_S))
        self._manage_mode = False
        self._last_rows: list[tuple[str, float, str]] = []
        self._destroyed   = False

        self._build_ui()
        self.update_idletasks()
        self._place_bottom_right()
        self.lift()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_refresh()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        hdr = tk.Frame(self, bg=BG, cursor="fleur")
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        self._bind_drag(hdr)

        title = tk.Label(hdr, text="Claude Usage", bg=BG, fg=FG,
                         font=("Segoe UI", 9, "bold"), cursor="fleur")
        title.pack(side="left")
        self._bind_drag(title)

        # Header buttons (right-to-left order in the layout)
        close = self._make_icon_btn(hdr, "✕", self._on_close)
        close.pack(side="right", padx=(0, 4))

        self._gear_btn = self._make_icon_btn(hdr, "⚙", self._toggle_manage, size=10)
        self._gear_btn.pack(side="right", padx=(0, 4))

        self._pin_btn = self._make_icon_btn(hdr, "📌", self._toggle_topmost)
        self._pin_btn.pack(side="right", padx=(0, 2))

        tk.Frame(self, bg=SEPARATOR, height=1).pack(fill="x", padx=6, pady=(0, 4))

        self._body = tk.Frame(self, bg=BG)
        self._body.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(self._body, text="Loading…", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w")

        self._ts_lbl = tk.Label(self, bg=BG, fg=FG_DIM, font=("Segoe UI", 7))
        self._ts_lbl.pack(anchor="e", padx=8, pady=(0, 4))

    def _make_icon_btn(self, parent: tk.Misc, text: str, cb, size: int = 9) -> tk.Label:
        lbl = tk.Label(parent, text=text, bg=BG, fg=ACCENT,
                       font=("Segoe UI", size), cursor="hand2")
        lbl.bind("<Button-1>", lambda _e: cb())
        return lbl

    def _bind_drag(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>",     self._drag_move)

    def _place_bottom_right(self) -> None:
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{sw - WIDTH - 20}+{sh - 220}")

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        self._destroyed = True
        self.destroy()

    def _after_safe(self, *args, **kwargs) -> None:
        """`self.after` that silently no-ops if the window has been destroyed."""
        if self._destroyed:
            return
        try:
            self.after(*args, **kwargs)
        except tk.TclError:
            pass

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
        self._pin_btn.config(fg=ACCENT if self._on_top else ACCENT_OFF)

    # ── refresh loop ──────────────────────────────────────────────────────────

    def _schedule_refresh(self) -> None:
        if self._destroyed:
            return
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        next_delay_s = self._refresh_s
        try:
            data = fetch_usage()
            rows = parse_usage(data)
            self._after_safe(0, self._render, rows)
        except Exception as exc:  # noqa: BLE001 — top-level handler for polling loop
            if is_auth_error(exc):
                self._after_safe(0, self._show_login)
            else:
                self._after_safe(0, self._show_error, str(exc))
            backoff = retry_after_seconds(exc)
            if backoff is not None:
                next_delay_s = max(next_delay_s, backoff)
        self._after_safe(next_delay_s * 1000, self._schedule_refresh)

    # ── render ────────────────────────────────────────────────────────────────

    def _render(self, rows: list[tuple[str, float, str]]) -> None:
        self._last_rows = rows
        for w in self._body.winfo_children():
            w.destroy()

        if self._manage_mode:
            self._add_settings_panel()
            visible = rows
        else:
            visible = [r for r in rows if r[0] not in self._hidden]

        if not visible and not self._manage_mode:
            tk.Label(self._body, text="All rows hidden — click ⚙ to show some",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 8),
                     wraplength=WIDTH - 20).pack(anchor="w")
        else:
            for row in visible:
                self._add_row(*row)

        self._ts_lbl.config(text=time.strftime("Updated %H:%M:%S"))
        self.update_idletasks()

    def _add_settings_panel(self) -> None:
        panel = tk.Frame(self._body, bg=BG)
        panel.pack(fill="x", pady=(0, 4))

        self._refresh_lbl = tk.Label(
            panel, text=self._fmt_refresh_label(self._refresh_s),
            bg=BG, fg=FG, font=("Segoe UI", 8))
        self._refresh_lbl.pack(anchor="w")

        scale = tk.Scale(
            panel, from_=REFRESH_MIN_S, to=REFRESH_MAX_S, resolution=5,
            orient="horizontal", showvalue=False,
            bg=BG, fg=FG, troughcolor=BAR_BG, highlightthickness=0,
            activebackground=ACCENT, sliderrelief="flat", borderwidth=0,
            command=lambda v: self._set_refresh(int(float(v))))
        scale.set(self._refresh_s)
        scale.pack(fill="x")

        tk.Frame(self._body, bg=SEPARATOR, height=1).pack(fill="x", pady=(0, 4))

    def _toggle_manage(self) -> None:
        self._manage_mode = not self._manage_mode
        self._gear_btn.config(fg="white" if self._manage_mode else ACCENT)
        if self._last_rows:
            self._render(self._last_rows)

    def _toggle_hidden(self, label: str) -> None:
        self._hidden ^= {label}
        self._settings["hidden"] = sorted(self._hidden)
        save_settings(self._settings)
        if self._last_rows:
            self._render(self._last_rows)

    @staticmethod
    def _clamp_refresh(value: object) -> int:
        try:
            n = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return REFRESH_DEFAULT_S
        return max(REFRESH_MIN_S, min(REFRESH_MAX_S, n))

    def _set_refresh(self, seconds: int) -> None:
        seconds = self._clamp_refresh(seconds)
        if seconds == self._refresh_s:
            return
        self._refresh_s = seconds
        self._settings["refresh_seconds"] = seconds
        save_settings(self._settings)
        if hasattr(self, "_refresh_lbl"):
            self._refresh_lbl.config(text=self._fmt_refresh_label(seconds))

    @staticmethod
    def _fmt_refresh_label(seconds: int) -> str:
        if seconds < 60:
            return f"Refresh: {seconds}s"
        m, s = divmod(seconds, 60)
        return f"Refresh: {m}m" if s == 0 else f"Refresh: {m}m {s}s"

    def _add_row(self, label: str, util: float, sub: str) -> None:
        pct        = min(int(util * 100), 100)
        hidden     = label in self._hidden
        active_fg  = FG_DIM if hidden else FG
        active_bar = FG_DIM if hidden else bar_color(util)

        row = tk.Frame(self._body, bg=BG)
        row.pack(fill="x", pady=(4, 0))

        top = tk.Frame(row, bg=BG)
        top.pack(fill="x")

        if self._manage_mode:
            box = tk.Label(top, text="☐" if hidden else "☑", bg=BG, fg=active_fg,
                           font=("Segoe UI", 9), cursor="hand2")
            box.pack(side="left", padx=(0, 4))
            box.bind("<Button-1>", lambda _e, l=label: self._toggle_hidden(l))

        tk.Label(top, text=label, bg=BG, fg=active_fg,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Label(top, text=f"{pct}%", bg=BG, fg=active_bar,
                 font=("Segoe UI", 8, "bold")).pack(side="right")

        bar_frame = tk.Frame(row, bg=BAR_BG, height=5)
        bar_frame.pack(fill="x", pady=(2, 0))
        bar_frame.pack_propagate(False)
        tk.Frame(bar_frame, bg=active_bar, height=5).place(
            relx=0, rely=0, relwidth=min(max(util, 0.0), 1.0), relheight=1.0)

        if sub:
            tk.Label(row, text=sub, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 7)).pack(anchor="w")

    def _reset_body(self) -> None:
        """Clear body and re-add the settings panel if we're in manage mode."""
        for w in self._body.winfo_children():
            w.destroy()
        if self._manage_mode:
            self._add_settings_panel()

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
