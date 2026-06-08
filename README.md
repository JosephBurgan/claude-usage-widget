# Claude Usage Widget

A small always-on-top floating window for Windows that shows your Claude.ai plan usage (5-hour, 7-day, Sonnet-only, extra credits) with live progress bars and reset countdowns. Refreshes every 30 seconds.

![widget](docs/screenshot.png)

## Features

- Always-on-top floating window — drag by the title bar to position
- Live progress bars colored by usage level (green / orange / red)
- Countdown to each limit's reset
- Click the pin (📌) to toggle always-on-top
- Click the gear (⚙) to enter manage mode — hide rows you don't care about
- Hidden-row preferences persist across restarts

## Requirements

- Windows 10 or 11
- Python 3.10+ ([install from python.org](https://www.python.org/downloads/windows/) — make sure "Add to PATH" is checked)
- [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) authenticated at least once
  (the widget reads tokens from `~/.claude/.credentials.json`)

## Install

```powershell
git clone https://github.com/JosephBurgan/claude-usage-widget.git
cd claude-usage-widget
pip install -r requirements.txt
```

Make sure you've run `claude` at least once and logged in — the widget needs the
credentials file that Claude Code creates at `%USERPROFILE%\.claude\.credentials.json`.

## Run

Double-click `claude_widget.vbs`. The widget appears in the bottom-right of your screen.

(The `.vbs` launcher runs the widget with `pythonw.exe` so no console window appears.
You can also run `python claude_widget.py` directly if you prefer.)

## Start with Windows

Place a shortcut to `claude_widget.vbs` in your Startup folder:

1. Press <kbd>Win</kbd>+<kbd>R</kbd>, type `shell:startup`, hit Enter
2. Right-click in that folder → New → Shortcut
3. Target: `wscript.exe "C:\path\to\claude-usage-widget\claude_widget.vbs"`

Or run this PowerShell snippet to create both a Start Menu and Startup shortcut:

```powershell
$repo  = "C:\path\to\claude-usage-widget"
$sh    = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Startup"),
                   "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")) {
  $lnk = $sh.CreateShortcut("$dir\Claude Usage Widget.lnk")
  $lnk.TargetPath       = "wscript.exe"
  $lnk.Arguments        = "`"$repo\claude_widget.vbs`""
  $lnk.WorkingDirectory = $repo
  $lnk.Save()
}
```

## How it works

- Reads the OAuth access + refresh tokens from `~/.claude/.credentials.json`
- Refreshes the access token automatically via `POST https://api.anthropic.com/v1/oauth/token`
  when it's close to expiring
- Polls `GET https://api.anthropic.com/api/oauth/usage` every 30 s with
  `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20`
- Writes refreshed tokens back to the credentials file

If the refresh token ever gets invalidated (e.g. you logged out elsewhere), the widget
will show "Not logged in" — run `claude` once, send any message, and the tokens will
be refreshed.

Per-window hidden-row preferences are stored at
`%USERPROFILE%\.claude_widget_settings.json`.

## License

MIT
