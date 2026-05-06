# smsFetcher

Polls a **D-Link DWR-M960** 4G modem (HW B1, FW v1.01.07), saves every received
SMS as a JSON file, deletes it from the SIM, and pops one saved message at a
time as a Windows toast notification (paced by a configurable interval, paused
under Focus Assist / DND). If you don't ack a popup within a configurable
grace window, the SMS gets forwarded to a relay phone via FTP-upload + a
short URL-only outbound SMS, so you receive it remotely. Also polls a
[Telina hosted-PBX](https://hub.telina.ir/) account for new incoming calls
and SMSes the caller numbers to a configured phone, so you get notified of
calls that came in to your PBX while you were away. Runs as a Windows tray
app.

## What's in this folder

| File                       | Purpose                                                                |
| -------------------------- | ---------------------------------------------------------------------- |
| `smsFetcher.py`            | Source                                                                 |
| `smsFetcher.sample.conf`   | Template config with placeholders for personal values (`xxxx` / `1111`); copy to `smsFetcher.conf` and fill in, OR just run the exe and edit the auto-generated file |
| `build.py`                 | Build script: regenerates `icon.ico` and produces `smsFetcher.exe`     |
| `icon.ico`                 | App icon (also used as the tray icon at runtime)                       |

`smsFetcher.exe` itself is **gitignored** because it's a 30 MB rebuild
artifact and shipping every build through git history would inflate
the repo by ~30 MB per commit. Build it once with `python build.py`
(see [Building from source](#building-from-source)) — the resulting
exe lives next to `build.py` and is fully portable.

When the exe runs for the first time it creates these siblings next to itself:

| Path                     | Created on                              |
| ------------------------ | --------------------------------------- |
| `smsFetcher.conf`        | First launch, from built-in defaults    |
| `sms/`                   | First launch (eagerly, before threads start) |
| `sms_del/`               | First launch (eagerly)                  |
| `log/`                   | First launch (eagerly); first log line of each day rolls into `yyyymmdd.log` |
| `usage_total/`           | First launch (eagerly)                  |
| `usage_clients/`         | First launch (eagerly)                  |
| `temp/`                  | First launch (eagerly)                  |
| `blacklist.json`         | First time you click Block on a popup   |
| `smsFetcher.state.json`  | First time a delete-after-save is queued for retry |
| `usage_state.json`       | First usage tick (carries last-counter snapshot for delta math) |
| `usage_total/total_yyyymmdd.txt` | First usage tick on a given day |
| `usage_clients/192-168-1-X_yyyymmdd.txt` | First per-IP usage tick on a given day |
| `temp/yyyymmdd-hhmmss.htm` | Each SMS forwarded to the relay phone (deleted on full success) |
| `relay_state.json`       | First relay attempt (carries per-SMS upload/sent timestamps + failure budget) |
| `temp/last-calls.json`   | First Telina watcher tick (snapshot of the 5 most-recent CDR `cuid`s) |

Everything is path-relative to the exe — fully portable, no install needed.

## Running

Double-click `smsFetcher.exe`. A green-circle "S" icon appears in the system
tray. Right-click it for two options:

- **Log** — opens a modal with today's log file (Esc or Close to dismiss)
- **Exit** — stops the threads and quits

A second launch shows a "smsFetcher is already running" warning and exits — the
single-instance guard binds `127.0.0.1:50917` (configurable).

### What you see at runtime

- **Notification popups** appear one at a time in the bottom-right
  corner of the primary monitor. Each popup is a small dark Tk window
  (no title bar, always on top) with:
  - The sender as the header
  - The body, truncated to `notification_body_max_chars`
  - Three buttons: **Dismiss** (close only) / **Block** (silence this
    sender forever) / **Open** (full SMS modal)
  - An × in the corner (same as Dismiss)
  - Click anywhere on the body = same as Open
  The popup is **sticky** — it stays on screen until you click one of
  the controls. **Only one popup at a time**: the next message
  appears as soon as you ack the current one, with no extra delay. If
  the queue is empty, smsFetcher waits `notification_interval_seconds`
  (60 s default) before re-checking. So the interval only paces *idle*
  polling, not your active read-through of a backlog.

- **Blacklist (Block button)**. Clicking Block on a popup adds the
  sender (phone number or alphanumeric sender id like `KHABAR`) to
  `blacklist.json` and advances the queue immediately, like Dismiss.
  Subsequent SMS from that sender are silently dropped:
  - The Fetcher saves them straight to `sms_del/` instead of `sms/`
    (logged as `BLOCKED ...`).
  - If a blacklisted SMS is already sitting in `sms/` when you click
    Block, the Notifier's next tick walks past it without showing a
    popup and moves it to `sms_del/` (also logged).
  `blacklist.json` is a UTF-8 JSON array of sender strings. Edit it
  manually to add/remove senders; changes take effect immediately —
  Fetcher and Notifier read it on every check.
- **Red tray icon while there are unread popups.** Each popup that's
  been shown but not acted on bumps an in-process counter; while count
  > 0 the tray icon turns red and the tooltip reads
  `smsFetcher (3 unread)`. Counter goes back to zero once you've cleared
  every outstanding popup, and the icon turns green again.
- **Yellow tray icon while a relay is in flight.** When an SMS is being
  forwarded to the relay phone (FTP upload + outbound SMS), the icon
  goes yellow. It reverts to red (still unread) or green (idle) once
  the relay finishes. Yellow is transient — it's a few seconds per
  forward — and always wins over red while it's on. See [SMS relay
  (when you're away)](#sms-relay-when-youre-away) below.
- **DND respected.** When Windows is in Focus Assist, full-screen game,
  presentation mode, or "do not disturb", the queue **pauses** — no JSON
  gets consumed. As soon as DND clears, the next tick picks up the
  oldest unread SMS. (Internally: `SHQueryUserNotificationState` must
  return `QUNS_ACCEPTS_NOTIFICATIONS`.)
- **Move on ack.** While a popup is on screen the SMS JSON stays in
  `sms/` so the Relayer can locate it under its original basename for
  potential forwarding. The moment you click Dismiss / Block / Open
  (or the × in the corner), the JSON is moved to `sms_del/`. The
  popup carries the parsed body in memory, so it works regardless of
  whether the file moves while the popup is open. If a name collision
  exists in `sms_del/` the suffix `-d02`, `-d03`, … is appended.

#### Why a custom popup instead of native Windows toasts?

Native toasts via `windows-toasts` were tried first and rejected:
- The basic `WindowsToaster` shows the toast for 5–7 seconds and
  auto-dismisses with no setting to make it sticky.
- `InteractableWindowsToaster` (which would give us sticky scenario +
  buttons) requires the application's AUMID to be registered via a
  Start Menu shortcut. For an unregistered AUMID,
  `show_toast()` returns success and `on_failed` doesn't fire, but the
  visual toast is silently suppressed by Windows — the failure is
  invisible to the app.

The Tk popup avoids that whole class of problem: there is no AUMID, no
Start Menu shortcut requirement, no Action Center registration. It also
gives full control over click behavior and styling.

## Config

`smsFetcher.conf` is JSON. Defaults:

```json
{
  "modem_url": "http://192.168.1.8",
  "username": "admin",
  "password": "xxxx",
  "poll_interval_seconds": 500,
  "sms_folder": "sms",
  "log_folder": "log",
  "state_file": "smsFetcher.state.json",
  "request_timeout_seconds": 15,
  "single_instance_port": 50917,
  "delete_after_save": true,
  "notification_interval_seconds": 60,
  "notified_folder": "sms_del",
  "enable_notifications": true,
  "respect_dnd": true,
  "notification_body_max_chars": 250,
  "blacklist_file": "blacklist.json",
  "usage_interval_seconds": 300,
  "usage_total_folder": "usage_total",
  "usage_clients_folder": "usage_clients",
  "usage_state_file": "usage_state.json",
  "client_name_refresh_seconds": 3600,
  "relay_enabled": true,
  "relay_timeout_seconds": 10,
  "relay_interval_seconds": 60,
  "relay_sms_country_code": "98",
  "relay_sms_number": "1111",
  "relay_url_base": "xxxx",
  "ftp_host": "xxxx",
  "ftp_port": 21,
  "ftp_user": "xxxx",
  "ftp_pass": "xxxx",
  "ftp_remote_dir": "/public_html/sms",
  "relay_temp_folder": "temp",
  "relay_state_file": "relay_state.json",
  "relay_failure_limit_per_hour": 10,
  "relay_pause_minutes_after_limit": 60,
  "ftp_cleanup_interval_hours": 6,
  "ftp_retention_hours": 24,
  "sms_del_retention_days": 30,
  "outbox_cleanup_after_minutes": 5,
  "telina_enabled": true,
  "telina_interval_seconds": 1800,
  "telina_username": "xxxx",
  "telina_password": "xxxx",
  "telina_notif_number": "09111111111",
  "telina_state_file": "temp/last-calls.json"
}
```

If you upgrade the exe and your existing `smsFetcher.conf` is missing newer
keys, smsFetcher fills them in automatically on next run (your existing
values are preserved).

Edit the file, restart the exe.

`smsFetcher.conf` itself is **gitignored** because it carries the modem
password, the FTP credentials, and the Telina hub password. The repo
ships `smsFetcher.sample.conf` instead — copy it to `smsFetcher.conf`
and fill in the placeholders, or just launch the exe once and edit
the auto-generated `smsFetcher.conf`.

## Saved SMS format

Filename: `yyyymmdd-hhmmss-<sender>.json`, e.g.
`20260430-175503-981111.json`. On collisions a `-02`, `-03`, … suffix is
appended.

```json
{
  "index": "47,48",
  "stat": 0,
  "sender": "981111",
  "received_at": "2026-04-30 17:53:37",
  "body": "F:+989999920000\nپانک سامان\n…",
  "fetched_at": "2026-04-30T19:52:14+03:30",
  "modem_url": "http://192.168.1.8"
}
```

- `index` — the SIM-slot id reported by the panel; multipart messages arrive as
  comma-grouped slots (e.g. `"47,48"`) already concatenated by the panel.
- `stat` — `0` = unread on the modem, `1` = read.
- `body` — UTF-8; the panel's `<br>` separators are converted to `\n`.

When the Notifier moves a JSON to `sms_del/`, on filename collision it
appends `-d02`, `-d03`, … so nothing is overwritten.

## SMS relay (when you're away)

If a popup sits unacked for `relay_timeout_seconds` (default 10 s),
smsFetcher concludes you're away from the PC and forwards the SMS to
a configured relay phone via FTP-upload + a short URL-only outbound
SMS sent through the same modem. The popup stays on screen for you to
ack later, and additional incoming SMS keep being forwarded — at most
once per `relay_interval_seconds` (default 60 s) — without showing
their own popups, since the popup queue advances only on ack and you
need to receive the new ones remotely too.

### What gets forwarded, in order

For each SMS the Relayer touches:

1. Writes a small HTML file to `temp/yyyymmdd-hhmmss.htm` (UTF-8,
   no BOM). The page declares `<meta charset="utf-8">` itself so the
   browser doesn't guess the encoding from HTTP headers — that's what
   was breaking Persian text in earlier `.txt` builds, where
   Apache/nginx default content-type for `.txt` doesn't carry a
   charset and the browser falls back to the locale default. The
   body is `dir="auto"` so RTL Persian and LTR English both render
   correctly. A small inline stylesheet keeps it readable on phones.
   Body text is HTML-escaped so any `<` / `&` / `>` in the SMS can't
   break the page. Sketch:

   ```html
   <!doctype html>
   <html lang="fa">
   <head><meta charset="utf-8"><title>SMS — 981111</title>
   <style>…inline styles…</style></head>
   <body>
   <div class="meta">
   <b>From:</b> 981111<br>
   <b>Date:</b> 2026-04-30 17:53:37
   </div>
   <div class="body" dir="auto">…sms body…</div>
   </body></html>
   ```

2. Uploads it to `<ftp_remote_dir>/` on `<ftp_host>` via plain FTP
   (port 21, cleartext password — the host the user pointed us at
   accepts this). Upload mode is **binary** (`ftplib.storbinary`,
   TYPE I) so the UTF-8 bytes transfer byte-for-byte without any
   line-ending or charset translation. On `STOR` failure, see
   "failure budget" below.

3. Sends a short outbound SMS via the modem panel's `sendMsg` action
   to `<relay_sms_country_code><relay_sms_number>` (default
   `981111`). The body is just the URL — `<relay_url_base>` +
   the remote filename, e.g. `xxxx/20260430-175533.htm`.
   Kept ASCII-only and well under the modem firmware's quiet
   send-limit so it actually delivers (Persian/UCS-2 text > ~17 chars
   silently drops, ASCII URLs at this length go through reliably).

4. On modem 200 OK, deletes the local temp file. On non-200, leaves
   the temp file in place and retries the SMS step on the next tick
   (the FTP step is not re-done — `relay_state.json` carries the
   `uploaded_at` per-SMS so we don't double-upload on retry/restart).

### Scheduling

The Relayer ticks once per second, but most ticks are no-ops. A relay
attempt fires only when **all** of these are true:

- Relay is enabled (`relay_enabled`).
- We're not in a paused window (failure budget — see below).
- Windows is **not** in DND / Focus Assist / full-screen.
- A popup is on screen and **not** acked.
- That popup has been on screen for at least `relay_timeout_seconds`.
- We haven't relayed anything within the last `relay_interval_seconds`
  (i.e. we're past the per-SMS cooldown — applies from the second
  forwarded SMS onward).

The first SMS forwarded each "away cycle" is the popup file itself.
Subsequent forwards iterate the oldest non-blacklisted, not-yet-fully-
relayed file in `sms/`. Once you ack the open popup, away mode ends
immediately — the next popup fires a fresh `relay_timeout_seconds`
window, and a quick ack from you skips relay entirely.

### Tray icon — yellow during relay

While an FTP upload + outbound SMS is in flight, the tray icon goes
yellow (and the tooltip reads `smsFetcher (relaying)` or
`smsFetcher (relaying, N unread)`). It reverts to red or green when
the relay finishes. Yellow always wins over red.

### State file: `relay_state.json`

Atomic-written via `.tmp` + replace, like `usage_state.json`. Carries:

- `relayed[<sms_basename>]` — for each SMS that's been relayed (in
  whole or in part):
  - `uploaded_at` — set after FTP STOR succeeds
  - `remote_filename`, `local_filename`, `url`
  - `source_basename` — the original `sms/` filename
  - `sent_at` — set after the modem returns 200 OK on the SMS send
- `failures` — rolling 1-hour list of FTP failure timestamps
- `pause_until` — ISO timestamp; when set, all relay attempts are
  paused until this time
- `last_outgoing_sms_at` — used to time outbox cleanup
- `outbox_cleaned` — boolean flag flipped to `false` on each send

The Relayer prunes `relayed` entries whose source file is no longer
present in either `sms/` or `sms_del/` (i.e. the SMS itself has been
purged by the `sms_del` retention pass below).

### Failure budget (FTP-only)

Only **FTP failures** accumulate against the budget — the FTP host
can IP-ban after too many bad attempts, while the modem just won't
ban us. Each failure appends to a 1-hour rolling window; once the
window holds `relay_failure_limit_per_hour` (default 10) entries, the
relay path pauses for `relay_pause_minutes_after_limit` (default 60)
and `pause_until` is set in the state file. After the pause window
elapses, the failure list resets to empty. A successful relay also
resets the budget (and clears any pause).

Modem-send failures (HTTP non-200 from the panel) do **not** count.
They retry indefinitely every `relay_interval_seconds`; the FTP slot
isn't re-spent because `uploaded_at` is already in state.

### Outbox cleanup

The DWR-M960's SIM has an outbox that fills up if you keep sending.
`outbox_cleanup_after_minutes` (default 5 min) after the most recent
outbound SMS, the Relayer connects to the panel, lists the outbox via
`/sms_outbox.htm` (same `smsListInfo` shape as the inbox), and
bulk-deletes everything via `formSmsManage` `action_id=delete` with
the indices comma-joined and `submit-url=/sms_outbox.htm`. The 5-min
delay is so the SMSC has time to actually deliver the message before
its outbox slot is freed.

### Server-side cleanup

Every `ftp_cleanup_interval_hours` (default 6 h) the Relayer connects
to the FTP server, lists `<ftp_remote_dir>` via MLSD, and deletes any
file whose `modify` timestamp (UTC, from the server) is older than
`ftp_retention_hours` (default 24 h).

### Local sms_del cleanup

In the same 6-hour cadence the Relayer also walks `sms_del/` and
deletes files older than `sms_del_retention_days` (default 30).
Stale `relay_state.json` entries get pruned in the same pass.

### What blocks the relay path entirely

- DND / Focus Assist / full-screen / presentation mode → entire relay
  path pauses (no FTP, no outbound SMS, no outbox cleanup, no
  server-side cleanup either).
- Sender on the blacklist → never enters the relay path. Either the
  Fetcher routed the SMS straight to `sms_del/` at receive time, or
  if the sender was added to the blacklist later the Relayer's
  per-tick scan skips already-saved files from that sender (and the
  Notifier silently moves them on its next tick).

## LTE usage tracking

A separate thread polls the modem's two stats pages every
`usage_interval_seconds` (300 s default) and turns the running counters
into per-day deltas, written to plain `.txt` files for easy reading.

### Sources

- **LTE total** — `GET /stats.htm`. The page embeds two JS variables in
  the HTML body:
  ```js
  var lteTx="<bytes_sent>";
  var lteRx="<bytes_received>";
  ```
  These are cumulative since modem boot.
- **Per-client (per-IP)** — `GET /usertraffic.htm`. Each `<tr>` has 5
  `<td>`: IP, Total Down, Total Up, **Lte Down**, **Lte Up**. Small
  numbers use space-as-thousand-separator (e.g. `1 435 508`); at the
  GB/TB boundary the firmware inserts a magnitude letter (K/M/G/T)
  in place of the space (e.g. `3G 894 619 280` = 3,894,619,280). We
  strip all non-digits to handle both. The same IP can appear on
  multiple rows; we sum them. **Caveat**: the per-IP downstream
  counter on this firmware significantly under-counts vs the global
  `lteRx`, so per-client `receive` numbers in `clients-total_*.txt`
  will not sum to the global total. See [Usage counters — known
  firmware quirks](#usage-counters--known-firmware-quirks) below.
- **Client names** — the modem itself has no usable hostname source on
  this firmware (`/getsignal.cgi?action=getClient` returns empty
  hostname columns). Names are resolved from the host with reverse
  DNS (`socket.gethostbyaddr`) on a slow cache: each IP is re-resolved
  at most every `client_name_refresh_seconds` (3600 s default). IPs
  that don't resolve simply have no `names=` line in their file.

### File format

While the day is **still running**, only the three byte lines are
written and the file is rewritten on every tick:

```
send=215 MB
receive=1.45 GB
total=1.66 GB
```

When the day is **finalized** — either smsFetcher's tick crossed
midnight, or the day was filled in retroactively from a multi-day
gap — a `calc-status=` line is appended:

```
send=2.31 GB
receive=14.07 GB
total=16.38 GB
calc-status=accurate
```

Per-IP files have the same shape, plus an optional `names=` line when
reverse DNS resolved a hostname:

```
send=12 MB
receive=420 MB
total=432 MB
calc-status=accurate
names=FATEHI-PC
```

Alongside `total_yyyymmdd.txt`, each tick also rewrites
`usage_total/clients-total_yyyymmdd.txt` — a space-padded table of
all non-zero clients for that day, heaviest first. Each column is
left-aligned to its widest cell and separated by 5 spaces, so the
file lines up cleanly when viewed in a monospace font:

```
ip             total      send       receive    names
192.168.1.6    1.66 GB    215 MB     1.45 GB    FATEHI-PC
192.168.1.18   14 MB      3 MB       11 MB
192.168.1.2    2 MB       500 KB     1.5 MB
```

Clients with `total=0` are excluded so the table stays focused on
actually-active hosts.

The absence of `calc-status=` on a file means "this day is still in
progress" (or, in pathological cases, the app was killed while running
and never got a chance to finalize it; on next launch the rollover
logic will finalize it).

Number format: B / KB / MB rounded to integer; GB / TB to 2 decimals.
There's always a space between the number and the unit.

### Day rollover and missed midnights

Each tick rewrites today's files with the running cumulative for the
day. The day boundary is **local midnight** (Windows clock).

If the app misses one or more midnights — e.g. PC was suspended from
22:35 to 08:05 next day — the cumulative delta from the modem since
the last tick is split across the affected days **proportionally to
elapsed seconds**. Each affected day's `calc-status` is set as
follows:

| Status       | Meaning |
| ------------ | ------- |
| `accurate`   | All ticks within this day landed on a real interval, no resets, no boundary crossings.  |
| `average`    | At least one tick crossed midnight (so the boundary value was estimated by averaging) **or** a counter reset (modem reboot) was detected during the day. |
| `incomplete` | A whole day passed with **zero** ticks (e.g. PC was off > 24 h). The number is still filled in by averaging across the full gap, but the value is best-effort only. |

A day's status is the **worst** of all events that contributed to it.

### State file

`usage_state.json` (atomically written via `.tmp` + replace) carries:

- `last_check_at` — ISO timestamp of the previous tick
- `last_lte` and `last_per_ip` — counter snapshot at the previous tick,
  used to compute the delta on the next tick
- `today` — running counters for today (so a restart mid-day doesn't
  lose the partial total)
- `client_names` — `{ip → {names: [...], resolved_at: "..."}}` cache

### Counter resets (modem reboot)

When the modem reboots, its byte counters reset to 0 and the next read
will be lower than the last snapshot. The tracker detects
`current < last` per metric, treats the *current* value as a fresh
increment from 0 (since the reset moment is unknown), and promotes the
day's `calc-status` to `average`. This produces a slight overestimate
relative to wall-clock truth — the alternative (zero out the bucket)
would underestimate.

## Telina hosted-PBX call notifier

A separate thread polls a [Telina hosted-PBX](https://hub.telina.ir/)
account every `telina_interval_seconds` (1800 s = 30 min default) for the
5 most-recent CDR rows, diffs them by call `cuid` against
`temp/last-calls.json`, and SMSes the caller numbers of any new calls
to `telina_notif_number` via the same modem the rest of the app uses.

### Why a separate thread

The Telina path is unrelated to the main goal of this app (forwarding
SIM-modem SMS) — it just happens to share the modem to send the
outbound notification. We isolate it in its own thread so any failure
(network blip, hub login error, malformed CDR row, panel firmware
hiccup) is logged and contained, never affecting the Fetcher /
Notifier / Relayer / UsageTracker. The whole feature can be turned off
by setting `telina_enabled` to `false` in the config.

### Tick flow

Each tick performs three plain HTTP calls — no headless browser, no
WebSocket, no background AJAX:

1. **Login.** `POST https://api.hostedpbx.ir/graphql` with the
   `signin(input: SigninInput)` query, where `SigninInput` takes
   `identity` / `password` / `domain`. The reseller domain
   (`hub.telina.ir`) is hardcoded — the API rejects the request
   without it because multiple reseller fronts share the same backend.
   Returns a JWT in `data.signin.token` and the user's id in
   `data.signin.user.id`.

2. **Resolve appId.** Same endpoint, query
   `nupGetMyApp(userId: String)`, with auth headers
   `X-APIKEY: <token>`, `APP-ID: app-selector` (literal string — that's
   the only value that works before an app is picked), and
   `USER-ID: <userId>`. Returns `userApps[].app.id`; we take the first
   one.

3. **Fetch top 5 CDR rows.** `GET https://pbx.telina.ir/api/trpc/`
   `report.advanced.getAdvancedSystem?input=<urlencoded JSON>`. The
   tRPC handler destructures `filters`, so the date window has to be
   present even when the sort makes it irrelevant — we pass the last
   30 days. Sort is `_id` desc (server-side stable) and limit is 5.
   Auth headers (lower-case for tRPC vs the upper-case GraphQL ones):
   `x-apikey`, `app-id`, `user-id`, `from-support-menu: no`. Returns
   `data.result.data.results: [{...row}]` with at least these fields:
   `cuid`, `type` (`incoming` / `outgoing`), `src`, `dst`, `disposition`
   (`ANSWERED` / `NO ANSWER` / …), `starttime`, `duration`, `billsec`.

The token + appId are cached across ticks. On a 401 from tRPC the
watcher drops both and re-logs in once before giving up for that tick;
the next tick starts fresh.

### Why the URL `pbx.telina.ir/dashboard/<num>/<5char>/dashboard` is
### irrelevant

When you click "Go to app" in the hub UI, the browser ends up at a
URL that looks server-meaningful — e.g.
`pbx.telina.ir/dashboard/1/jh88q/dashboard`. It isn't: those two path
segments are an auto-increment id and a 5-character random hash that
the **client-side** code generates the first time you open the panel
and stores in your browser's IndexedDB (`appIds` store, keyed by
appId). The server doesn't care about them — auth on tRPC is purely
the three headers above. We skip the whole bridge-token round-trip
and the IndexedDB shenanigans entirely.

### Diff logic

`temp/last-calls.json` carries the cuids of the 5 rows seen on the
previous tick:

```json
{
  "cuids": [
    "1777799482.32427",
    "1777724903.66392",
    "1777697442.762",
    "1777627145.2880",
    "1777627132.2877"
  ]
}
```

Each tick compares the current 5 cuids against that set. New cuids
(present now, absent before) become the SMS body — one number per
line, no header, in the same order the API returned them (newest
first). The notification recipient is `telina_notif_number`; numbers
in Iran local format (`09xxxxxxxxx`) are rewritten by stripping the
leading `0` and prefixing `relay_sms_country_code` (default `98`),
giving the correct international form.

After a successful send (or if there were no new cuids) the state
file is rewritten with the current 5 cuids so they don't keep firing.
On send failure the state is left alone — the same cuids will retry
on the next tick.

### First-run silence

If `temp/last-calls.json` is missing on tick 1 (fresh install,
disabled→enabled toggle, or you wiped state), the watcher writes the
current snapshot and **sends no SMS** that tick. Otherwise the very
first run after install would treat all 5 rows as new and fire on
startup, which is almost never what the user wants.

A corrupt-but-present state file is treated the same as "no new" —
we suppress one tick rather than fire 5 unwanted SMSes.

## How it works (modem reverse-engineering reference)

The modem runs two HTTP servers behind port 80: **Boa/0.94.14rc21** (login) and
**micro_httpd** (everything else). The session token is the cookie
`webuicookie`.

### 1. Login — HMAC-MD5 challenge-response

```
POST /boafrm/formLoginKey
Content-Type: application/x-www-form-urlencoded
Body: username=admin

→ 200 application/json: {"Challenge": "<20 chars>", "PublicKey": "<20 chars>"}
```

```python
priv      = hmac_md5_hex(key = PublicKey + plain_password, msg = Challenge).upper()
loginPwd  = hmac_md5_hex(key = priv,                       msg = Challenge).upper()
```

```
POST /boafrm/formLoginSetup
Content-Type: application/x-www-form-urlencoded
Referer: http://<modem>/login.htm
Body: username=admin&password=<loginPwd>

→ 200, Set-Cookie: webuicookie=<token>; path=/
```

If credentials are wrong the response is `302 Location: /login.htm` with no
Set-Cookie. Sessions idle-expire after roughly 15 minutes — the client
detects a 108-byte unauthenticated stub on next request and re-logs in.

### 2. List inbox

```
GET /sms_inbox.htm
Cookie: webuicookie=<token>
```

The inbox is rendered server-side as a JS string assigned to the global
`smsListInfo`. Records are split by `|,|`, fields by `}-{`:

```
{slotIndex}}-{{stat}}-{{sender}}-{{received}}-{{body}|,| …
```

Multipart messages have comma-grouped slot indices (e.g. `"47,48"`); body line
breaks are encoded as `<br>`. Persian text is plain UTF-8.

### 3. Delete

```
POST /boafrm/formSmsManage
Cookie:  webuicookie=<token>
Referer: http://<modem>/sms_inbox.htm
Content-Type: application/x-www-form-urlencoded
Body: submit-url=/sms_inbox.htm
    & action_id=delete
    & action_value=<index>          # e.g. "47,48"
```

**Critical**: the deletion only commits when the client follows the `302
Location: /sms_inbox.htm` that the modem returns. Skipping the redirect (i.e.
`allow_redirects=False`) *also* returns 302, but silently drops the deletion
and corrupts the session — subsequent requests look authenticated for a while
but no further deletes take effect. `ModemClient.delete_sms` therefore uses
`allow_redirects=True`.

### Sending SMS — known firmware limit

The send endpoint is the same `formSmsManage` with `action_id=sendMsg`,
`submit-url=/sms_new.htm`, and fields `countryCode`, `sendMsgNumber`,
`sendMsgContent`. The DWR-M960 v1.01.07 firmware silently drops sends
longer than roughly **17 UCS-2 characters** — HTTP returns 200, no
error indication, the message never reaches the SMSC and never appears
in the panel's outbox. **ASCII-only** sends of ~30 chars (URLs) go
through reliably, however, which is why the relay path forwards a
short URL rather than the SMS body itself.

### Outbox listing & delete

Outbox is at `GET /sms_outbox.htm`. Same `smsListInfo` JS-string shape
as the inbox: records split by `|,|`, fields by `}-{`, layout
`{slotIndex}}-{{stat}}-{{recipient}}-{{time}}-{{body}`. On this
firmware `stat=3` for sent messages and the `time` field is blank.

Bulk delete uses the same `formSmsManage` with `submit-url=/sms_outbox.htm`,
`action_id=delete`, `action_value=<index1,index2,…>`. Same redirect-
required quirk as inbox delete (must follow the 302 back to
`/sms_outbox.htm` for the deletion to commit).

### Usage counters — known firmware quirks

Two endpoints expose byte counters; both have caveats on this
firmware that are worth documenting.

`GET /stats.htm` returns the global LTE Tx/Rx counters as JS variables
in the page body (`var lteTx="<digits>"; var lteRx="<digits>";`).
Plain decimal strings, no formatting, no magnitude markers regardless
of size. Counters reset on modem reboot. **Reliable.**

`GET /usertraffic.htm` returns one `<tr>` per client session with 5
`<td>`: IP, Total Down, Total Up, Lte Down, Lte Up. Two firmware
quirks here:

1. **Number formatting.** Cell values use space-thousand-separators
   for small numbers (`"1 435 508"`), but at GB/TB boundaries the
   firmware inserts a magnitude letter (K/M/G/T) in place of the
   space between the high group and the rest: `"3G 894 619 280"`
   means 3,894,619,280. A naive `int(cell.replace(" ", ""))` raises
   `ValueError` on these and silently skips the row, dropping
   exactly the heaviest-traffic clients. Strip all non-digits
   (`re.sub(r'\D+', '', cell)`) instead. The "Total Down" and
   "Lte Down" columns hold identical values per row on this firmware
   (LTE is the only WAN); same for the Up columns. We read the LTE
   pair only.

2. **Per-IP counter is unstable in BOTH directions.**

   *Under-counting:* real-world test on 2026-05-04 mid-day,
   `/stats.htm lteRx = 2.79 GB` (since boot), while the sum of every
   row's "Lte Down" was only `619 MB` (78% of received bytes
   unaccounted-for). Most clients show `0 B` Lte Down even when
   actively downloading. Upload is closer but still off (~24% missing
   in the same test). The per-IP counter appears to lose traffic on
   session ageing / NAT table flush.

   *Over-counting via spurious resets:* the per-IP cumulative drops
   sharply mid-day without a modem reboot — log evidence shows ~22
   `cur < last` events for one heavy client during one day. Naive
   `_delta_with_reset(last, cur) = cur if cur < last else cur - last`
   adds the post-reset value as fresh usage at every reset, even
   though no real bytes were lost. Combined with the midnight
   rollover splitting the spike across days, this produced a
   recorded `57 GB` daily total for one client on a day when the
   global only saw `4.58 GB` — a physically-impossible 12.4× over-
   count.

   *Mitigation in code:* in `UsageTracker.tick()`, after computing
   `d_per_ip`, sum the per-IP tx and rx deltas; if either sum
   exceeds the corresponding global delta from `/stats.htm`, scale
   all per-IP deltas down proportionally so the sum equals the
   global. The global counter is treated as ground truth and per-IP
   becomes attribution-by-share. **Implication for
   `clients-total_*.txt`**: with the cap in place, per-client
   `receive` numbers sum to **at most** the global `receive` in
   `total_*.txt` — usually less (because of the under-counting
   problem above). Per-client is best-effort attribution only —
   useful for ranking heavy users but not for absolute accounting.

The same IP can appear on multiple `<tr>` rows in one response —
these are concurrent or historical client sessions for the same IP
(e.g. successive DHCP leases). We sum them.

### Other operational notes

- The modem inbox HTML is mostly UTF-8 but contains a few non-UTF-8 bytes in
  unrelated parts of the page. The `smsListInfo` regex extracts raw bytes,
  which decode cleanly as UTF-8 on their own.
- Behind a system HTTP proxy, the LAN connection to the modem can be hijacked
  by the proxy. `requests.Session.trust_env` is therefore set to `False` so
  `HTTP_PROXY` / `HTTPS_PROXY` env vars are ignored.
- Save-then-delete is safe across crashes: every message is `fsync`'d before
  the delete is attempted. If the delete fails after a successful save, the
  index is recorded in `smsFetcher.state.json` (`pending_delete`) and retried
  every poll cycle without re-saving.

## Building from source

Requirements (developer machine only — end users only need the exe):

```
Python 3.10+
pip install requests pystray pillow pyinstaller
```

Then:

```
python build.py
```

This regenerates `icon.ico` from `make_icon_image()` (so the binary's icon
always matches the tray idle state) and runs PyInstaller with `--onefile
--noconsole`. Output: `smsFetcher.exe` in this folder, ~24 MB.

## Logs

`log/yyyymmdd.log`, INFO level, one line per action:

```
00:21:14 [INFO] --- smsFetcher starting (config: smsFetcher.conf) ---
00:21:14 [INFO] logged in to modem
00:21:15 [INFO] polled inbox: 5 message(s) present
00:21:15 [INFO] saved SMS index=25 sender=981111 received_at='…' -> 20260430-232555-981111.json
00:21:16 [INFO] deleted from modem: index=25
00:21:16 [INFO] notified sender=981111 -> sms_del/20260430-212251-981111-02.json
00:21:20 [INFO] tray: Log clicked
00:21:25 [INFO] tray: Exit clicked
00:21:25 [INFO] --- smsFetcher stopping ---
```

The log writer rolls over at midnight (next entry opens
`yyyymmdd.log` for the new date).

## Threads

- **Fetcher** — polls the modem every `poll_interval_seconds` (500 s default),
  saves new SMS to `sms/`, deletes from SIM after fsync.
- **Notifier** — every `notification_interval_seconds` (60 s default), checks
  DND state and (if OK) pops the oldest JSON from `sms/` into a Tk
  popup window, then moves it to `sms_del/`.
- **UsageTracker** — every `usage_interval_seconds` (300 s default), reads
  `/stats.htm` and `/usertraffic.htm` from the modem, computes the delta
  vs the previous tick, and accumulates per-day LTE send/receive
  totals (overall and per client IP). Writes `usage_total/total_yyyymmdd.txt`
  and one `usage_clients/IP_yyyymmdd.txt` per active IP every tick.
- **Relayer** — ticks once per second; most ticks are no-ops. When a
  popup has been on screen for ≥ `relay_timeout_seconds` without an
  ack, forwards one SMS per `relay_interval_seconds` via FTP-upload
  + outbound modem SMS. Also runs the modem-outbox cleanup (5 min
  after last send), the FTP server cleanup (every 6 h), and the
  `sms_del/` retention sweep (every 6 h). State (per-SMS upload/sent
  timestamps, failure budget, pause-until) lives in
  `relay_state.json`.
- **TelinaWatcher** — every `telina_interval_seconds` (1800 s = 30 min
  default), logs in to the Telina hub via GraphQL, fetches the 5
  most-recent CDR rows from the PBX panel via tRPC, diffs by `cuid`
  against `temp/last-calls.json`, and SMSes new caller numbers to
  `telina_notif_number` via the same modem. Skipped entirely when
  `telina_enabled` is `false`. Exceptions are caught and logged in
  `tick()` so a broken Telina poll never affects the main flow.
- **Tk main thread** — runs the tray icon's mainloop and drains a
  `queue.Queue` for cross-thread UI requests (Log modal, SMS detail modal,
  Exit). Tray and toast callbacks fire on background threads and are
  bounced through the queue, since `tk.after()` is only loosely
  thread-safe across Python versions.
