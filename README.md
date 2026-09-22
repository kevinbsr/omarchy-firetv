# Fire TV Now Playing

This Omarchy bar widget reads the foreground app and active Android media
session from a Fire TV Stick using ADB. It shows the
current title when the app provides one, or the app name otherwise. The bar
shows a compact TV icon; clicking it opens a panel with the app icon, title,
playback and connection states, elapsed time, controls, and a refresh action.
It refreshes every 15 seconds and shows when the last check succeeded.

The compact panel puts a remote grid beside the installed app shortcuts.
Previous, Play/Pause, and Next form its top row while media is active; the row
disappears when no media is active. The grid also includes Select, Back, and
Home. Rewind and Forward occupy the next row's corners when the active app
advertises those media actions; their
step size is decided by that app. Artist and album appear when the media
session reports them. If the session exposes a usable HTTP or file artwork
URI, the panel displays it in place of the app icon. Fire OS often exposes
only a title through ADB, so the app icon remains the normal fallback.
On this stick, Prime Video reports the placeholder title `PrimeVideo` during
playback; the widget shows the app name until Prime exposes a real title.

Title-change desktop alerts are on by default and can be muted in the panel.
The Private toggle hides titles from the bar tooltip and suppresses alerts;
the title remains visible when the panel is open. Both switches persist in
Omarchy shell settings. Alerts do not fire for the first title seen when the
shell starts, so restarting the shell does not produce an old alert.

Installed streaming apps appear as shortcuts in the panel. Selecting one
opens it on the stick, which can interrupt current playback. The text field
sends basic Latin letters, digits, spaces, and common punctuation to the
currently focused TV text field (up to 100 characters). Focus a search box on
the TV first; the widget does not choose a field automatically.

The footer shows the latest ADB check time and full check duration. Reconnect
restarts this computer's ADB connection to the stick. The optional Timeline
records title changes only after it is enabled. It stores up to 100 entries
in `~/.local/state/omarchy/firetv/history.json` with owner-only file
permissions. The panel shows the latest five and has a Clear history action.
Turning Timeline off stops new entries but retains the existing file until
Clear history is used.

The control buttons send previous, play/pause, or next only when the active
app's media session advertises that action. A progress bar and total time
appear only if the app exposes a duration. The current YouTube session exposes
elapsed position but does not expose duration through ADB's text output, so
the panel shows elapsed time without a progress bar there.

## Install and connect

After this folder is published as a Git repository, install it on Omarchy with:

```sh
omarchy plugin add <git-repository-url> --enable
```

Click the TV icon in the bar. The first-run panel scans the local network for
devices listening on the Fire TV ADB port, then tries to connect to the single
device it finds. If it finds several, select the right address. You can always
enter the TV's IP manually. On the TV, turn on **Settings → My Fire TV →
Developer options → ADB Debugging** and accept **Allow USB debugging** for this
computer. If Developer options is hidden, select the device name under
**My Fire TV → About** seven times. The computer and stick must be on the same
network. The plugin saves the authorized IP in Omarchy's shell settings; use
**Change Fire TV** in the panel if it moves to another address.

Omarchy needs `adb` from `android-tools`. If it is not already installed, run
`omarchy pkg add android-tools` in a terminal. No additional service, account,
or app on the stick is needed. Scanning checks at most 512 local addresses and
only looks for port 5555; it does not connect to candidate devices until you
select one (or exactly one device is found). ADB authorization remains a
required action on the TV. The refresh interval is configurable in Omarchy's
shell settings.

The widget records a viewing timeline only when you enable it. It can only see activity on the
Fire TV Stick, not other TV inputs. App logos use locally installed icons when
available; other apps show a two-letter badge.
