"""
Claude Usage Widget — minimal always-on-top floating window.
Reads ~/.claude/.credentials.json, refreshes OAuth token as needed,
calls api.anthropic.com/api/oauth/usage every 30 seconds.
"""

import json
import os
import threading
import time
import tkinter as tk

import requests

# ── Credentials ──────────────────────────────────────────────────────────────

CREDS_FILE    = os.path.join(os.environ["USERPROFILE"], ".claude", ".credentials.json")
SETTINGS_FILE = os.path.join(os.environ["USERPROFILE"], ".claude_widget_settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_settings(s: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
API_HDRS  = {"anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}


def _load_creds() -> dict:
    with open(CREDS_FILE, encoding="utf-8") as f:
        return json.load(f)["claudeAiOauth"]


def _save_creds(oauth: dict):
    with open(CREDS_FILE, encoding="utf-8") as f:
        root = json.load(f)
    root["claudeAiOauth"].update(oauth)
    with open(CREDS_FILE, "w", encoding="utf-8") as f:
        json.dump(root, f, indent=2)


def get_fresh_token() -> str:
    """Return a valid access token, refreshing if expired."""
    creds = _load_creds()
    # Consider expired if within 5 minutes of expiry (ms timestamp)
    if creds["expiresAt"] - time.time() * 1000 > 300_000:
        return creds["accessToken"]

    r = requests.post(TOKEN_URL,
        json={
            "grant_type":    "refresh_token",
            "client_id":     CLIENT_ID,
            "refresh_token": creds["refreshToken"],
            "scope":         " ".join(creds.get("scopes", [])),
        },
        headers={"Content-Type": "application/json", **API_HDRS},
        timeout=10)
    r.raise_for_status()
    data = r.json()

    new_expiry = int(time.time() * 1000) + data["expires_in"] * 1000
    _save_creds({
        "accessToken":  data["access_token"],
        "refreshToken": data.get("refresh_token", creds["refreshToken"]),
        "expiresAt":    new_expiry,
    })
    return data["access_token"]


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_usage() -> dict:
    token = get_fresh_token()
    r = requests.get(USAGE_URL,
        headers={"Authorization": f"Bearer {token}", **API_HDRS},
        timeout=10)
    r.raise_for_status()
    return r.json()


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
        if diff <= 0:
            return "resetting…"
        h, rem = divmod(diff, 3600)
        m = rem // 60
        return f"resets in {h}h {m}m" if h else f"resets in {m}m"
    except Exception:
        return ""


def parse_usage(data: dict) -> list[tuple[str, float, str]]:
    rows = []
    for key, label in TIER_LABELS.items():
        tier = data.get(key)
        if not tier:
            continue
        util = tier.get("utilization")
        if util is None:
            continue
        resets_at = tier.get("resets_at") or ""
        rows.append((label, float(util) / 100.0, _fmt_countdown(resets_at)))

    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used  = extra.get("used_credits") or 0
        limit = extra.get("monthly_limit") or 1
        util  = used / limit if limit else 0
        cur   = extra.get("currency", "USD")
        rows.append(("Extra credits", util, f"{cur} {used:.2f} / {limit:.2f}"))

    return rows


# ── Widget ────────────────────────────────────────────────────────────────────

BG         = "#1a1a1a"
FG         = "#e0e0e0"
FG_DIM     = "#888888"
BAR_BG     = "#333333"
BAR_LOW    = "#4caf50"
BAR_MED    = "#ff9800"
BAR_HIGH   = "#f44336"
ACCENT     = "#7c7c7c"
WIDTH      = 220
REFRESH_MS = 30_000


def bar_color(util: float) -> str:
    if util >= 0.9:  return BAR_HIGH
    if util >= 0.6:  return BAR_MED
    return BAR_LOW


class UsageWidget(tk.Tk):
    def __init__(self):
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
        self._hidden      = set(self._settings.get("hidden", []))
        self._manage_mode = False
        self._last_rows: list = []

        self._build_ui()
        self.update_idletasks()
        self._place_bottom_right()
        self.lift()
        self._schedule_refresh()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=BG, cursor="fleur")
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        hdr.bind("<ButtonPress-1>", self._drag_start)
        hdr.bind("<B1-Motion>",     self._drag_move)

        title = tk.Label(hdr, text="Claude Usage", bg=BG, fg=FG,
                         font=("Segoe UI", 9, "bold"), cursor="fleur")
        title.pack(side="left")
        title.bind("<ButtonPress-1>", self._drag_start)
        title.bind("<B1-Motion>",     self._drag_move)

        self._pin_btn = tk.Label(hdr, text="📌", bg=BG, fg=ACCENT,
                                 font=("Segoe UI", 9), cursor="hand2")
        self._pin_btn.pack(side="right", padx=(0, 2))
        self._pin_btn.bind("<Button-1>", self._toggle_topmost)

        self._gear_btn = tk.Label(hdr, text="⚙", bg=BG, fg=ACCENT,
                                  font=("Segoe UI", 10), cursor="hand2")
        self._gear_btn.pack(side="right", padx=(0, 4))
        self._gear_btn.bind("<Button-1>", self._toggle_manage)

        close = tk.Label(hdr, text="✕", bg=BG, fg=ACCENT,
                         font=("Segoe UI", 9), cursor="hand2")
        close.pack(side="right", padx=(0, 4))
        close.bind("<Button-1>", lambda _: self.destroy())

        tk.Frame(self, bg="#333333", height=1).pack(fill="x", padx=6, pady=(0, 4))

        self._body = tk.Frame(self, bg=BG)
        self._body.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(self._body, text="Loading…", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w")

        self._ts_lbl = tk.Label(self, bg=BG, fg=FG_DIM, font=("Segoe UI", 7))
        self._ts_lbl.pack(anchor="e", padx=8, pady=(0, 4))

    def _place_bottom_right(self):
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{sw - WIDTH - 20}+{sh - 220}")

    # ── drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, e):
        self._drag_x = e.x_root - self.winfo_x()
        self._drag_y = e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    # ── pin ───────────────────────────────────────────────────────────────────

    def _toggle_topmost(self, _=None):
        self._on_top = not self._on_top
        self.attributes("-topmost", self._on_top)
        self._pin_btn.config(fg=ACCENT if self._on_top else "#444444")

    # ── refresh ───────────────────────────────────────────────────────────────

    def _schedule_refresh(self):
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        try:
            data = fetch_usage()
            rows = parse_usage(data)
            self.after(0, self._render, rows)
        except FileNotFoundError:
            self.after(0, self._show_login)
        except Exception as exc:
            msg = str(exc)
            if "invalid_grant" in msg or "401" in msg or "400" in msg:
                self.after(0, self._show_login)
            else:
                self.after(0, self._show_error, msg)
        self.after(REFRESH_MS, self._schedule_refresh)

    # ── render ────────────────────────────────────────────────────────────────

    def _render(self, rows: list):
        self._last_rows = rows
        for w in self._body.winfo_children():
            w.destroy()

        visible = rows if self._manage_mode else [r for r in rows if r[0] not in self._hidden]

        if not visible and not self._manage_mode:
            tk.Label(self._body, text="All rows hidden — click ⚙ to show some",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 8),
                     wraplength=WIDTH - 20).pack(anchor="w")
        else:
            for label, util, sub in visible:
                self._add_row(label, util, sub)

        self._ts_lbl.config(text=time.strftime("Updated %H:%M:%S"))
        self.update_idletasks()

    def _toggle_manage(self, _=None):
        self._manage_mode = not self._manage_mode
        self._gear_btn.config(fg="white" if self._manage_mode else ACCENT)
        if self._last_rows:
            self._render(self._last_rows)

    def _toggle_hidden(self, label: str):
        if label in self._hidden:
            self._hidden.remove(label)
        else:
            self._hidden.add(label)
        self._settings["hidden"] = sorted(self._hidden)
        save_settings(self._settings)
        if self._last_rows:
            self._render(self._last_rows)

    def _add_row(self, label: str, util: float, sub: str):
        pct    = min(int(util * 100), 100)
        color  = bar_color(util)
        hidden = label in self._hidden

        row = tk.Frame(self._body, bg=BG)
        row.pack(fill="x", pady=(4, 0))

        top = tk.Frame(row, bg=BG)
        top.pack(fill="x")

        if self._manage_mode:
            box_char = "☑" if not hidden else "☐"
            box = tk.Label(top, text=box_char, bg=BG,
                           fg=FG if not hidden else FG_DIM,
                           font=("Segoe UI", 9), cursor="hand2")
            box.pack(side="left", padx=(0, 4))
            box.bind("<Button-1>", lambda _e, l=label: self._toggle_hidden(l))

        text_color = FG if not hidden else FG_DIM
        tk.Label(top, text=label, bg=BG, fg=text_color,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Label(top, text=f"{pct}%", bg=BG,
                 fg=color if not hidden else FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="right")

        bar_frame = tk.Frame(row, bg=BAR_BG, height=5)
        bar_frame.pack(fill="x", pady=(2, 0))
        bar_frame.pack_propagate(False)
        tk.Frame(bar_frame, bg=color if not hidden else FG_DIM, height=5).place(
            relx=0, rely=0, relwidth=min(util, 1.0), relheight=1.0)

        if sub:
            tk.Label(row, text=sub, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 7)).pack(anchor="w")

    def _show_login(self):
        for w in self._body.winfo_children():
            w.destroy()
        tk.Label(self._body, text="Not logged in", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w")
        tk.Label(self._body, text="Run 'claude' in a terminal\nto refresh credentials.",
                 bg=BG, fg=FG, font=("Segoe UI", 8), justify="left",
                 wraplength=WIDTH - 20).pack(anchor="w", pady=(4, 0))
        self._ts_lbl.config(text="")

    def _show_error(self, msg: str):
        for w in self._body.winfo_children():
            w.destroy()
        tk.Label(self._body, text=f"Error: {msg}", bg=BG, fg=BAR_HIGH,
                 font=("Segoe UI", 7), wraplength=WIDTH - 20,
                 justify="left").pack(anchor="w")
        self._ts_lbl.config(text=time.strftime("Failed %H:%M:%S"))


if __name__ == "__main__":
    app = UsageWidget()
    app.mainloop()
