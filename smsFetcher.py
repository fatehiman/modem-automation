"""
smsFetcher — fetches SMS from a D-Link DWR-M960 modem panel, saves them as
JSON files, and deletes them from the SIM. Runs as a Windows tray app.

Modem: D-Link DWR-M960 (HW B1, FW v1.01.07). Login uses HMAC-MD5
challenge-response; inbox is rendered server-side as a JS string in
/sms_inbox.htm; deletion is a POST to /boafrm/formSmsManage.
"""

import ftplib
import hashlib
import hmac
import html
import json
import logging
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import ctypes

import requests
from PIL import Image, ImageDraw, ImageFont
import pystray
import tkinter as tk
from tkinter import scrolledtext, messagebox
# Native Windows toasts via windows-toasts proved unreliable here:
# - Basic WindowsToaster: shows briefly (5–7 s) and auto-dismisses, with no
#   way to make the toast sticky.
# - InteractableWindowsToaster: claims to support sticky scenario + buttons,
#   but the visual toast is silently suppressed unless the AUMID is
#   registered via a Start Menu shortcut — show_toast() returns success and
#   no on_failed fires, so the failure is invisible to us.
# We draw our own popup with Tk instead. DND is still respected via
# accepts_notifications() before each tick.

APP_NAME = "smsFetcher"

DEFAULT_CONFIG = {
    "modem_url": "http://192.168.1.8",
    "username": "admin",
    "password": "xxxx",
    "poll_interval_seconds": 500,
    "sms_folder": "sms",
    "log_folder": "log",
    "state_file": "smsFetcher.state.json",
    "request_timeout_seconds": 15,
    "single_instance_port": 50917,
    "delete_after_save": True,
    "notification_interval_seconds": 60,
    "notified_folder": "sms_del",
    "enable_notifications": True,
    "respect_dnd": True,
    "notification_body_max_chars": 250,
    "blacklist_file": "blacklist.json",
    "usage_interval_seconds": 300,
    "usage_total_folder": "usage_total",
    "usage_clients_folder": "usage_clients",
    "usage_state_file": "usage_state.json",
    "client_name_refresh_seconds": 3600,
    "relay_enabled": True,
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
    "telina_enabled": True,
    "telina_interval_seconds": 1800,
    "telina_username": "xxxx",
    "telina_password": "xxxx",
    "telina_notif_number": "09111111111",
    "telina_state_file": "temp/last-calls.json",
}


# ---------- Telina hosted-PBX endpoints (hardcoded) ----------

TELINA_API_URL = "https://api.hostedpbx.ir/graphql"
TELINA_PBX_URL = "https://pbx.telina.ir"
TELINA_LOGIN_DOMAIN = "hub.telina.ir"


# ---------- DND / notification-state ----------

# SHQueryUserNotificationState return values (shellapi.h)
_QUNS_ACCEPTS_NOTIFICATIONS = 5

def accepts_notifications() -> bool:
    """True iff Windows is in a state that displays toasts (no Focus
    Assist, no full-screen game, no presentation mode, etc.)."""
    try:
        state = ctypes.c_int()
        hr = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if hr != 0:
            return True   # API failed — fail-open, don't drop notifications
        return state.value == _QUNS_ACCEPTS_NOTIFICATIONS
    except Exception:
        return True


def app_dir() -> Path:
    """Folder where the exe (or .py) lives — used for sibling config/sms/log."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# ---------- blacklist ----------

class Blacklist:
    """Set of sender ids (phone numbers or alphanumeric sender names)
    persisted as a JSON array. Both Fetcher (read on save) and Notifier
    (read on popup, write on Block click) touch this concurrently, so
    every operation goes through a lock."""

    def __init__(self, path: Path, logger: logging.Logger):
        self.path = path
        self.logger = logger
        self._lock = threading.RLock()
        self._senders: set[str] = set()
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._senders = {str(x).strip() for x in data if str(x).strip()}
            elif isinstance(data, dict) and isinstance(data.get("senders"), list):
                self._senders = {str(x).strip() for x in data["senders"] if str(x).strip()}
            self.logger.info(
                f"blacklist loaded: {len(self._senders)} sender(s)"
            )
        except Exception as e:
            self.logger.error(f"blacklist load failed: {e}")
            self._senders = set()

    def _save(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(sorted(self._senders), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass

    def add(self, sender: str) -> bool:
        sender = str(sender).strip()
        if not sender:
            return False
        with self._lock:
            if sender in self._senders:
                return False
            self._senders.add(sender)
            try:
                self._save()
            except Exception as e:
                self.logger.error(f"blacklist save failed: {e}")
                return False
            self.logger.info(f"blacklist: added '{sender}'")
            return True

    def contains(self, sender: str) -> bool:
        with self._lock:
            return str(sender).strip() in self._senders


# ---------- config ----------

def load_or_create_config():
    cfg_path = app_dir() / f"{APP_NAME}.conf"
    if not cfg_path.exists():
        cfg_path.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2),
            encoding="utf-8",
        )
        return dict(DEFAULT_CONFIG), cfg_path
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    missing = {k: v for k, v in DEFAULT_CONFIG.items() if k not in cfg}
    if missing:
        cfg.update(missing)
        cfg_path.write_text(
            json.dumps(cfg, indent=2),
            encoding="utf-8",
        )
    return cfg, cfg_path


# ---------- logging ----------

class DailyFileHandler(logging.Handler):
    """Writes to <log_dir>/yyyymmdd.log, switching files at midnight."""

    def __init__(self, log_dir: Path):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._cur_day = None
        self._fp = None
        self._lock = threading.Lock()

    def _open(self, day: str):
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
        path = self.log_dir / f"{day}.log"
        self._fp = path.open("a", encoding="utf-8")
        self._cur_day = day

    def emit(self, record):
        try:
            day = datetime.now().strftime("%Y%m%d")
            with self._lock:
                if day != self._cur_day:
                    self._open(day)
                self._fp.write(self.format(record) + "\n")
                self._fp.flush()
        except Exception:
            self.handleError(record)


def setup_logging(log_dir: Path) -> logging.Logger:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )
    handler = DailyFileHandler(log_dir)
    handler.setFormatter(fmt)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    return logger


# ---------- single instance ----------

def acquire_single_instance(port: int):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return None


# ---------- modem client ----------

class ModemClient:
    def __init__(self, base_url: str, username: str, password: str,
                 timeout: float, logger: logging.Logger):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.logger = logger
        self.session = requests.Session()
        self.session.trust_env = False

    @staticmethod
    def _hmac_md5_upper(key: str, msg: str) -> str:
        return hmac.new(
            key.encode("utf-8"), msg.encode("utf-8"), hashlib.md5
        ).hexdigest().upper()

    def login(self):
        self.session.cookies.clear()
        r = self.session.post(
            f"{self.base}/boafrm/formLoginKey",
            data={"username": self.username},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        challenge = data["Challenge"]
        public_key = data["PublicKey"]
        priv = self._hmac_md5_upper(public_key + self.password, challenge)
        login_pw = self._hmac_md5_upper(priv, challenge)
        r = self.session.post(
            f"{self.base}/boafrm/formLoginSetup",
            data={"username": self.username, "password": login_pw},
            headers={"Referer": f"{self.base}/login.htm"},
            timeout=self.timeout,
            allow_redirects=False,
        )
        cookie = self.session.cookies.get("webuicookie")
        if not cookie:
            raise RuntimeError(
                "login failed (no webuicookie set — wrong credentials?)"
            )
        self.logger.info("logged in to modem")

    @staticmethod
    def _looks_unauthed(body: bytes) -> bool:
        return b"parent.location='/login.htm'" in body or len(body) < 200

    def list_sms(self):
        url = f"{self.base}/sms_inbox.htm"
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        if self._looks_unauthed(r.content):
            self.logger.info("session expired, re-logging in")
            self.login()
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            if self._looks_unauthed(r.content):
                raise RuntimeError(
                    "could not authenticate to fetch inbox"
                )
        m = re.search(rb'smsListInfo\s*=\s*"(.*?)";', r.content, re.DOTALL)
        if not m:
            raise RuntimeError("smsListInfo not found in /sms_inbox.htm")
        raw = m.group(1).decode("utf-8", errors="replace")
        records = []
        for rec in raw.split("|,|"):
            if not rec:
                continue
            fields = rec.split("}-{")
            if len(fields) < 5:
                self.logger.warning(
                    f"skipping malformed SMS record: {rec[:80]!r}"
                )
                continue
            stat_raw = fields[1]
            try:
                stat = int(stat_raw)
            except ValueError:
                stat = stat_raw
            records.append({
                "index": fields[0],
                "stat": stat,
                "sender": fields[2],
                "received_at": fields[3],
                "body": fields[4].replace("<br>", "\n"),
            })
        return records

    def delete_sms(self, index_str: str):
        # The modem persists the deletion only when we follow the redirect back
        # to /sms_inbox.htm — otherwise rapid POSTs corrupt the session and
        # subsequent deletes are silently dropped (still returning 302).
        r = self.session.post(
            f"{self.base}/boafrm/formSmsManage",
            data={
                "submit-url": "/sms_inbox.htm",
                "action_id": "delete",
                "action_value": index_str,
            },
            headers={"Referer": f"{self.base}/sms_inbox.htm"},
            timeout=self.timeout,
            allow_redirects=True,
        )
        if r.status_code != 200 or self._looks_unauthed(r.content):
            raise RuntimeError(
                f"delete returned HTTP {r.status_code} or session lost"
            )

    def send_sms(self, country_code: str, number: str, content: str):
        """Send an outbound SMS via /sms_new.htm's form. The panel JS caps
        the body at 765 chars; longer than that is silently rejected.
        ASCII bodies of ~30 chars (URLs) go through reliably on this
        firmware — UCS-2/Persian sends are the ones that get silently
        dropped above ~17 chars, so this is only safe for ASCII."""
        url = f"{self.base}/boafrm/formSmsManage"
        r = self.session.post(
            url,
            data={
                "submit-url": "/sms_new.htm",
                "action_id": "sendMsg",
                "action_value": "tmp",
                "countryCode": country_code,
                "sendMsgNumber": number,
                "sendMsgContent": content,
            },
            headers={"Referer": f"{self.base}/sms_new.htm"},
            timeout=self.timeout,
            allow_redirects=True,
        )
        if r.status_code != 200 or self._looks_unauthed(r.content):
            raise RuntimeError(
                f"send_sms returned HTTP {r.status_code} or session lost"
            )

    def _list_sms_box(self, page: str) -> list:
        """Shared parser for /sms_inbox.htm and /sms_outbox.htm. Both
        render their records as a JS string `smsListInfo` with the same
        `|,|`-separated `}-{`-fielded layout (index, stat, peer, time,
        body)."""
        url = f"{self.base}/{page}"
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        if self._looks_unauthed(r.content):
            self.logger.info(f"session expired, re-logging in ({page})")
            self.login()
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            if self._looks_unauthed(r.content):
                raise RuntimeError(f"could not authenticate to fetch {page}")
        m = re.search(rb'smsListInfo\s*=\s*"(.*?)";', r.content, re.DOTALL)
        if not m:
            raise RuntimeError(f"smsListInfo not found in /{page}")
        raw = m.group(1).decode("utf-8", errors="replace")
        records = []
        for rec in raw.split("|,|"):
            if not rec:
                continue
            fields = rec.split("}-{")
            if len(fields) < 5:
                continue
            try:
                stat = int(fields[1])
            except ValueError:
                stat = fields[1]
            records.append({
                "index": fields[0],
                "stat": stat,
                "peer": fields[2],
                "time": fields[3],
                "body": fields[4].replace("<br>", "\n"),
            })
        return records

    def list_outbox(self) -> list:
        """Read the outbox. Outbox records have stat=3 (sent) and a blank
        `time` field on this firmware. Returns a list of {index, stat,
        peer, time, body}."""
        return self._list_sms_box("sms_outbox.htm")

    def delete_outbox(self, index_str: str):
        """Bulk-delete one or more outbox entries. `index_str` is a
        comma-joined list of slot indices (e.g. "5,7,8"). Same redirect
        rule as delete_sms — must follow the 302 back to /sms_outbox.htm
        for the deletion to commit."""
        r = self.session.post(
            f"{self.base}/boafrm/formSmsManage",
            data={
                "submit-url": "/sms_outbox.htm",
                "action_id": "delete",
                "action_value": index_str,
            },
            headers={"Referer": f"{self.base}/sms_outbox.htm"},
            timeout=self.timeout,
            allow_redirects=True,
        )
        if r.status_code != 200 or self._looks_unauthed(r.content):
            raise RuntimeError(
                f"delete_outbox returned HTTP {r.status_code} "
                f"or session lost"
            )

    # --- Usage statistics ---
    # /stats.htm renders LTE Tx/Rx as JS variables in the page body:
    #     var lteTx="<bytes_sent>";
    #     var lteRx="<bytes_received>";
    # /usertraffic.htm renders one <tr> per IP with 5 <td>: IP, Total Down,
    #   Total Up, LTE Down, LTE Up. Numbers may include space-thousand-separators
    #   (e.g. "1 435 508"). The same IP can appear on multiple rows; we sum.

    _LTE_TX_RE = re.compile(rb'var\s+lteTx\s*=\s*"(\d+)"')
    _LTE_RX_RE = re.compile(rb'var\s+lteRx\s*=\s*"(\d+)"')

    def get_lte_total(self) -> dict:
        r = self.session.get(f"{self.base}/stats.htm", timeout=self.timeout)
        if self._looks_unauthed(r.content):
            self.logger.info("session expired, re-logging in (stats)")
            self.login()
            r = self.session.get(
                f"{self.base}/stats.htm", timeout=self.timeout
            )
        m_tx = self._LTE_TX_RE.search(r.content)
        m_rx = self._LTE_RX_RE.search(r.content)
        if not m_tx or not m_rx:
            raise RuntimeError("stats.htm: lteTx/lteRx not found")
        return {"tx": int(m_tx.group(1)), "rx": int(m_rx.group(1))}

    _IP_RE = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
    _NON_DIGIT_RE = re.compile(r'\D+')

    @classmethod
    def _parse_byte_cell(cls, s: str) -> int:
        digits = cls._NON_DIGIT_RE.sub('', s)
        return int(digits) if digits else 0

    def get_per_ip_traffic(self) -> dict:
        r = self.session.get(
            f"{self.base}/usertraffic.htm", timeout=self.timeout
        )
        if self._looks_unauthed(r.content):
            self.logger.info("session expired, re-logging in (usertraffic)")
            self.login()
            r = self.session.get(
                f"{self.base}/usertraffic.htm", timeout=self.timeout
            )
        text = r.content.decode("utf-8", errors="replace")
        result: dict[str, dict[str, int]] = {}
        # Each <tr> row: <td>IP</td><td>TotalDown</td><td>TotalUp</td>
        # <td>LteDown</td><td>LteUp</td>. Numbers use spaces as thousands
        # separators, AND the firmware inserts K/M/G/T as thousands-group
        # markers at magnitude boundaries (e.g. "3G 894 619 280" = 3,894,619,280).
        # Stripping all non-digits handles both. We only care about the
        # LTE columns (3 and 4, 0-indexed).
        for row in re.findall(r"<tr>(.*?)</tr>", text, re.DOTALL):
            cells = re.findall(r"<td>([^<]*?)(?:<|$)", row)
            if len(cells) < 5:
                continue
            ip = cells[0].strip()
            if not self._IP_RE.match(ip):
                continue
            try:
                lte_down = self._parse_byte_cell(cells[3])
                lte_up = self._parse_byte_cell(cells[4])
            except (ValueError, IndexError):
                continue
            entry = result.setdefault(ip, {"tx": 0, "rx": 0})
            entry["tx"] += lte_up      # uploaded
            entry["rx"] += lte_down    # downloaded
        return result


# ---------- persistence ----------

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9+_\-.]")


def _safe_part(s: str) -> str:
    return _FILENAME_SAFE.sub("_", s).strip("_") or "unknown"


def save_sms(record: dict, sms_dir: Path, modem_url: str) -> Path:
    sms_dir.mkdir(parents=True, exist_ok=True)
    try:
        dt = datetime.strptime(record["received_at"], "%Y-%m-%d %H:%M:%S")
        ts = dt.strftime("%Y%m%d-%H%M%S")
    except Exception:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    sender = _safe_part(record["sender"].lstrip("+"))
    base_name = f"{ts}-{sender}"
    path = sms_dir / f"{base_name}.json"
    n = 2
    while path.exists():
        path = sms_dir / f"{base_name}-{n:02d}.json"
        n += 1
    payload = {
        **record,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "modem_url": modem_url,
    }
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    return path


# ---------- state (pending deletions) ----------

class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.pending_delete = set()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.pending_delete = set(data.get("pending_delete", []))
            except Exception:
                pass

    def save(self):
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(
                {"pending_delete": sorted(self.pending_delete)},
                f, indent=2,
            )
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)


# ---------- fetcher loop ----------

class Fetcher:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.modem = ModemClient(
            cfg["modem_url"], cfg["username"], cfg["password"],
            cfg["request_timeout_seconds"], logger,
        )
        self.sms_dir = app_dir() / cfg["sms_folder"]
        self.del_dir = app_dir() / cfg["notified_folder"]
        self.state = State(app_dir() / cfg["state_file"])
        self.blacklist: "Blacklist | None" = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def trigger_now(self):
        self._wake.set()

    def run(self):
        try:
            self.modem.login()
        except Exception as e:
            self.logger.error(f"initial login failed: {e}")
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as e:
                self.logger.error(f"cycle error: {e}", exc_info=True)
            self._wake.clear()
            self._wake.wait(self.cfg["poll_interval_seconds"])

    def cycle(self):
        records = self.modem.list_sms()
        self.logger.info(f"polled inbox: {len(records)} message(s) present")
        new_count = 0
        for rec in records:
            idx = rec["index"]
            if idx in self.state.pending_delete:
                try:
                    self.modem.delete_sms(idx)
                    self.state.pending_delete.discard(idx)
                    self.state.save()
                    self.logger.info(f"retried delete OK: index={idx}")
                except Exception as e:
                    self.logger.warning(
                        f"retry delete still failing: index={idx}: {e}"
                    )
                continue
            sender = rec["sender"]
            blocked = bool(self.blacklist and self.blacklist.contains(sender))
            target_dir = self.del_dir if blocked else self.sms_dir
            try:
                path = save_sms(rec, target_dir, self.cfg["modem_url"])
            except Exception as e:
                self.logger.error(
                    f"save failed: index={idx} sender={sender}: {e}"
                )
                continue
            if blocked:
                self.logger.info(
                    f"BLOCKED index={idx} sender={sender} "
                    f"received_at='{rec['received_at']}' -> "
                    f"{target_dir.name}/{path.name}"
                )
            else:
                self.logger.info(
                    f"saved SMS index={idx} sender={sender} "
                    f"received_at='{rec['received_at']}' -> {path.name}"
                )
            new_count += 1
            if not self.cfg.get("delete_after_save", True):
                continue
            try:
                self.modem.delete_sms(idx)
                self.logger.info(f"deleted from modem: index={idx}")
            except Exception as e:
                self.logger.warning(
                    f"delete failed (will retry next cycle): index={idx}: {e}"
                )
                self.state.pending_delete.add(idx)
                self.state.save()
        if new_count == 0 and not records:
            self.logger.info("inbox empty, nothing to do")


# ---------- notifier (Windows toasts) ----------

class Notifier:
    """Pops one saved SMS into a Windows toast every interval, then moves
    the JSON to `notified_folder/`. Pauses while Windows is in DND/Focus
    Assist/full-screen so messages aren't lost behind a suppressed toast.

    Toasts are shown with scenario=Reminder + Open/Dismiss buttons so they
    stay on screen until the user acts. An in-process unread counter is
    kept; the tray icon is asked to recolour itself when the count crosses
    zero in either direction."""

    def __init__(self, cfg: dict, logger: logging.Logger,
                 ui_queue: "queue.Queue",
                 on_unread_change=None,
                 blacklist: "Blacklist | None" = None):
        self.cfg = cfg
        self.logger = logger
        self.ui_queue = ui_queue
        self.on_unread_change = on_unread_change
        self.blacklist = blacklist
        self.sms_dir = app_dir() / cfg["sms_folder"]
        self.del_dir = app_dir() / cfg["notified_folder"]
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._unread_lock = threading.Lock()
        self._unread = 0
        # True while a popup is on screen waiting for user ack — used to
        # serialise popups (no stacking) and to wake the run loop the
        # moment the user clicks Open / Dismiss / ×.
        self._popup_pending = False
        # Snapshot of the currently-shown popup, exposed to the Relayer
        # via get_current_popup(). Set when tick() shows a popup,
        # cleared on ack. Read by the Relayer to decide when the 10 s
        # away-mode timer has elapsed and which SMS to forward.
        self._current_popup_lock = threading.Lock()
        self._current_popup: "dict | None" = None

    def _change_unread(self, delta: int):
        with self._unread_lock:
            self._unread = max(0, self._unread + delta)
            n = self._unread
        self.logger.info(f"unread count: {n} (delta {delta:+d})")
        if self.on_unread_change:
            try:
                self.on_unread_change(n)
            except Exception as e:
                self.logger.error(f"on_unread_change failed: {e}")

    def stop(self):
        self._stop.set()
        self._wake.set()

    def get_current_popup(self) -> "dict | None":
        """Snapshot of the popup currently on screen, or None. The
        Relayer polls this to decide when the per-popup grace window
        has elapsed and which SMS to forward."""
        with self._current_popup_lock:
            if self._current_popup is None:
                return None
            return dict(self._current_popup)

    def run(self):
        while not self._stop.is_set():
            try:
                shown = self.tick()
            except Exception as e:
                self.logger.error(f"notifier tick error: {e}", exc_info=True)
                shown = False
            if self._stop.is_set():
                break
            self._wake.clear()
            if shown:
                # A popup is on screen — wait until the user acks (Open /
                # Dismiss / ×). Acking sets self._wake immediately so the
                # next message appears without any extra interval delay.
                self._wake.wait()
            else:
                # Nothing shown (DND, empty queue, errors). Sleep for the
                # configured interval before re-checking.
                self._wake.wait(self.cfg["notification_interval_seconds"])

    def _silent_move(self, path: Path) -> Path | None:
        """Move a JSON straight to del_dir without showing a popup.
        Returns the new path on success, None on failure."""
        self.del_dir.mkdir(parents=True, exist_ok=True)
        target = self.del_dir / path.name
        n = 2
        while target.exists():
            target = self.del_dir / f"{path.stem}-d{n:02d}.json"
            n += 1
        try:
            shutil.move(str(path), str(target))
            return target
        except Exception as e:
            self.logger.error(
                f"notifier: move failed {path.name} -> "
                f"{self.del_dir.name}/: {e}"
            )
            return None

    def tick(self) -> bool:
        """Show one popup if conditions allow. Return True iff a popup
        is currently on screen waiting for user ack (either we just
        showed one, or one was already pending)."""
        if self._popup_pending:
            return True
        if not self.cfg.get("enable_notifications", True):
            return False
        if self.cfg.get("respect_dnd", True) and not accepts_notifications():
            self.logger.info("notifier: DND/focus-assist active, skipping")
            return False
        if not self.sms_dir.exists():
            return False

        # Walk the queue, silently dropping anything from a blacklisted
        # sender, until we find one to actually show.
        files = sorted(self.sms_dir.glob("*.json"))
        path = None
        data = None
        sender = None
        for candidate in files:
            try:
                cdata = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception as e:
                self.logger.error(
                    f"notifier: unreadable {candidate.name}: {e}"
                )
                continue
            csender = str(cdata.get("sender", "?"))
            if self.blacklist and self.blacklist.contains(csender):
                moved = self._silent_move(candidate)
                if moved is not None:
                    self.logger.info(
                        f"BLOCKED sender={csender}: silently moved "
                        f"{candidate.name} -> "
                        f"{self.del_dir.name}/{moved.name}"
                    )
                continue
            path, data, sender = candidate, cdata, csender
            break

        if path is None:
            return False

        body = str(data.get("body", ""))
        max_len = int(self.cfg.get("notification_body_max_chars", 250))
        shown_body = body
        if len(body) > max_len:
            shown_body = body[:max_len].rstrip() + "\n… (Open to view full)"

        sms_data = dict(data)
        sms_data["_source_filename"] = path.name

        # Leave the JSON in sms/ until the user acks. The Relayer needs
        # to find it under its original basename so that, if the user
        # is away for >10 s, this SMS gets forwarded via FTP+SMS like
        # any other queued message. The popup itself carries the parsed
        # data in memory, so a concurrent move (on ack) can't break it.
        with self._current_popup_lock:
            self._current_popup = {
                "filename": path.name,
                "data": sms_data,
                "shown_at": time.monotonic(),
                "acked": False,
            }
        self._popup_pending = True
        self._change_unread(+1)
        self.logger.info(f"notified sender={sender} (file={path.name})")

        def _release():
            self._popup_pending = False
            self._wake.set()

        def on_ack(action: str):
            # Popup itself is gone now — drop the unread counter either way.
            self._change_unread(-1)
            # Mark the popup acked so the Relayer's away-mode gating
            # stops applying. An in-flight relay for this same SMS is
            # allowed to complete (it's already past its critical
            # section), per design.
            with self._current_popup_lock:
                if self._current_popup is not None:
                    self._current_popup["acked"] = True
            # Now move the JSON to sms_del/. Was previously done before
            # the popup was shown; deferring it lets the Relayer locate
            # the file in sms/ during the popup window.
            target = self._silent_move(path)
            if target is not None:
                self.logger.info(
                    f"acked={action} sender={sender} -> "
                    f"{self.del_dir.name}/{target.name}"
                )
            if action == "block":
                if self.blacklist is not None:
                    try:
                        self.blacklist.add(sender)
                    except Exception as e:
                        self.logger.error(f"blacklist add failed: {e}")
            # Drop the current-popup snapshot now that the file is
            # moved and the user has decided what to do with it.
            with self._current_popup_lock:
                self._current_popup = None
            if action == "open":
                # Hold popup_pending=True until the detail modal is closed,
                # so the queue doesn't advance behind the user's back while
                # they're actively reading the SMS.
                self.ui_queue.put((
                    "show_sms_data",
                    {"data": sms_data, "on_close": _release},
                ))
                return
            # block & dismiss both fall through to releasing the queue.
            _release()

        self.ui_queue.put((
            "show_toast_popup",
            {"sender": sender, "body": shown_body, "on_ack": on_ack},
        ))
        return True


# ---------- relayer (FTP forward + outbound SMS) ----------

class Relayer:
    """Forwards SMS to a remote phone via the same modem when the user
    doesn't ack a popup within ``relay_timeout_seconds``.

    For each SMS to relay:

      1. A small text file is written to ``relay_temp_folder`` with the
         sender, the original received-at timestamp, and the body.
      2. That file is FTP-uploaded to ``ftp_remote_dir`` on
         ``ftp_host``.
      3. A short outbound SMS containing only the resulting URL
         (``relay_url_base`` + remote filename) is sent to
         ``relay_sms_country_code`` + ``relay_sms_number`` via the
         modem panel's ``sendMsg`` action.
      4. On full success the local temp file is deleted.

    Scheduling is popup-anchored: the moment a popup is shown for a new
    SMS, a 10 s grace window arms. If the user acks (Dismiss / Block /
    Open) inside that window, no relay happens. If the window elapses
    without an ack, away-mode is on — the popup file is forwarded, then
    one more SMS per ``relay_interval_seconds`` (queued, no popup of
    their own) until the user finally acks the open popup. Already-
    relayed SMS keep getting popups when their turn eventually comes;
    a fresh relay state entry with ``sent_at`` short-circuits the
    second relay.

    DND, full-screen, presentation mode → entire relay path pauses.
    Blacklisted senders are skipped (Notifier's existing path either
    pre-routes them to ``sms_del/`` at fetch time or silently moves
    them on its next tick).

    Failure budget: only FTP failures count, since the FTP server can
    IP-ban after too many bad attempts. Modem-send failures retry
    indefinitely (the modem doesn't ban). 10 FTP failures within an
    hour pause the relay path for ``relay_pause_minutes_after_limit``;
    a successful relay clears the budget.

    Outbox cleanup: ``outbox_cleanup_after_minutes`` after the most
    recent outbound SMS (waiting for delivery), the modem's outbox is
    bulk-deleted so the SIM doesn't fill up.

    FTP cleanup: every ``ftp_cleanup_interval_hours``, files older
    than ``ftp_retention_hours`` on the FTP server are deleted via
    MLSD-listed mtimes (server clock is UTC).

    sms_del cleanup: every 6 h, locally-stored already-shown SMS older
    than ``sms_del_retention_days`` are removed from disk. Relay state
    entries whose source basename is no longer present in either sms/
    or sms_del/ are pruned in the same pass."""

    def __init__(self, cfg: dict, logger: logging.Logger,
                 modem: ModemClient, notifier: "Notifier",
                 blacklist: "Blacklist | None" = None,
                 on_relay_state_change=None):
        self.cfg = cfg
        self.logger = logger
        self.modem = modem
        self.notifier = notifier
        self.blacklist = blacklist
        self.on_relay_state_change = on_relay_state_change

        self.app = app_dir()
        self.sms_dir = self.app / cfg["sms_folder"]
        self.del_dir = self.app / cfg["notified_folder"]
        self.temp_dir = self.app / cfg["relay_temp_folder"]
        self.state_path = self.app / cfg["relay_state_file"]

        self.timeout_s = int(cfg.get("relay_timeout_seconds", 10))
        self.interval_s = int(cfg.get("relay_interval_seconds", 60))
        self.failure_limit = int(cfg.get("relay_failure_limit_per_hour", 10))
        self.pause_minutes = int(cfg.get("relay_pause_minutes_after_limit", 60))
        self.outbox_after_min = int(cfg.get("outbox_cleanup_after_minutes", 5))
        self.ftp_cleanup_h = int(cfg.get("ftp_cleanup_interval_hours", 6))
        self.ftp_retention_h = int(cfg.get("ftp_retention_hours", 24))
        self.sms_del_retention_d = int(cfg.get("sms_del_retention_days", 30))

        self.last_relay_at: "float | None" = None
        self.last_ftp_cleanup_at: "float | None" = None
        self.last_sms_del_cleanup_at: "float | None" = None

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception as e:
                self.logger.error(f"relay state load failed: {e}")
        return {
            "relayed": {},
            "failures": [],
            "pause_until": None,
            "last_outgoing_sms_at": None,
            "outbox_cleaned": True,
        }

    def _save_state(self):
        try:
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as e:
            self.logger.error(f"relay state save failed: {e}")

    def stop(self):
        self._stop.set()
        self._wake.set()

    def trigger_now(self):
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.logger.error(f"relayer tick error: {e}", exc_info=True)
            self._wake.clear()
            # 1 s tick keeps the per-popup 10 s grace timer accurate
            # without spinning. Most ticks are no-ops.
            self._wake.wait(1.0)

    # --- failure budget ---

    def _is_paused(self) -> bool:
        pause_iso = self._state.get("pause_until")
        if not pause_iso:
            return False
        try:
            pause_until = datetime.fromisoformat(pause_iso)
        except Exception:
            self._state["pause_until"] = None
            self._save_state()
            return False
        if datetime.now() >= pause_until:
            self.logger.info(
                "relay: pause window elapsed, resetting failure budget"
            )
            self._state["pause_until"] = None
            self._state["failures"] = []
            self._save_state()
            return False
        return True

    def _record_ftp_failure(self, why: str):
        """Append a failure timestamp; if we've hit the per-hour limit,
        arm the pause window. Only FTP failures land here — modem-send
        failures don't accumulate (the modem doesn't IP-ban)."""
        ts = datetime.now()
        failures = self._state.setdefault("failures", [])
        failures.append(ts.isoformat())
        cutoff = ts - timedelta(hours=1)
        kept = []
        for f in failures:
            try:
                if datetime.fromisoformat(f) >= cutoff:
                    kept.append(f)
            except Exception:
                continue
        self._state["failures"] = kept
        if len(kept) >= self.failure_limit:
            pause_until = ts + timedelta(minutes=self.pause_minutes)
            self._state["pause_until"] = pause_until.isoformat()
            self.logger.warning(
                f"relay: FTP failure budget hit "
                f"({len(kept)}/{self.failure_limit} in last hour) — "
                f"pausing relay until "
                f"{pause_until.isoformat(timespec='seconds')}: {why}"
            )
        else:
            self.logger.warning(
                f"relay: FTP failure {len(kept)}/{self.failure_limit} "
                f"in last hour: {why}"
            )
        self._save_state()

    def _record_relay_success(self):
        if self._state.get("failures") or self._state.get("pause_until"):
            self._state["failures"] = []
            self._state["pause_until"] = None
            self._save_state()

    # --- main scheduler ---

    def tick(self):
        # Maintenance jobs run independently of the popup-anchored relay
        # gating: they should still happen during DND or when no popup
        # is on screen.
        self._maybe_cleanup_outbox()
        self._maybe_cleanup_ftp()
        self._maybe_cleanup_sms_del()

        if not self.cfg.get("relay_enabled", True):
            return
        if self._is_paused():
            return
        if self.cfg.get("respect_dnd", True) and not accepts_notifications():
            return

        popup = self.notifier.get_current_popup() if self.notifier else None
        if popup is None or popup.get("acked"):
            return

        if (time.monotonic() - popup["shown_at"]) < self.timeout_s:
            return

        if self.last_relay_at is not None:
            since = time.monotonic() - self.last_relay_at
            if since < self.interval_s:
                return

        relayed = self._state.setdefault("relayed", {})
        target_basename: "str | None" = None
        target_data: "dict | None" = None

        # 1) The popup file itself if not yet fully relayed.
        popup_entry = relayed.get(popup["filename"])
        if not popup_entry or "sent_at" not in popup_entry:
            target_basename = popup["filename"]
            target_data = popup["data"]
        else:
            # 2) Oldest queued sms/ file that's not blacklisted and
            #    not yet fully relayed.
            try:
                files = sorted(self.sms_dir.glob("*.json"))
            except Exception:
                files = []
            for f in files:
                if f.name == popup["filename"]:
                    continue
                entry = relayed.get(f.name)
                if entry and "sent_at" in entry:
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception as e:
                    self.logger.error(f"relay: unreadable {f.name}: {e}")
                    continue
                sender = str(data.get("sender", ""))
                if self.blacklist and self.blacklist.contains(sender):
                    continue
                target_basename = f.name
                target_data = data
                break

        if target_basename is None or target_data is None:
            return

        self._relay_one(target_basename, target_data)

    # --- single-SMS relay (idempotent across restart via state file) ---

    def _relay_one(self, basename: str, data: dict):
        self._set_relaying(True)
        try:
            relayed = self._state.setdefault("relayed", {})
            entry = relayed.get(basename, {})

            if "uploaded_at" not in entry:
                entry = self._do_upload(basename, data)
                if entry is None:
                    # Failure already recorded + last_relay_at set.
                    return
                relayed[basename] = entry
                self._save_state()

            self.last_relay_at = time.monotonic()

            if "sent_at" not in entry:
                self._do_send(basename, entry)
        finally:
            self._set_relaying(False)

    def _do_upload(self, basename: str, data: dict) -> "dict | None":
        sender = str(data.get("sender", "?"))
        received_at = str(data.get("received_at", ""))
        body = str(data.get("body", ""))
        # HTML lets us declare charset=utf-8 right in the document, so
        # the browser doesn't guess the encoding from the HTTP headers
        # (Apache/nginx serving plain .txt often advertises ISO-8859-1
        # or no charset, which mangles Persian / other non-Latin text).
        # The body uses dir="auto" so RTL Persian and LTR English render
        # correctly, and white-space: pre-wrap preserves the SMS line
        # breaks. html.escape() neutralises any &/</> in the SMS body.
        content = (
            "<!doctype html>\n"
            "<html lang=\"fa\">\n"
            "<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\">\n"
            f"<title>SMS — {html.escape(sender)}</title>\n"
            "<style>\n"
            "body{font-family:Tahoma,Segoe UI,Arial,sans-serif;"
            "max-width:720px;margin:24px auto;padding:0 16px;color:#222}\n"
            ".meta{color:#666;font-size:13px;line-height:1.6;"
            "border-bottom:1px solid #eee;padding-bottom:12px;"
            "margin-bottom:16px}\n"
            ".meta b{color:#222}\n"
            ".body{white-space:pre-wrap;word-wrap:break-word;"
            "font-size:16px;line-height:1.7}\n"
            "</style>\n"
            "</head>\n"
            "<body>\n"
            "<div class=\"meta\">\n"
            f"<b>From:</b> {html.escape(sender)}<br>\n"
            f"<b>Date:</b> {html.escape(received_at)}\n"
            "</div>\n"
            f"<div class=\"body\" dir=\"auto\">{html.escape(body)}</div>\n"
            "</body>\n"
            "</html>\n"
        )

        ts = datetime.now()
        stamp = ts.strftime("%Y%m%d-%H%M%S")
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._record_ftp_failure(f"temp dir create: {e}")
            self.last_relay_at = time.monotonic()
            return None

        remote_name = f"{stamp}.htm"
        local_path = self.temp_dir / remote_name
        n = 2
        while local_path.exists():
            remote_name = f"{stamp}-{n:02d}.htm"
            local_path = self.temp_dir / remote_name
            n += 1

        # Write the file as UTF-8 bytes explicitly (no BOM, no
        # platform-default encoding), so the FTP binary upload
        # transfers the exact same bytes the browser will read.
        try:
            local_path.write_bytes(content.encode("utf-8"))
        except Exception as e:
            self._record_ftp_failure(f"temp write {local_path.name}: {e}")
            self.last_relay_at = time.monotonic()
            return None

        try:
            self._ftp_upload(local_path, remote_name)
        except Exception as e:
            self._record_ftp_failure(f"FTP {basename}: {e}")
            self.last_relay_at = time.monotonic()
            return None

        url = self.cfg["relay_url_base"].rstrip("/") + "/" + remote_name
        entry = {
            "uploaded_at": ts.isoformat(timespec="seconds"),
            "remote_filename": remote_name,
            "local_filename": local_path.name,
            "url": url,
            "source_basename": basename,
        }
        self.logger.info(f"relay: uploaded {basename} -> {url}")
        return entry

    def _do_send(self, basename: str, entry: dict):
        try:
            self.modem.send_sms(
                country_code=str(self.cfg["relay_sms_country_code"]),
                number=str(self.cfg["relay_sms_number"]),
                content=entry["url"],
            )
        except Exception as e:
            # Modem failures don't accumulate — retry next tick. We do
            # bump last_relay_at so we don't hammer the modem faster
            # than the configured interval.
            self.logger.warning(
                f"relay: modem send failed for {basename}: {e} (will retry)"
            )
            return

        sent_iso = datetime.now().isoformat(timespec="seconds")
        entry["sent_at"] = sent_iso
        self._state["last_outgoing_sms_at"] = sent_iso
        self._state["outbox_cleaned"] = False
        self._save_state()
        self.logger.info(f"relay: sent SMS for {basename} -> {entry['url']}")

        local_filename = entry.get("local_filename")
        if local_filename:
            local_path = self.temp_dir / local_filename
            try:
                if local_path.exists():
                    local_path.unlink()
            except Exception as e:
                self.logger.warning(
                    f"relay: temp delete {local_filename}: {e}"
                )

        self._record_relay_success()

    # --- FTP ---

    def _ftp_open(self) -> ftplib.FTP:
        host = str(self.cfg["ftp_host"])
        port = int(self.cfg.get("ftp_port", 21))
        user = str(self.cfg["ftp_user"])
        pwd = str(self.cfg["ftp_pass"])
        timeout = int(self.cfg.get("request_timeout_seconds", 15))
        ftp = ftplib.FTP(timeout=timeout)
        ftp.connect(host, port)
        ftp.login(user, pwd)
        ftp.cwd(str(self.cfg["ftp_remote_dir"]))
        return ftp

    def _ftp_upload(self, local_path: Path, remote_name: str):
        ftp = self._ftp_open()
        try:
            with local_path.open("rb") as f:
                ftp.storbinary(f"STOR {remote_name}", f)
        finally:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    # --- maintenance ---

    def _maybe_cleanup_outbox(self):
        last_iso = self._state.get("last_outgoing_sms_at")
        if not last_iso:
            return
        if self._state.get("outbox_cleaned", True):
            return
        try:
            last_at = datetime.fromisoformat(last_iso)
        except Exception:
            self._state["outbox_cleaned"] = True
            self._save_state()
            return
        if (datetime.now() - last_at).total_seconds() < self.outbox_after_min * 60:
            return

        try:
            records = self.modem.list_outbox()
        except Exception as e:
            self.logger.warning(f"relay: list outbox failed (will retry): {e}")
            return

        indices = [r["index"] for r in records if r.get("index")]
        if indices:
            try:
                self.modem.delete_outbox(",".join(indices))
                self.logger.info(
                    f"relay: cleaned modem outbox ({len(indices)} entries)"
                )
            except Exception as e:
                self.logger.warning(f"relay: delete outbox failed: {e}")
                return
        else:
            self.logger.info("relay: outbox already empty at cleanup time")

        self._state["outbox_cleaned"] = True
        self._save_state()

    def _maybe_cleanup_ftp(self):
        now = time.monotonic()
        if (self.last_ftp_cleanup_at is not None
                and now - self.last_ftp_cleanup_at < self.ftp_cleanup_h * 3600):
            return
        # Stamp before attempting so a hanging cleanup doesn't
        # immediately retry next tick.
        self.last_ftp_cleanup_at = now
        try:
            self._ftp_purge_old()
        except Exception as e:
            self.logger.warning(f"relay: FTP cleanup failed: {e}")

    def _ftp_purge_old(self):
        # ProFTPD MLSD's `modify` field is in UTC.
        cutoff = datetime.utcnow() - timedelta(hours=self.ftp_retention_h)
        deleted = 0
        ftp = self._ftp_open()
        try:
            entries = list(ftp.mlsd())
            for name, facts in entries:
                if facts.get("type") not in (None, "file"):
                    continue
                modify = facts.get("modify")
                if not modify:
                    continue
                try:
                    mtime = datetime.strptime(modify, "%Y%m%d%H%M%S")
                except ValueError:
                    continue
                if mtime < cutoff:
                    try:
                        ftp.delete(name)
                        deleted += 1
                    except Exception as e:
                        self.logger.warning(
                            f"relay: FTP delete {name} failed: {e}"
                        )
        finally:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass
        if deleted:
            self.logger.info(
                f"relay: FTP cleanup deleted {deleted} file(s) older "
                f"than {self.ftp_retention_h}h"
            )

    def _maybe_cleanup_sms_del(self):
        now = time.monotonic()
        if (self.last_sms_del_cleanup_at is not None
                and now - self.last_sms_del_cleanup_at < 6 * 3600):
            return
        self.last_sms_del_cleanup_at = now
        retention_d = self.sms_del_retention_d
        cutoff = time.time() - retention_d * 86400
        deleted = 0
        if self.del_dir.exists():
            for f in self.del_dir.iterdir():
                if not f.is_file():
                    continue
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        deleted += 1
                except Exception as e:
                    self.logger.warning(
                        f"relay: sms_del cleanup {f.name}: {e}"
                    )
        if deleted:
            self.logger.info(
                f"relay: sms_del cleanup deleted {deleted} file(s) "
                f"older than {retention_d}d"
            )
        self._prune_relay_state()

    def _prune_relay_state(self):
        """Drop relay state entries whose source basename is no longer
        present in either sms/ or sms_del/. Prevents the state file
        from growing forever."""
        relayed = self._state.get("relayed", {})
        if not relayed:
            return
        live: set[str] = set()
        try:
            live.update(p.name for p in self.sms_dir.glob("*.json"))
        except Exception:
            pass
        try:
            live.update(p.name for p in self.del_dir.glob("*.json"))
        except Exception:
            pass
        # _silent_move appends `-d02`, `-d03`, … on collision in
        # sms_del/, so match by exact stem or the -dNN suffix variant.
        live_stems = {p.rsplit(".", 1)[0] for p in live}
        before = len(relayed)
        kept = {}
        for basename, entry in relayed.items():
            stem = basename.rsplit(".", 1)[0]
            if stem in live_stems or any(
                ls.startswith(stem + "-d") for ls in live_stems
            ):
                kept[basename] = entry
        if len(kept) != before:
            self._state["relayed"] = kept
            self._save_state()
            self.logger.info(
                f"relay: pruned {before - len(kept)} stale state entries"
            )

    # --- tray icon hook ---

    def _set_relaying(self, on: bool):
        if self.on_relay_state_change:
            try:
                self.on_relay_state_change(on)
            except Exception as e:
                self.logger.error(f"relay state change cb failed: {e}")


# ---------- LTE usage tracker ----------

def fmt_bytes(n: float) -> str:
    """Format byte count: '123 B' / '12 KB' / '34 MB' / '1.23 GB' / '4.56 TB'.
    B/KB/MB rounded to integer; GB and above with 2 decimals."""
    n = max(0, int(n))
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{int(round(kb))} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{int(round(mb))} MB"
    gb = mb / 1024
    if gb < 1024:
        return f"{gb:.2f} GB"
    tb = gb / 1024
    return f"{tb:.2f} TB"


def _delta_with_reset(last: int, current: int) -> int:
    """Return delta accounting for counter resets. If current dropped
    below last, the modem rebooted and the new counter is the increment
    since the reset (best estimate without knowing the reset moment)."""
    if current >= last:
        return current - last
    return current


_STATUS_RANK = {"accurate": 0, "average": 1, "incomplete": 2}


def _worst_status(*statuses: str) -> str:
    return max(statuses, key=lambda s: _STATUS_RANK.get(s, 0))


class UsageTracker:
    """Polls the modem's stats pages on an interval, tracks per-day LTE
    send/receive totals (overall and per client IP), persists state across
    restarts, and writes one .txt per day per resource.

    The day boundary is local midnight. If the app misses one or more
    midnights (e.g. PC was off), the cumulative delta is split across the
    affected days proportionally to elapsed seconds — affected days are
    flagged ``calc-status=average`` (or ``incomplete`` if a whole day had
    no ticks at all). Counter resets (current < last) are detected and
    counted as fresh increments since the unknown reset moment, also
    promoting the day's status to ``average``."""

    def __init__(self, cfg: dict, logger: logging.Logger,
                 modem: ModemClient):
        self.cfg = cfg
        self.logger = logger
        self.modem = modem
        self.app = app_dir()
        self.state_path = self.app / cfg["usage_state_file"]
        self.total_dir = self.app / cfg["usage_total_folder"]
        self.clients_dir = self.app / cfg["usage_clients_folder"]
        self.name_ttl = int(cfg.get("client_name_refresh_seconds", 3600))
        self.state = self._load_state()
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception as e:
                self.logger.error(f"usage state load failed: {e}")
        return {
            "last_check_at": None,
            "last_lte": {"tx": 0, "rx": 0},
            "last_per_ip": {},
            "today": None,
            "client_names": {},
        }

    def _save_state(self):
        try:
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as e:
            self.logger.error(f"usage state save failed: {e}")

    def run(self):
        # Wake on min(interval, time-until-just-after-midnight) so the
        # very first tick after a day boundary fires within ~1 s of
        # midnight, even if the configured interval is 5 / 15 / 30 min.
        # That keeps the rollover finalization clean instead of up to
        # one interval late.
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.logger.error(f"usage tick error: {e}", exc_info=True)
            self._wake.clear()
            self._wake.wait(self._next_wake_seconds())

    def _next_wake_seconds(self) -> float:
        interval = float(self.cfg["usage_interval_seconds"])
        now = datetime.now()
        midnight = datetime.combine(now.date() + timedelta(days=1), dtime.min)
        until_midnight = (midnight - now).total_seconds() + 1.0
        return max(1.0, min(interval, until_midnight))

    def tick(self):
        now = datetime.now()
        try:
            lte = self.modem.get_lte_total()
            per_ip = self.modem.get_per_ip_traffic()
        except Exception as e:
            self.logger.warning(f"usage: fetch failed: {e}")
            return

        last_at = self.state.get("last_check_at")
        if last_at is None:
            # First run ever — seed state, no delta yet.
            self.logger.info(
                f"usage: first run baseline lte_tx={lte['tx']} "
                f"lte_rx={lte['rx']} clients={len(per_ip)}"
            )
            self.state["last_check_at"] = now.isoformat()
            self.state["last_lte"] = lte
            self.state["last_per_ip"] = per_ip
            self.state["today"] = self._fresh_day(now)
            self._refresh_names(per_ip.keys(), now)
            self._save_state()
            self._write_running_day(self.state["today"])
            return

        try:
            last_check = datetime.fromisoformat(last_at)
        except Exception:
            last_check = now
        last_lte = self.state.get("last_lte", {"tx": 0, "rx": 0})
        last_per_ip = self.state.get("last_per_ip", {})

        # Total deltas since last tick (whole gap, regardless of day count).
        d_lte_tx = _delta_with_reset(last_lte.get("tx", 0), lte["tx"])
        d_lte_rx = _delta_with_reset(last_lte.get("rx", 0), lte["rx"])
        reset = (lte["tx"] < last_lte.get("tx", 0)
                 or lte["rx"] < last_lte.get("rx", 0))

        d_per_ip: dict[str, dict[str, int]] = {}
        for ip in set(per_ip.keys()) | set(last_per_ip.keys()):
            last = last_per_ip.get(ip, {"tx": 0, "rx": 0})
            cur = per_ip.get(ip, {"tx": 0, "rx": 0})
            d_per_ip[ip] = {
                "tx": _delta_with_reset(last.get("tx", 0), cur.get("tx", 0)),
                "rx": _delta_with_reset(last.get("rx", 0), cur.get("rx", 0)),
            }
            if (cur.get("tx", 0) < last.get("tx", 0)
                    or cur.get("rx", 0) < last.get("rx", 0)):
                reset = True

        if reset:
            self.logger.warning(
                "usage: counter decrease detected (modem reset?) — "
                "today flagged calc-status=average"
            )

        last_date = last_check.date()
        today_date = now.date()

        if last_date == today_date:
            today = self.state.get("today")
            if today is None or today.get("date") != today_date.strftime("%Y%m%d"):
                today = self._fresh_day(now)
                self.state["today"] = today
            self._accumulate(today, d_lte_tx, d_lte_rx, d_per_ip)
            if reset:
                today["calc_status"] = _worst_status(
                    today.get("calc_status", "accurate"), "average"
                )
        else:
            self._handle_rollover(
                now, last_check, d_lte_tx, d_lte_rx, d_per_ip, reset
            )

        self._refresh_names(per_ip.keys(), now)

        self.state["last_check_at"] = now.isoformat()
        self.state["last_lte"] = lte
        self.state["last_per_ip"] = per_ip
        self._save_state()
        if self.state.get("today"):
            self._write_running_day(self.state["today"])

    def _handle_rollover(self, now: datetime, last_check: datetime,
                         d_lte_tx: int, d_lte_rx: int,
                         d_per_ip: dict, reset: bool):
        """Day boundary crossed at least once since last tick. Distribute
        the cumulative delta across each affected day proportionally to
        the elapsed seconds in that day."""
        last_date = last_check.date()
        today_date = now.date()
        days = []
        d = last_date
        while d <= today_date:
            days.append(d)
            d = d + timedelta(days=1)

        # Per-day elapsed seconds in the gap
        seconds = []
        for i, day in enumerate(days):
            if i == 0:
                start = last_check
                end = datetime.combine(day, dtime.max)
            elif i == len(days) - 1:
                start = datetime.combine(day, dtime.min)
                end = now
            else:
                start = datetime.combine(day, dtime.min)
                end = datetime.combine(day, dtime.max)
            seconds.append(max(0.0, (end - start).total_seconds()))
        total_sec = sum(seconds) or 1.0

        for i, day in enumerate(days):
            share = seconds[i] / total_sec
            day_str = day.strftime("%Y%m%d")
            day_tx = d_lte_tx * share
            day_rx = d_lte_rx * share
            day_clients = {
                ip: {
                    "tx": v["tx"] * share,
                    "rx": v["rx"] * share,
                }
                for ip, v in d_per_ip.items()
            }

            if i == 0:
                # Old "today" — finalize.
                today = self.state.get("today")
                if today is None or today.get("date") != day_str:
                    today = {
                        "date": day_str,
                        "lte": {"send": 0, "receive": 0},
                        "clients": {},
                        "calc_status": "accurate",
                    }
                self._accumulate(today, day_tx, day_rx, day_clients)
                today["calc_status"] = _worst_status(
                    today.get("calc_status", "accurate"), "average"
                )
                self._finalize_day(today)
                self.logger.info(
                    f"usage: finalized {day_str} "
                    f"calc-status={today['calc_status']}"
                )
            elif i == len(days) - 1:
                new_today = {
                    "date": day_str,
                    "lte": {"send": day_tx, "receive": day_rx},
                    "clients": {
                        ip: {"send": v["tx"], "receive": v["rx"]}
                        for ip, v in day_clients.items()
                    },
                    "calc_status": "average",
                }
                self.state["today"] = new_today
            else:
                # Whole intermediate day with zero ticks → incomplete.
                day_data = {
                    "date": day_str,
                    "lte": {"send": day_tx, "receive": day_rx},
                    "clients": {
                        ip: {"send": v["tx"], "receive": v["rx"]}
                        for ip, v in day_clients.items()
                    },
                    "calc_status": "incomplete",
                }
                self._finalize_day(day_data)
                self.logger.info(
                    f"usage: filled {day_str} (no live ticks) "
                    f"calc-status=incomplete"
                )

    def _accumulate(self, day: dict, d_lte_tx: float, d_lte_rx: float,
                    d_per_ip: dict):
        day["lte"]["send"] += d_lte_tx
        day["lte"]["receive"] += d_lte_rx
        for ip, v in d_per_ip.items():
            cur = day["clients"].setdefault(ip, {"send": 0, "receive": 0})
            cur["send"] += v["tx"]
            cur["receive"] += v["rx"]

    def _fresh_day(self, now: datetime) -> dict:
        return {
            "date": now.strftime("%Y%m%d"),
            "lte": {"send": 0, "receive": 0},
            "clients": {},
            "calc_status": "accurate",
        }

    def _refresh_names(self, ips, now: datetime):
        cache = self.state.setdefault("client_names", {})
        ttl = self.name_ttl
        # Use a short DNS timeout so the tick doesn't hang on a slow
        # resolver.
        prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(0.5)
        try:
            for ip in ips:
                entry = cache.get(ip)
                stale = True
                if entry and entry.get("resolved_at"):
                    try:
                        age = (
                            now - datetime.fromisoformat(entry["resolved_at"])
                        ).total_seconds()
                        stale = age > ttl
                    except Exception:
                        stale = True
                if not stale:
                    continue
                names: list[str] = []
                try:
                    hostname, aliaslist, _ = socket.gethostbyaddr(ip)
                    names = [n for n in [hostname] + list(aliaslist) if n]
                except Exception:
                    names = []
                cache[ip] = {
                    "names": names,
                    "resolved_at": now.isoformat(),
                }
        finally:
            socket.setdefaulttimeout(prev)

    def _write_running_day(self, day: dict):
        """Overwrite a day's files with running totals only — no
        ``calc-status`` line. Used for the still-in-progress current day."""
        self._write_lines(day, include_status=False)

    def _finalize_day(self, day: dict):
        """Overwrite a day's files with running totals AND a ``calc-status``
        line. Used when the day is closed (rollover crossed it) or when
        backfilling whole missed days."""
        self._write_lines(day, include_status=True)

    def _write_lines(self, day: dict, include_status: bool):
        date_str = day["date"]
        send = int(round(day["lte"]["send"]))
        receive = int(round(day["lte"]["receive"]))
        total = send + receive
        status = day.get("calc_status", "accurate")

        self.total_dir.mkdir(parents=True, exist_ok=True)
        total_path = self.total_dir / f"total_{date_str}.txt"
        lines = [
            f"send={fmt_bytes(send)}",
            f"receive={fmt_bytes(receive)}",
            f"total={fmt_bytes(total)}",
        ]
        if include_status:
            lines.append(f"calc-status={status}")
        try:
            total_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as e:
            self.logger.error(f"write {total_path.name} failed: {e}")

        self.clients_dir.mkdir(parents=True, exist_ok=True)
        names_cache = self.state.get("client_names", {})
        summary_rows = []
        for ip, v in day.get("clients", {}).items():
            send_c = int(round(v["send"]))
            receive_c = int(round(v["receive"]))
            total_c = send_c + receive_c
            ip_dash = ip.replace(".", "-")
            f = self.clients_dir / f"{ip_dash}_{date_str}.txt"
            row = [
                f"send={fmt_bytes(send_c)}",
                f"receive={fmt_bytes(receive_c)}",
                f"total={fmt_bytes(total_c)}",
            ]
            if include_status:
                row.append(f"calc-status={status}")
            entry = names_cache.get(ip, {})
            names = sorted({n for n in entry.get("names", []) if n})
            if names:
                row.append(f"names={','.join(names)}")
            try:
                f.write_text("\n".join(row) + "\n", encoding="utf-8")
            except Exception as e:
                self.logger.error(f"write {f.name} failed: {e}")
            if total_c > 0:
                summary_rows.append(
                    (ip, total_c, send_c, receive_c, ",".join(names))
                )

        # Daily summary of all non-zero clients, heaviest users first.
        # Space-padded for monospace-font readability — each column is
        # left-aligned to its widest cell and separated by 5 spaces.
        summary_rows.sort(key=lambda r: -r[1])
        summary_path = self.total_dir / f"clients-total_{date_str}.txt"
        table = [["ip", "total", "send", "receive", "names"]]
        for ip, total_c, send_c, receive_c, names_str in summary_rows:
            table.append([
                ip,
                fmt_bytes(total_c),
                fmt_bytes(send_c),
                fmt_bytes(receive_c),
                names_str,
            ])
        col_widths = [
            max(len(row[i]) for row in table) for i in range(len(table[0]))
        ]
        gap = "     "  # 5 spaces between columns
        summary_lines = []
        for row in table:
            line = gap.join(
                cell.ljust(col_widths[i]) for i, cell in enumerate(row)
            )
            summary_lines.append(line.rstrip())   # no trailing pad
        try:
            summary_path.write_text(
                "\n".join(summary_lines) + "\n", encoding="utf-8"
            )
        except Exception as e:
            self.logger.error(f"write {summary_path.name} failed: {e}")


# ---------- Telina hosted-PBX call-list watcher ----------

class TelinaWatcher:
    """Polls Telina hosted-PBX for the 5 most-recent CDR rows, diffs against
    a saved snapshot, and SMSes the caller numbers of any new calls via
    the modem.

    The flow is three plain HTTP calls — login + nupGetMyApp on the hub
    GraphQL, then a tRPC GET on the PBX panel — none of which require
    going through the browser UI. See readme.md for the reverse-
    engineered details. Token + appId are cached across ticks; on any
    auth failure we drop them and re-login on the next attempt.

    First-run behavior: if the state file is absent we write the current
    snapshot silently and send no SMS, otherwise the first tick after a
    fresh install would treat all 5 rows as new and fire on startup."""

    def __init__(self, cfg: dict, logger: logging.Logger,
                 modem: ModemClient):
        self.cfg = cfg
        self.logger = logger
        self.modem = modem
        self.app = app_dir()
        self.state_path = self.app / cfg["telina_state_file"]
        self.timeout = float(cfg.get("request_timeout_seconds", 15))
        self.session = requests.Session()
        self.session.trust_env = False
        self._token: "str | None" = None
        self._user_id: "str | None" = None
        self._app_id: "str | None" = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.logger.error(f"telina tick error: {e}", exc_info=True)
            self._wake.clear()
            self._wake.wait(float(self.cfg["telina_interval_seconds"]))

    # ---- HTTP layer ----

    def _gql(self, query: str, variables: dict, headers: dict) -> dict:
        body = {"query": query, "variables": variables}
        r = self.session.post(
            TELINA_API_URL, json=body,
            headers={"Content-Type": "application/json", **headers},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"GraphQL errors: {data['errors']}")
        return data["data"]

    def _login(self):
        # `signin` takes an `identity` field (not `email`) plus the
        # reseller domain — the API rejects requests without `domain`
        # since multiple reseller fronts share the same backend.
        q = (
            "query login($input: SigninInput) { "
            "signin(input: $input) { token user { id } } }"
        )
        data = self._gql(q, {"input": {
            "identity": self.cfg["telina_username"],
            "password": self.cfg["telina_password"],
            "domain": TELINA_LOGIN_DOMAIN,
        }}, headers={})
        self._token = data["signin"]["token"]
        self._user_id = data["signin"]["user"]["id"]
        self.logger.info("telina: logged in")

    def _resolve_app_id(self):
        # The hub uses the literal string "app-selector" as APP-ID before
        # the user has picked an app — that's the only header value that
        # works for nupGetMyApp.
        q = (
            "query nupGetMyApp($userId: String) { "
            "nupGetMyApp(userId: $userId) { userApps { app { id } } } }"
        )
        data = self._gql(q, {"userId": self._user_id}, headers={
            "X-APIKEY": self._token,
            "APP-ID": "app-selector",
            "USER-ID": self._user_id,
        })
        apps = (data.get("nupGetMyApp") or {}).get("userApps") or []
        if not apps:
            raise RuntimeError("telina: account has no apps")
        self._app_id = apps[0]["app"]["id"]
        self.logger.info(f"telina: resolved appId={self._app_id}")

    def _fetch_recent_calls(self) -> list:
        # tRPC's httpLink expects the input as a URL-encoded JSON query
        # parameter on a GET. The handler destructures `filters`, so the
        # date range is required even when we don't care about it — we
        # pass a wide window (last 30 days, +1 day forward) and let the
        # server's _id-desc sort do the work.
        now = datetime.utcnow()
        from_iso = (now - timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        to_iso = (now + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.999Z"
        )
        payload = {
            "pagination": {"limit": 5, "page": 1},
            "sort": {"sortBy": "_id", "sortOrder": "desc"},
            "filters": {
                "fromDate": from_iso,
                "toDate": to_iso,
                "groupBy": "", "dst": "", "src": "", "type": "",
                "disposition": "", "did": "", "useLike": False,
            },
        }
        encoded = urllib.parse.quote(json.dumps(payload, separators=(",", ":")))
        url = (
            f"{TELINA_PBX_URL}/api/trpc/report.advanced.getAdvancedSystem"
            f"?input={encoded}"
        )
        r = self.session.get(url, headers={
            "x-apikey": self._token,
            "app-id": self._app_id,
            "user-id": self._user_id,
            "from-support-menu": "no",
        }, timeout=self.timeout)
        if r.status_code == 401:
            # Drop cached creds; next tick will re-login.
            raise PermissionError("telina: 401 (token rejected)")
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"telina trpc error: {data['error']}")
        return ((data.get("result") or {}).get("data") or {}).get(
            "results"
        ) or []

    # ---- state ----

    def _load_seen_cuids(self) -> "set[str] | None":
        # Returns None when the file is absent — the caller treats that
        # as a first run and skips the SMS step.
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(c) for c in data.get("cuids") or []}
        except Exception as e:
            self.logger.error(f"telina state load failed: {e}")
            # Treat a corrupt file as "no state" rather than firing 5
            # unwanted SMSes — safer to suppress one tick.
            return set()

    def _save_state(self, calls: list):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"cuids": [c.get("cuid") for c in calls if c.get("cuid")]},
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as e:
            self.logger.error(f"telina state save failed: {e}")

    # ---- tick ----

    def tick(self):
        # Lazy login + appId resolution. On 401 mid-run we drop the
        # cached creds and re-acquire them once before giving up for
        # this tick.
        for attempt in (1, 2):
            try:
                if not self._token or not self._user_id:
                    self._login()
                if not self._app_id:
                    self._resolve_app_id()
                calls = self._fetch_recent_calls()
                break
            except PermissionError as e:
                self.logger.warning(f"{e}; retrying with fresh login")
                self._token = self._user_id = self._app_id = None
                if attempt == 2:
                    raise
        else:
            return

        self.logger.info(f"telina: fetched {len(calls)} recent call(s)")

        seen = self._load_seen_cuids()
        if seen is None:
            # First run — seed the file and stay silent.
            self._save_state(calls)
            self.logger.info(
                "telina: first run, saved snapshot without sending SMS"
            )
            return

        new_calls = [c for c in calls if str(c.get("cuid", "")) not in seen]
        if not new_calls:
            self.logger.info("telina: no new calls")
            # Still rewrite state so old cuids don't accumulate forever.
            self._save_state(calls)
            return

        # Caller numbers, in the same order the API returned them
        # (newest first). `src` is the originator on incoming calls and
        # the local extension on outgoing — for this notification we
        # always use `src`, matching what the panel's table shows in
        # the caller column.
        numbers = [str(c.get("src", "")).strip() for c in new_calls]
        numbers = [n for n in numbers if n]
        if not numbers:
            self.logger.warning(
                "telina: new calls had no src field, skipping SMS"
            )
            self._save_state(calls)
            return

        body = "\n".join(numbers)
        cc = str(self.cfg["relay_sms_country_code"])
        # Iranian local format includes a leading 0 (e.g. "09111111111")
        # but with a country code that 0 has to be dropped — otherwise
        # the modem composes "+9809111111111" and the SMSC rejects it.
        local = str(self.cfg["telina_notif_number"]).strip()
        if cc and local.startswith("0"):
            local = local[1:]
        try:
            self.modem.send_sms(
                country_code=cc,
                number=local,
                content=body,
            )
            self.logger.info(
                f"telina: sent SMS with {len(numbers)} new caller(s) "
                f"to {self.cfg['telina_notif_number']}"
            )
        except Exception as e:
            self.logger.error(f"telina: send_sms failed: {e}", exc_info=True)
            # Don't update state — retry these calls on the next tick.
            return

        self._save_state(calls)


# ---------- tray icon image ----------

def make_icon_image(unread: int = 0, relaying: bool = False) -> Image.Image:
    """Black 'S' on a filled circle, transparent background. Three states:
    yellow while a relay (FTP upload + outbound SMS) is in flight, red
    while toasts are awaiting acknowledgement, green when idle. Yellow
    wins over red because it is transient and meaningful (the user is
    actively being forwarded an SMS via the modem)."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if relaying:
        circle_fill = (240, 200, 0, 255)
    elif unread > 0:
        circle_fill = (210, 30, 30, 255)
    else:
        circle_fill = (0, 170, 60, 255)
    d.ellipse((1, 1, size - 1, size - 1), fill=circle_fill)
    font = None
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(name, 48)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    text = "S"
    if hasattr(d, "textbbox"):
        bbox = d.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        ox = -bbox[0]
        oy = -bbox[1]
    else:
        tw, th = d.textsize(text, font=font)
        ox = oy = 0
    d.text(
        ((size - tw) / 2 + ox, (size - th) / 2 + oy - 2),
        text, fill=(0, 0, 0, 255), font=font,
    )
    return img


# ---------- log viewer ----------

def show_log_window(parent: tk.Tk, log_dir: Path):
    today = datetime.now().strftime("%Y%m%d")
    path = log_dir / f"{today}.log"
    try:
        content = (
            path.read_text(encoding="utf-8") if path.exists()
            else "(no log entries yet today)"
        )
    except Exception as e:
        content = f"(error reading log: {e})"

    win = tk.Toplevel(parent)
    win.title(f"smsFetcher — {datetime.now().strftime('%Y-%m-%d')} log")
    win.geometry("900x520")
    txt = scrolledtext.ScrolledText(
        win, wrap="none", font=("Consolas", 10),
    )
    txt.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    txt.insert("1.0", content)
    txt.config(state="disabled")
    txt.see("end")
    btn = tk.Button(win, text="Close", width=12, command=win.destroy)
    btn.pack(pady=(0, 8))
    win.bind("<Escape>", lambda e: win.destroy())
    win.lift()
    win.attributes("-topmost", True)
    win.after(200, lambda: win.attributes("-topmost", False))
    btn.focus_set()


# ---------- toast popups (custom Tk) ----------

class _ToastSlots:
    """Tracks vertical positions for stacked popups in the bottom-right
    corner. Slot 0 is the bottommost; higher slots stack upward."""

    def __init__(self):
        self._slots: list[bool] = []
        self._lock = threading.Lock()

    def claim(self) -> int:
        with self._lock:
            for i, used in enumerate(self._slots):
                if not used:
                    self._slots[i] = True
                    return i
            self._slots.append(True)
            return len(self._slots) - 1

    def release(self, slot: int):
        with self._lock:
            if 0 <= slot < len(self._slots):
                self._slots[slot] = False
            while self._slots and not self._slots[-1]:
                self._slots.pop()


TOAST_SLOTS = _ToastSlots()

POPUP_W = 380
POPUP_H = 180
POPUP_GAP = 10
POPUP_BOTTOM_MARGIN = 60   # leave room above the taskbar
POPUP_RIGHT_MARGIN = 20

POPUP_BG = "#2b2b2b"
POPUP_FG = "#f1f1f1"
POPUP_FG_MUTED = "#bbbbbb"
POPUP_ACCENT = "#3a90e0"


def show_toast_popup(parent: tk.Tk, sender: str, body: str, on_ack):
    """Bottom-right Tk popup that stays until the user clicks Open,
    Dismiss, Block, or ×. on_ack(action) is called exactly once with
    action in {"open", "dismiss", "block"}."""
    slot = TOAST_SLOTS.claim()
    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=POPUP_BG)

    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = sw - POPUP_W - POPUP_RIGHT_MARGIN
    y = sh - POPUP_BOTTOM_MARGIN - (slot + 1) * (POPUP_H + POPUP_GAP)
    win.geometry(f"{POPUP_W}x{POPUP_H}+{x}+{y}")

    closed = {"v": False}

    def _close(action: str):
        if closed["v"]:
            return
        closed["v"] = True
        TOAST_SLOTS.release(slot)
        try:
            win.destroy()
        except Exception:
            pass
        try:
            on_ack(action)
        except Exception:
            pass

    # Accent bar on the left
    bar = tk.Frame(win, bg=POPUP_ACCENT, width=4)
    bar.pack(side="left", fill="y")

    inner = tk.Frame(win, bg=POPUP_BG)
    inner.pack(side="left", fill="both", expand=True)

    header = tk.Frame(inner, bg=POPUP_BG)
    header.pack(fill="x", padx=10, pady=(8, 2))
    tk.Label(
        header, text=f"SMS — {sender}",
        bg=POPUP_BG, fg=POPUP_FG,
        font=("Segoe UI", 10, "bold"),
    ).pack(side="left")
    xbtn = tk.Label(
        header, text="×",
        bg=POPUP_BG, fg=POPUP_FG_MUTED,
        font=("Segoe UI", 14, "bold"),
        cursor="hand2",
    )
    xbtn.pack(side="right")
    xbtn.bind("<Button-1>", lambda _e: _close("dismiss"))

    # Pack the button bar FIRST at the bottom so it always reserves its
    # natural height; the body label fills whatever's left in the middle.
    btns = tk.Frame(inner, bg=POPUP_BG)
    btns.pack(side="bottom", fill="x", padx=10, pady=(4, 10))
    # Order in the bar (right→left because side="right" stacks): Open,
    # Block, Dismiss — so visually it reads Dismiss / Block / Open.
    tk.Button(
        btns, text="Open", width=8, height=1,
        command=lambda: _close("open"),
    ).pack(side="right", padx=(6, 0))
    tk.Button(
        btns, text="Block", width=8, height=1,
        command=lambda: _close("block"),
    ).pack(side="right", padx=(6, 0))
    tk.Button(
        btns, text="Dismiss", width=8, height=1,
        command=lambda: _close("dismiss"),
    ).pack(side="right")

    body_lbl = tk.Label(
        inner, text=body,
        bg=POPUP_BG, fg=POPUP_FG,
        font=("Tahoma", 10),
        wraplength=POPUP_W - 30, justify="left", anchor="nw",
        cursor="hand2",
    )
    body_lbl.pack(fill="both", expand=True, padx=10, pady=2)
    body_lbl.bind("<Button-1>", lambda _e: _close("open"))


def show_sms_window(parent: tk.Tk, data: dict, on_close=None):
    """Show SMS detail modal. on_close (optional) is invoked exactly once
    when the window is closed via Close button, Esc, or the X."""
    sender = str(data.get("sender", "?"))
    received = str(data.get("received_at", "?"))
    body = str(data.get("body", ""))

    win = tk.Toplevel(parent)
    win.title(f"SMS — {sender}")
    win.geometry("720x440")

    closed = {"v": False}

    def _close():
        if closed["v"]:
            return
        closed["v"] = True
        try:
            win.destroy()
        except Exception:
            pass
        if on_close:
            try:
                on_close()
            except Exception:
                pass

    info = tk.Frame(win)
    info.pack(fill="x", padx=10, pady=(10, 4))
    tk.Label(info, text=f"From:     {sender}",
             anchor="w", font=("Segoe UI", 10, "bold")).pack(fill="x")
    tk.Label(info, text=f"Received: {received}",
             anchor="w", font=("Segoe UI", 10)).pack(fill="x")
    tk.Frame(win, height=1, bg="#cccccc").pack(fill="x", padx=10, pady=4)

    txt = scrolledtext.ScrolledText(win, wrap="word", font=("Tahoma", 11))
    txt.pack(fill="both", expand=True, padx=10, pady=4)
    txt.insert("1.0", body)
    txt.config(state="disabled")

    def _select_all(_event=None):
        # ScrolledText is "disabled" but selection still works in Tk 8.6+;
        # Ctrl+A selects everything for easy copy.
        txt.tag_add("sel", "1.0", "end-1c")
        return "break"

    txt.bind("<Control-a>", _select_all)
    txt.bind("<Control-A>", _select_all)

    btn = tk.Button(win, text="Close", width=12, command=_close)
    btn.pack(pady=(2, 10))
    win.bind("<Escape>", lambda _e: _close())
    win.protocol("WM_DELETE_WINDOW", _close)
    win.lift()
    win.attributes("-topmost", True)
    win.after(250, lambda: win.attributes("-topmost", False))
    # Steal keyboard focus to this window so Esc works immediately, and
    # park focus on the body so Ctrl+A / Ctrl+C target the SMS text.
    win.focus_force()
    txt.focus_set()


# ---------- main ----------

def _ensure_dirs(cfg: dict, logger: "logging.Logger | None" = None):
    """Create every folder smsFetcher writes into, up-front. Each
    individual writer also lazily creates its target dir before
    writing, so a missing folder isn't a hard error — but eagerly
    creating them at startup makes the directory tree visible
    immediately (so the user can see where data will land before any
    SMS arrives or any tick fires)."""
    base = app_dir()
    keys = (
        "sms_folder", "notified_folder", "log_folder",
        "usage_total_folder", "usage_clients_folder",
        "relay_temp_folder",
    )
    for k in keys:
        rel = cfg.get(k)
        if not rel:
            continue
        try:
            (base / rel).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            if logger is not None:
                logger.warning(f"could not create {rel}/: {e}")


def main() -> int:
    cfg, cfg_path = load_or_create_config()
    # Create all sibling folders before threads start, so the layout
    # is consistent on first launch regardless of which writer fires
    # first (Fetcher vs Notifier vs UsageTracker vs Relayer).
    _ensure_dirs(cfg)
    log_dir = app_dir() / cfg["log_folder"]
    logger = setup_logging(log_dir)
    logger.info(f"--- {APP_NAME} starting (config: {cfg_path.name}) ---")

    lock = acquire_single_instance(cfg["single_instance_port"])
    if lock is None:
        logger.warning("another instance is already running; exiting")
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(
                APP_NAME,
                "smsFetcher is already running.",
            )
            root.destroy()
        except Exception:
            pass
        return 1

    root = tk.Tk()
    root.withdraw()
    root.title(APP_NAME)

    # Cross-thread UI dispatch: pystray callbacks and toast click handlers
    # push commands into this queue; the Tk poller drains it on the main
    # thread. Tk's after() is only loosely thread-safe, and bouncing
    # through a queue is the canonical fix.
    ui_queue: "queue.Queue" = queue.Queue()

    blacklist = Blacklist(app_dir() / cfg["blacklist_file"], logger)

    fetcher = Fetcher(cfg, logger)
    fetcher.blacklist = blacklist
    fetcher_thread = threading.Thread(target=fetcher.run, daemon=True)
    fetcher_thread.start()

    # UsageTracker shares the Fetcher's ModemClient to avoid two parallel
    # logins fighting over the modem's single-IP session cookie.
    usage = UsageTracker(cfg, logger, fetcher.modem)
    usage_thread = threading.Thread(target=usage.run, daemon=True)
    usage_thread.start()

    icon_holder = {"icon": None}
    icon_state = {"unread": 0, "relaying": False}
    icon_state_lock = threading.Lock()

    def _refresh_icon():
        # Yellow (relaying) wins over red (unread) wins over green
        # (idle). pystray supports updating icon.icon and icon.title
        # from any thread, but bouncing through one helper keeps the
        # state machine in one place.
        icon = icon_holder.get("icon")
        if icon is None:
            return
        with icon_state_lock:
            unread = icon_state["unread"]
            relaying = icon_state["relaying"]
        try:
            icon.icon = make_icon_image(unread=unread, relaying=relaying)
            if relaying:
                icon.title = (
                    f"{APP_NAME} (relaying, {unread} unread)" if unread
                    else f"{APP_NAME} (relaying)"
                )
            elif unread:
                icon.title = f"{APP_NAME} ({unread} unread)"
            else:
                icon.title = APP_NAME
        except Exception as e:
            logger.error(f"icon update failed: {e}", exc_info=True)

    def on_unread_change(count: int):
        with icon_state_lock:
            icon_state["unread"] = count
        _refresh_icon()
        logger.info(f"icon: unread={count}")

    def on_relay_state_change(on: bool):
        with icon_state_lock:
            icon_state["relaying"] = on
        _refresh_icon()
        logger.info(f"icon: relaying={on}")

    notifier = Notifier(
        cfg, logger, ui_queue,
        on_unread_change=on_unread_change,
        blacklist=blacklist,
    )
    notifier_thread = threading.Thread(target=notifier.run, daemon=True)
    notifier_thread.start()

    relayer = Relayer(
        cfg, logger, fetcher.modem, notifier,
        blacklist=blacklist,
        on_relay_state_change=on_relay_state_change,
    )
    relayer_thread = threading.Thread(target=relayer.run, daemon=True)
    relayer_thread.start()

    # TelinaWatcher is a separate, isolated thread; its exceptions are
    # caught and logged in tick() so a broken Telina poll never affects
    # the main SMS forwarding flow.
    if cfg.get("telina_enabled", True):
        telina = TelinaWatcher(cfg, logger, fetcher.modem)
        telina_thread = threading.Thread(target=telina.run, daemon=True)
        telina_thread.start()
        logger.info("telina: watcher thread started")
    else:
        logger.info("telina: disabled in config")

    def on_log(_icon, _item):
        logger.info("tray: Log clicked")
        ui_queue.put("show_log")

    def on_exit(_icon, _item):
        logger.info("tray: Exit clicked")
        ui_queue.put("exit")

    def pump_ui():
        try:
            while True:
                try:
                    item = ui_queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, tuple):
                    cmd, payload = item
                else:
                    cmd, payload = item, None
                if cmd == "show_log":
                    try:
                        show_log_window(root, log_dir)
                    except Exception as e:
                        logger.error(
                            f"opening log window failed: {e}", exc_info=True
                        )
                elif cmd == "show_sms_data":
                    data = payload.get("data", {})
                    on_close = payload.get("on_close")
                    try:
                        show_sms_window(root, data, on_close=on_close)
                    except Exception as e:
                        logger.error(
                            f"opening sms window failed: {e}", exc_info=True
                        )
                        if on_close:
                            try:
                                on_close()
                            except Exception:
                                pass
                elif cmd == "show_toast_popup":
                    try:
                        show_toast_popup(
                            root,
                            sender=payload["sender"],
                            body=payload["body"],
                            on_ack=payload["on_ack"],
                        )
                    except Exception as e:
                        logger.error(
                            f"opening toast popup failed: {e}", exc_info=True
                        )
                elif cmd == "exit":
                    logger.info(f"--- {APP_NAME} stopping ---")
                    fetcher.stop()
                    notifier.stop()
                    usage.stop()
                    relayer.stop()
                    try:
                        icon_holder["icon"].stop()
                    except Exception:
                        pass
                    root.destroy()
                    return
        finally:
            root.after(100, pump_ui)

    root.after(100, pump_ui)

    icon = pystray.Icon(
        APP_NAME,
        make_icon_image(),
        APP_NAME,
        menu=pystray.Menu(
            pystray.MenuItem("Log", on_log),
            pystray.MenuItem("Exit", on_exit),
        ),
    )
    icon_holder["icon"] = icon
    threading.Thread(target=icon.run, daemon=True).start()

    try:
        root.mainloop()
    finally:
        fetcher.stop()
        notifier.stop()
        usage.stop()
        relayer.stop()
        try:
            icon.stop()
        except Exception:
            pass
        try:
            lock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
