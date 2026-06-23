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
import subprocess
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
    "forward_enabled": False,
    "forward_match_sender": "",
    "forward_match_substring": "",
    "forward_replacements": {},
    "forward_regex_replacements": [],
    "mci_enabled": True,
    "mci_username": "9133169571",
    # Normal poll cadence (seconds). Accelerates to
    # mci_fast_poll_interval_seconds once today's usage reaches
    # mci_fast_poll_usage_gb.
    "mci_interval_seconds": 3600,
    "mci_fast_poll_interval_seconds": 600,
    "mci_state_file": "mci_state.json",
    "mci_quota_below_gb": 5.0,
    # Daily usage-cap model. Usage today = day-begin remaining baseline
    # minus current remaining quota. day-begin.bat runs at the first
    # poll of each day; mci-quota-reached.bat runs once when today's
    # usage hits mci_quota_reached_usage_gb (then polling pauses until
    # the next day). Paths are resolved against the app dir if relative.
    "mci_daily_limit_gb": 5.0,
    "mci_quota_reached_usage_gb": 4.9,
    "mci_fast_poll_usage_gb": 4.0,
    "mci_day_begin_bat": "day-begin.bat",
    "mci_quota_reached_bat": "mci-quota-reached.bat",
    "mci_otp_match_substring": "کد یکبار مصرف همراه‌من",
    "mci_otp_pattern": r"Code:\s*(\d+)",
    # How long one OTP attempt stays "active" (icon blinks) waiting for
    # the SMS to land. Kept short so a never-arriving OTP doesn't blink
    # forever — on expiry the watcher backs off and retries. Also the
    # send-otp re-send cooldown.
    "mci_otp_wait_seconds": 60,
    # Retry-with-backoff ladder (seconds) used after an OTP attempt
    # times out without the code arriving. The watcher waits the next
    # rung, re-requests an OTP, and waits ``mci_otp_wait_seconds`` again.
    # The last rung repeats forever (every 6 h) until a human intervenes
    # or auth succeeds. The icon does NOT blink during these waits — only
    # during the active OTP-wait window of each attempt.
    "mci_otp_backoff_seconds": [300, 1800, 3600, 21600],
    # Upper bound on any single _wake.wait() slice so the loop stays
    # responsive (detects OTP-wait expiry / stop within this window even
    # when a long backoff or sleep-until-midnight is scheduled).
    "mci_max_wait_slice_seconds": 60,
    "mci_quota_log_filename": "remained_quota.txt",
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
        """Send an outbound SMS via /sms_new.htm's form. Two firmware
        thresholds are silent (HTTP 200, no exception, no outbox entry):
        UCS-2/Persian sends drop above ~17 chars, and ASCII/GSM-7 sends
        drop above ~30 chars — confirmed empirically with a 66-char
        ASCII body that vanished while a 28-char one queued. Stay well
        under 30 chars for direct sends; use the relay path for
        anything larger."""
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

    # --- Mobile-network enable/disable ---
    # Settings → Mobile Network (Basic Settings) is rendered at /lte.htm as
    # the single form `formLteSetup` (POST /boafrm/formLteSetup). Crucially
    # the page does NOT carry current values in HTML attributes — it bakes
    # them into JS `var` assignments consumed by Load_Setting() on load
    # (e.g. `var enableLte = '1';`, `var manual_apn = '1';`), while text
    # fields keep their value in the inline `value="…"`. A real browser
    # save serialises the live DOM: text inputs always, selects by selected
    # option, checkboxes ONLY when checked (submitting name=on), the
    # `enabled` radio (Priority Nettype), plus a `save_apply` flag.
    #
    # To toggle the "Enabled" checkbox (`USB3G_ENABLE`) without disturbing
    # anything else we read the page, reconstruct that exact serialisation
    # from the on-load state, flip only USB3G_ENABLE, and re-POST. No
    # buffering/caching — every call re-reads the live form first.

    _LTE_VAR_RE = re.compile(r"var\s+(\w+)\s*=\s*'([^']*)'\s*;")

    # Map: POST field name -> JS var that holds its on-load checked state.
    # A checkbox is submitted (as `on`) iff its var is truthy ('1' / non-empty
    # for the enable flag). USB3G_ENABLE keys off `enableLte`.
    _LTE_CHECKBOX_VARS = {
        "USB3G_ENABLE": "enableLte",
        "USB3G_MANUALAPN": "manual_apn",
        "USB3G_LTEBRIDGE": "lteBridge",
        "USB3G_LTEPPP": "ltePpp",
        "USB3G_NAT": "usb3g_nat",
        "USB3G_Roam": "usb3g_roam",
        "MANUAL_DNS": "manualDns",
        "USB3G_PINGCHECK": "netcheckEnable",
    }
    # Select field -> JS var holding its selected option value.
    _LTE_SELECT_VARS = {
        "USB3G_NETSELECT": "netselect",
        "USB3G_AUTHMETHOD": "authmode",
        "USB3G_IPVER": "ipver",
    }
    # Text fields are read from their inline value="" attribute.
    _LTE_TEXT_FIELDS = (
        "USB3G_USER", "USB3G_PASS", "USB3G_APN", "USB3G_PIN",
        "USB3G_DIALNUM", "USB3G_MTU", "USB3G_DNS1", "USB3G_DNS2",
        "USB3G_PINGPERIOD", "USB3G_PINGADDR1", "USB3G_PINGADDR2",
        "USB3G_PINGADDR3", "USB3G_PINGADDR4", "USB3G_PINGADDR5",
    )

    def _get_lte_page(self) -> str:
        url = f"{self.base}/lte.htm"
        r = self.session.get(url, timeout=self.timeout)
        if self._looks_unauthed(r.content):
            self.logger.info("session expired, re-logging in (lte)")
            self.login()
            r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        if self._looks_unauthed(r.content):
            raise RuntimeError("could not authenticate to fetch /lte.htm")
        return r.content.decode("utf-8", errors="replace")

    def _parse_lte_form(self, t: str) -> dict:
        """Reconstruct the browser's formLteSetup serialisation from the
        on-load state baked into the page. Returns
        {"enabled": bool, "fields": {name: value}, "checkboxes":
        {name: bool}} so callers can flip one checkbox and rebuild the
        POST body deterministically."""
        vars_map: dict[str, str] = {}
        for m in self._LTE_VAR_RE.finditer(t):
            # Load_Setting()'s block runs last; last write wins, which is
            # exactly the live state we want.
            vars_map[m.group(1)] = m.group(2)

        def text_val(name: str) -> str:
            m = re.search(
                r'<input[^>]*name\s*=\s*"?' + re.escape(name) + r'"?[^>]*>',
                t, re.I,
            )
            if not m:
                return ""
            v = re.search(r'value\s*=\s*"([^"]*)"', m.group(0))
            return v.group(1) if v else ""

        fields = {name: text_val(name) for name in self._LTE_TEXT_FIELDS}
        for field, var in self._LTE_SELECT_VARS.items():
            fields[field] = vars_map.get(var, "0")
        # Priority Nettype radio: Load_Setting picks index 1 (ON) only when
        # firstWanIndex == '6', else index 0 (OFF).
        fields["enabled"] = (
            "ON" if vars_map.get("firstWanIndex", "0") == "6" else "OFF"
        )

        checkboxes: dict[str, bool] = {}
        for field, var in self._LTE_CHECKBOX_VARS.items():
            raw = vars_map.get(var, "0")
            checkboxes[field] = raw not in ("0", "")

        return {
            "enabled": checkboxes["USB3G_ENABLE"],
            "fields": fields,
            "checkboxes": checkboxes,
        }

    def get_mobile_network_enabled(self) -> bool:
        """Current state of Settings → Mobile Network → Enabled
        (USB3G_ENABLE)."""
        return self._parse_lte_form(self._get_lte_page())["enabled"]

    def set_mobile_network_enabled(self, enabled: bool) -> bool:
        """Flip only the "Enabled" checkbox of the Mobile Network form,
        preserving every other field, and POST the full form exactly as
        the panel's Save & Apply would. Re-reads the page afterwards to
        confirm the new state. Returns the verified state (True/False).
        Raises on transport/auth failure or if the modem didn't accept
        the change."""
        parsed = self._parse_lte_form(self._get_lte_page())
        data: list[tuple[str, str]] = []
        for name in self._LTE_TEXT_FIELDS:
            data.append((name, parsed["fields"].get(name, "")))
        for name in self._LTE_SELECT_VARS:
            data.append((name, parsed["fields"].get(name, "0")))
        # Checkboxes: a browser only sends checked boxes (as `on`). Flip
        # USB3G_ENABLE to the requested state; leave the rest untouched.
        checkboxes = dict(parsed["checkboxes"])
        checkboxes["USB3G_ENABLE"] = bool(enabled)
        for name, on in checkboxes.items():
            if on:
                data.append((name, "on"))
        data.append(("enabled", parsed["fields"].get("enabled", "OFF")))
        data.append(("submit-url", "/lte.htm"))
        # The panel renames the hidden save_apply_flag to `save_apply` on
        # submit; replicate the resulting field (value "1").
        data.append(("save_apply", "1"))

        r = self.session.post(
            f"{self.base}/boafrm/formLteSetup",
            data=data,
            headers={"Referer": f"{self.base}/lte.htm"},
            timeout=self.timeout,
            allow_redirects=True,
        )
        if r.status_code != 200 or self._looks_unauthed(r.content):
            raise RuntimeError(
                f"set_mobile_network_enabled: HTTP {r.status_code} "
                f"or session lost"
            )
        # Verify by re-reading. The modem applies the LTE profile change
        # synchronously enough that the next page render reflects it.
        actual = self.get_mobile_network_enabled()
        if actual != bool(enabled):
            raise RuntimeError(
                f"set_mobile_network_enabled: requested {enabled} but "
                f"modem still reports {actual}"
            )
        self.logger.info(
            f"mobile network Enabled set to {actual} (USB3G_ENABLE)"
        )
        return actual


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


# ---------- remained-quota log ----------

QUOTA_LOG_FILENAME = "remained_quota_sms.txt"


def quota_log_path(cfg: dict) -> Path:
    """Sibling of the per-day usage files. Shared by the live Fetcher
    hook and `backfill_quota_log.py` so both write the same file."""
    return app_dir() / cfg["usage_total_folder"] / QUOTA_LOG_FILENAME


def parse_quota_received_at(received_at: str) -> str:
    """Render an SMS `received_at` (`YYYY-MM-DD HH:MM:SS`) as the
    `YYYY/MM/DD HH:MM:SS` log timestamp. Falls back to current local
    time if the input is unparseable."""
    try:
        dt = datetime.strptime(received_at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        dt = datetime.now()
    return dt.strftime("%Y/%m/%d %H:%M:%S")


def write_quota_line(log_path: Path, ts: str, mb: int):
    """Insert (or overwrite, keyed by timestamp prefix) one `ts mb`
    line in `log_path`. File stays sorted chronologically. Atomic via
    `.tmp` + replace. Sub-second collisions from the SIM are effectively
    impossible, so the timestamp prefix is a unique key per SMS."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if log_path.exists():
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            if raw.startswith(ts + " "):
                continue
            lines.append(raw)
    lines.append(f"{ts} {mb}")
    lines.sort()
    tmp = log_path.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(log_path)


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
        # Set by main() after both Fetcher and MciWatcher are
        # constructed. When non-None, _maybe_dispatch_otp() forwards
        # the digits from MCI OTP SMSes for verify + deferred quota
        # fetch. See MciWatcher.on_otp_received.
        self.mci_watcher: "MciWatcher | None" = None
        # Pre-compile the OTP-digits regex once. Invalid pattern only
        # logs at first use; the SMS still saves either way.
        self._mci_otp_re: "re.Pattern[str] | None" = None
        try:
            self._mci_otp_re = re.compile(
                str(cfg.get("mci_otp_pattern") or r"Code:\s*(\d+)")
            )
        except re.error as e:
            self.logger.error(
                f"mci_otp_pattern invalid (OTP dispatch disabled): {e}"
            )
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
            # Clear BEFORE the cycle so a trigger_now() during the cycle
            # (e.g. MciWatcher after POSTing send-otp) is captured by the
            # subsequent wait() and causes an immediate next cycle. The
            # opposite order (clear after cycle) loses any trigger that
            # fired while the cycle was running.
            self._wake.clear()
            try:
                self.cycle()
            except Exception as e:
                self.logger.error(f"cycle error: {e}", exc_info=True)
            self._wake.wait(self.cfg["poll_interval_seconds"])

    def _maybe_forward(self, rec: dict, json_path: Path):
        """If `forward_enabled` and the message matches both
        `forward_match_sender` (exact) and `forward_match_substring`
        (CSV of substrings — body must contain ANY one; "" means any),
        rewrite the body and send it to `telina_notif_number` via the
        modem. Whichever substring matched is auto-stripped (along
        with the immediately following newline if present) before
        `forward_replacements` (literal) and
        `forward_regex_replacements` (re.sub) run. Best-effort: a
        single attempt, no retry — failures log and move on. Silently
        suppressed for blocklisted senders (caller already routed
        those away from this branch).

        Note: the modem's send path silently drops UCS-2/Persian sends
        above ~17 chars and ASCII/GSM-7 sends above ~30 chars (see
        "Sending SMS — known firmware limit"). The rewrite chain
        exists to bring matched bodies under both ceilings; the success
        log records `len` and `ascii=` so over-budget sends are at
        least visible after the fact."""
        if not self.cfg.get("forward_enabled", False):
            return
        match_sender = str(self.cfg.get("forward_match_sender", "")).strip()
        if not match_sender:
            return
        sender = str(rec.get("sender", ""))
        if sender != match_sender:
            return
        match_sub_raw = str(self.cfg.get("forward_match_substring", ""))
        match_subs = [s.strip() for s in match_sub_raw.split(",") if s.strip()]
        body = str(rec.get("body", ""))
        matched_subs = [s for s in match_subs if s in body]
        if match_subs and not matched_subs:
            return
        local = str(self.cfg.get("telina_notif_number", "")).strip()
        if not local:
            self.logger.warning(
                f"forward: trigger matched ({sender}) but "
                f"telina_notif_number is empty; skipping"
            )
            return
        cc = str(self.cfg.get("relay_sms_country_code", "98"))
        if cc and local.startswith("0"):
            local = local[1:]
        # Apply config-defined rewrites so an otherwise-Persian or
        # over-budget body becomes a short ASCII string that fits in a
        # single SMS. Match was against the original body; we send the
        # rewritten one.
        # 0. Whichever match substring(s) hit are auto-stripped, plus
        #    the immediately following newline if present. Saves the
        #    user from having to repeat each match candidate inside
        #    `forward_replacements`.
        # 1. `forward_replacements` — literal str.replace, applied in
        #    insertion order. Use for transliterations and fixed
        #    substrings that aren't match candidates.
        # 2. `forward_regex_replacements` — list of [pattern, repl]
        #    pairs, applied in order with re.sub. Use for variable
        #    content like dates and ids.
        send_body = body
        for sub in matched_subs:
            send_body = send_body.replace(sub + "\n", "").replace(sub, "")
        replacements = self.cfg.get("forward_replacements") or {}
        if isinstance(replacements, dict):
            for src, dst in replacements.items():
                if src:
                    send_body = send_body.replace(str(src), str(dst))
        regex_replacements = self.cfg.get("forward_regex_replacements") or []
        if isinstance(regex_replacements, list):
            for entry in regex_replacements:
                if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                    continue
                try:
                    send_body = re.sub(
                        str(entry[0]), str(entry[1]), send_body,
                    )
                except re.error as e:
                    self.logger.error(
                        f"forward: invalid regex {entry[0]!r}: {e}"
                    )
        is_ascii = send_body.isascii()
        try:
            self.modem.send_sms(
                country_code=cc, number=local, content=send_body,
            )
            self.logger.info(
                f"forward: sent body from sender={sender} "
                f"to {self.cfg['telina_notif_number']} "
                f"({len(send_body)} chars, ascii={is_ascii}, "
                f"source={json_path.name})"
            )
            if not is_ascii and len(send_body) > 17:
                self.logger.warning(
                    f"forward: body still contains non-ASCII at "
                    f"{len(send_body)} chars; DWR-M960 silently drops "
                    f"UCS-2 sends > ~17 chars — destination may "
                    f"receive nothing"
                )
        except Exception as e:
            self.logger.error(
                f"forward: send_sms failed for {json_path.name}: {e}",
                exc_info=True,
            )

    def _is_mci_otp(self, rec: dict) -> bool:
        """True iff this SMS looks like an MCI panel OTP — body contains
        the configured `mci_otp_match_substring` (default the Persian
        'one-time code, My-MCI' header). Such SMSes are routed straight
        to ``sms_del/`` at save time so they don't pop a toast, and
        the digits are dispatched to MciWatcher (see
        `_maybe_dispatch_otp`)."""
        if not self.cfg.get("mci_enabled", True):
            return False
        needle = str(self.cfg.get("mci_otp_match_substring") or "").strip()
        if not needle:
            return False
        body = str(rec.get("body") or "")
        return needle in body

    def _maybe_dispatch_otp(self, rec: dict):
        """Extract the OTP digits from the SMS body and hand them to
        MciWatcher.on_otp_received(). Called from cycle() right after
        an OTP-shaped SMS is saved. Quiet no-op if the watcher isn't
        wired up (mci_enabled is false) or the regex doesn't match."""
        if self.mci_watcher is None or self._mci_otp_re is None:
            return
        body = str(rec.get("body") or "")
        m = self._mci_otp_re.search(body)
        if not m:
            self.logger.warning(
                f"mci_otp: body matched substring but pattern "
                f"{self._mci_otp_re.pattern!r} did not — no dispatch"
            )
            return
        try:
            code = m.group(1)
        except IndexError:
            code = m.group(0)
        try:
            self.mci_watcher.on_otp_received(code)
        except Exception as e:
            self.logger.error(
                f"mci_otp: dispatch to MciWatcher failed: {e}",
                exc_info=True,
            )

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
            is_otp = self._is_mci_otp(rec)
            # OTP SMSes are saved to sms/ and notified like any other
            # received SMS (even when auto-triggered) so the user always
            # sees the code arrive. The MciWatcher additionally receives
            # the digits via the _maybe_dispatch_otp callback below.
            # Blocked senders still go straight to sms_del/.
            target_dir = self.del_dir if blocked else self.sms_dir
            try:
                path = save_sms(rec, target_dir, self.cfg["modem_url"])
            except Exception as e:
                self.logger.error(
                    f"save failed: index={idx} sender={sender}: {e}"
                )
                continue
            if is_otp:
                # Saved to sms/ (unless the sender is blacklisted) so the
                # Notifier shows it like any received SMS. Always dispatch
                # the digits to the MciWatcher. Not forwarded onward.
                self.logger.info(
                    f"OTP-MCI index={idx} sender={sender} "
                    f"received_at='{rec['received_at']}' -> "
                    f"{target_dir.name}/{path.name} "
                    f"({'blocked, silent' if blocked else 'notified'}; "
                    f"code dispatched)"
                )
                self._maybe_dispatch_otp(rec)
            elif blocked:
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
                self._maybe_forward(rec, path)
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

        # Cap sum of per-IP deltas at the global delta. The /usertraffic.htm
        # per-IP counter on this firmware resets multiple times per hour
        # without actually losing bytes (firmware glitch — see readme.md
        # "Usage counters — known firmware quirks"). Without this cap,
        # _delta_with_reset treats each spurious reset's post-reset value
        # as fresh usage and per-IP totals balloon to physically-impossible
        # numbers (e.g. 57 GB attributed to one client on a day where the
        # global only saw 4 GB). The global lteTx/lteRx is the reliable
        # upper bound — sum of per-IP attributions can't exceed it.
        sum_tx = sum(v["tx"] for v in d_per_ip.values())
        sum_rx = sum(v["rx"] for v in d_per_ip.values())
        if sum_tx > d_lte_tx:
            factor = (d_lte_tx / sum_tx) if sum_tx else 0
            for v in d_per_ip.values():
                v["tx"] = int(v["tx"] * factor)
            self.logger.info(
                f"usage: capped per-IP tx sum {sum_tx} -> {d_lte_tx} bytes"
            )
        if sum_rx > d_lte_rx:
            factor = (d_lte_rx / sum_rx) if sum_rx else 0
            for v in d_per_ip.values():
                v["rx"] = int(v["rx"] * factor)
            self.logger.info(
                f"usage: capped per-IP rx sum {sum_rx} -> {d_lte_rx} bytes"
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
        if 500 <= r.status_code < 600:
            # The Telina hub returns 5xx (not 401) when a cached token
            # has aged out — observed 2026-05-08/09 with a token that
            # had been live since process start on 2026-05-05. Treat
            # 5xx the same as 401 and let the retry loop re-login once
            # before propagating. If the server is genuinely 500-ing,
            # the retry will also fail and the exception bubbles up.
            preview = r.text[:200].replace("\n", " ")
            raise PermissionError(
                f"telina: HTTP {r.status_code} (treating as stale "
                f"auth); body: {preview!r}"
            )
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
        # Lazy login + appId resolution. On 401 (or 5xx, which Telina
        # uses for stale tokens) we drop the cached creds and re-acquire
        # them once before giving up for this tick.
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


# ---------- quota-low warning ----------

class QuotaWarner:
    """Drains a FIFO of MCI quota-related notifications into
    bottom-right Tk popups, one at a time, only while Windows accepts
    notifications. The MciWatcher calls one of three enqueue methods
    after each panel interaction:

    - ``enqueue_warning(gb)`` — below-threshold scrape (orange accent).
    - ``enqueue_info(gb)``    — normal scrape, user wants to know
                                 the figure (green accent).
    - ``enqueue_error(msg)``  — fetch / auth failure (red accent).

    Why a separate thread instead of dispatching the popup straight
    from the watcher: the popup needs to defer under DND / Focus
    Assist / fullscreen (same gating as the SMS toast popups). Keeping
    the queue here decouples those two concerns, so a backlog of
    notifications drains cleanly when the user comes out of DND."""

    TICK_SECONDS = 15

    def __init__(self, ui_queue: "queue.Queue", logger: logging.Logger):
        self.ui_queue = ui_queue
        self.logger = logger
        # Each item: {"kind": "warning"|"info"|"error", "value": ...}
        # where value is float GB for warning/info and str message for
        # error.
        self._pending: "list[dict]" = []
        self._lock = threading.Lock()
        self._showing = False
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _enqueue(self, item: dict, log_line: str):
        with self._lock:
            self._pending.append(item)
            depth = len(self._pending)
        self.logger.info(f"{log_line} (pending={depth})")
        self._wake.set()

    def enqueue_warning(self, gb: float):
        """Low-quota warning. The MciWatcher routes here when the
        scraped value is below ``mci_quota_below_gb``."""
        self._enqueue(
            {"kind": "warning", "value": float(gb)},
            f"quota: enqueued warning for {gb:.2f} GB",
        )

    def enqueue_info(self, gb: float):
        """Informational notification with the latest quota figure.
        Fires on successful fetches that the user should see — every
        manual tray-menu trigger and the first auto-fetch each day."""
        self._enqueue(
            {"kind": "info", "value": float(gb)},
            f"quota: enqueued info for {gb:.2f} GB",
        )

    def enqueue_error(self, message: str):
        """Error notification. Fires on auth/fetch failures the user
        should know about (same gating as info)."""
        self._enqueue(
            {"kind": "error", "value": str(message)},
            f"quota: enqueued error: {message!r}",
        )

    def enqueue_cap(self, usage_gb: float, remaining_gb: float,
                    limit_gb: float):
        """Daily usage-cap reached. Distinct from the low-remaining
        warning: this fires when today's *usage* (day-begin baseline
        minus current remaining) crosses the configured cap, right after
        the MciWatcher launches mci-quota-reached.bat."""
        self._enqueue(
            {
                "kind": "cap",
                "value": {
                    "usage": float(usage_gb),
                    "remaining": float(remaining_gb),
                    "limit": float(limit_gb),
                },
            },
            f"quota: enqueued daily-cap (usage={usage_gb:.2f} GB)",
        )

    def _on_ack(self):
        # Tk-thread callback: popup just closed. Allow the next tick to
        # show another one (if any are queued and DND is clear).
        with self._lock:
            self._showing = False
        self._wake.set()

    def tick(self):
        with self._lock:
            if self._showing or not self._pending:
                return
        if not accepts_notifications():
            return
        with self._lock:
            item = self._pending.pop(0)
            self._showing = True
        kind = item["kind"]
        value = item["value"]
        self.ui_queue.put((
            "show_quota_popup",
            {"kind": kind, "value": value, "on_ack": self._on_ack},
        ))
        if isinstance(value, float):
            self.logger.info(
                f"quota: showing {kind} popup for {value:.2f} GB"
            )
        else:
            self.logger.info(
                f"quota: showing {kind} popup ({str(value)[:80]!r})"
            )

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.logger.error(
                    f"quota: tick error: {e}", exc_info=True,
                )
            self._wake.clear()
            self._wake.wait(self.TICK_SECONDS)


# ---------- MCI panel watcher (auto-login + remaining-quota scrape) ----------

# Hardcoded — these are MCI's own endpoints and not configurable. If MCI
# rewires the panel, the constants here are the only place to update.
MCI_API_BASE = "https://my.mci.ir"
MCI_ENDPOINT_SEND_OTP = "/api/idm/v1/auth/send-otp"
MCI_ENDPOINT_VERIFY_OTP = "/api/idm/v1/auth"
MCI_ENDPOINT_QUOTA = "/api/unit/v1/packages/details"


class MciClient:
    """Client for https://my.mci.ir/panel — authenticates via SMS OTP
    and reads the remaining-quota figure off the packages-details API.

    Login flow (two JSON POSTs):
      1. POST /api/idm/v1/auth/send-otp  body {"username": "<10-digit mobile>"}
      2. POST /api/idm/v1/auth           body {"username": "<mobile>",
                                              "credential": "<5-digit OTP>",
                                              "credential_type": "OTP"}

    Auth is **JWT Bearer**, confirmed by end-to-end test against the
    live server. The verify response carries
    `{"access_token": "<JWT>"}`; the only cookie set during the whole
    flow is the CDN/WAF's `cookiesession1`, which doesn't authorize
    the quota GET on its own. verify_otp() pulls the JWT out of the
    body (checking `access_token` / `token` / `accessToken` / `id_token`
    at the top level and one level deep under `data`) and adds it as
    `Authorization: Bearer …`. Session state is persisted to
    `mci_state.json` (cookies + bearer + extras + saved_at) and
    replayed on restart, so the OTP flow only runs when the server
    has expired the session remotely (401/403 on a quota GET).

    The state file also carries arbitrary scalar "extras" written via
    `set_extra(key, value)` — used by MciWatcher to persist
    `last_check_date` alongside the auth material in the same file."""

    BASE = MCI_API_BASE

    # Browser-shaped headers so the panel's WAF / CDN doesn't classify
    # us as a bot. Origin / Referer mimic the SPA itself. The MCI panel
    # sits behind a TLS-fingerprinting WAF — Python `requests` (OpenSSL)
    # passes, Windows' bundled curl (Schannel) gets 599-Blocked. See
    # the README's "WAF — TLS fingerprinting" note.
    _COMMON_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fa,en;q=0.9",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://my.mci.ir",
        "Referer": "https://my.mci.ir/panel",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    def __init__(self, username: str, state_path: Path, timeout: float,
                 logger: logging.Logger):
        self.username = username
        self.state_path = state_path
        self.timeout = timeout
        self.logger = logger
        self.session = requests.Session()
        # Same rationale as ModemClient: behind a system HTTP proxy the
        # request could be hijacked. We always talk directly.
        self.session.trust_env = False
        self.session.headers.update(self._COMMON_HEADERS)
        # Optional Bearer token, populated when verify_otp finds one in
        # the JSON response body.
        self._bearer: "str | None" = None
        # Extra scalar fields persisted alongside cookies/bearer. The
        # MciWatcher uses this to store `last_check_date`. Survives
        # clear_session() — only cookies/bearer are cleared on session
        # expiry; the "last successful quota check date" is a separate
        # fact and shouldn't be reset just because auth lapsed.
        self._extra: dict = {}
        # Guards _extra + the state-file I/O. RLock so set_extra() can
        # call _save_state_unlocked() without deadlocking.
        self._state_lock = threading.RLock()
        self._load_state()

    # ---- state persistence ----

    def _state_payload(self) -> dict:
        cookies = []
        for c in self.session.cookies:
            cookies.append({
                "name": c.name, "value": c.value,
                "domain": c.domain, "path": c.path,
                "expires": c.expires, "secure": c.secure,
            })
        payload = dict(self._extra)
        payload.update({
            "cookies": cookies,
            "bearer": self._bearer,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        })
        return payload

    def _save_state_unlocked(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    self._state_payload(),
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as e:
            self.logger.error(f"mci: save state failed: {e}")

    def _save_state(self):
        with self._state_lock:
            self._save_state_unlocked()

    def _load_state(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for c in data.get("cookies") or []:
                self.session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain") or None,
                    path=c.get("path") or "/",
                )
            self._bearer = data.get("bearer") or None
            if self._bearer:
                self.session.headers["Authorization"] = (
                    f"Bearer {self._bearer}"
                )
            # Anything that isn't a known auth field becomes an "extra".
            reserved = {"cookies", "bearer", "saved_at"}
            self._extra = {
                k: v for k, v in data.items() if k not in reserved
            }
            self.logger.info(
                f"mci: loaded session from {self.state_path.name} "
                f"(cookies={len(data.get('cookies') or [])}, "
                f"bearer={'yes' if self._bearer else 'no'}, "
                f"extras={list(self._extra.keys()) or 'none'})"
            )
        except Exception as e:
            self.logger.error(f"mci: load state failed: {e}")

    def get_extra(self, key: str, default=None):
        with self._state_lock:
            return self._extra.get(key, default)

    def set_extra(self, key: str, value):
        """Persist an arbitrary scalar alongside the auth state. Writes
        the whole state file atomically so cookies/bearer aren't lost."""
        with self._state_lock:
            self._extra[key] = value
            self._save_state_unlocked()

    def del_extra(self, key: str):
        """Remove an extra and persist. No-op if the key isn't there."""
        with self._state_lock:
            if key in self._extra:
                self._extra.pop(key)
                self._save_state_unlocked()

    def clear_session(self):
        """Drop cookies/bearer (in memory) and persist the cleared
        state. Called when the server returns 401/403 on a quota GET —
        the watcher then re-runs the OTP flow. ``_extra`` is preserved:
        `last_check_date` records "we last successfully read quota on
        date X" and isn't invalidated by a session expiry."""
        with self._state_lock:
            self.session.cookies.clear()
            self._bearer = None
            self.session.headers.pop("Authorization", None)
            self._save_state_unlocked()

    # ---- HTTP calls ----

    def request_otp(self):
        """POST send-otp → MCI sends a 5-digit code SMS to the registered
        SIM. Raises on non-2xx."""
        url = self.BASE + MCI_ENDPOINT_SEND_OTP
        r = self.session.post(
            url, json={"username": self.username},
            timeout=self.timeout,
        )
        if not r.ok:
            preview = r.text[:200].replace("\n", " ")
            raise RuntimeError(
                f"mci: send-otp HTTP {r.status_code}: {preview!r}"
            )
        self.logger.info(f"mci: send-otp OK (HTTP {r.status_code})")

    def verify_otp(self, code: str):
        """POST verify with the OTP digits. Captures whatever auth
        material the server returns: cookies (auto-handled by the
        Session), and/or a Bearer token if one shows up in the JSON
        body. Raises on non-2xx OR if no auth material was returned
        (so we don't silently persist an unauthenticated session)."""
        url = self.BASE + MCI_ENDPOINT_VERIFY_OTP
        r = self.session.post(
            url, json={
                "username": self.username,
                "credential": code,
                "credential_type": "OTP",
            },
            timeout=self.timeout,
        )
        if not r.ok:
            preview = r.text[:200].replace("\n", " ")
            raise RuntimeError(
                f"mci: verify-otp HTTP {r.status_code}: {preview!r}"
            )
        # Best-effort token extraction. We try a few common shapes
        # because the actual response schema wasn't captured when this
        # was written; whichever one matches wins, the others are
        # harmless. If none match and there are cookies, that's fine —
        # cookie auth alone is sufficient for the quota GET.
        try:
            data = r.json()
        except Exception:
            data = {}
        token = None
        candidates = ("access_token", "token", "accessToken", "id_token")
        if isinstance(data, dict):
            for key in candidates:
                v = data.get(key)
                if isinstance(v, str) and len(v) > 10:
                    token = v
                    break
            if not token and isinstance(data.get("data"), dict):
                inner = data["data"]
                for key in candidates:
                    v = inner.get(key)
                    if isinstance(v, str) and len(v) > 10:
                        token = v
                        break
        if token:
            self._bearer = token
            self.session.headers["Authorization"] = f"Bearer {token}"
        n_cookies = len(self.session.cookies)
        self.logger.info(
            f"mci: verify-otp OK (HTTP {r.status_code}, "
            f"cookies={n_cookies}, bearer={'yes' if token else 'no'})"
        )
        if not token and n_cookies == 0:
            raise RuntimeError(
                "mci: verify-otp returned no cookies and no token; "
                f"response preview: {r.text[:200]!r}"
            )
        self._save_state()

    def fetch_quota(self) -> dict:
        """GET packages-details. Returns
        {"unused_gb": float, "unit": str, "raw": dict}.
        Raises PermissionError on 401/403 so the watcher knows to
        re-run the OTP flow; raises RuntimeError on other failures."""
        url = self.BASE + MCI_ENDPOINT_QUOTA
        r = self.session.get(url, timeout=self.timeout)
        if r.status_code in (401, 403):
            raise PermissionError(
                f"mci: quota HTTP {r.status_code} (session expired)"
            )
        if not r.ok:
            preview = r.text[:200].replace("\n", " ")
            raise RuntimeError(
                f"mci: quota HTTP {r.status_code}: {preview!r}"
            )
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"mci: quota response not JSON: {e}")
        unused = data.get("totalUnusedBytes")
        unit = (
            data.get("bytesUnusedUnit")
            or data.get("bytesUnit")
            or "گیگ"
        )
        if unused is None:
            raise RuntimeError(
                "mci: quota response missing totalUnusedBytes; "
                f"preview: {json.dumps(data, ensure_ascii=False)[:200]}"
            )
        try:
            gb = float(unused)
        except (TypeError, ValueError):
            raise RuntimeError(
                f"mci: totalUnusedBytes not numeric: {unused!r}"
            )
        return {"unused_gb": gb, "unit": str(unit), "raw": data}


class MciWatcher:
    """Polls the MCI panel's remaining-quota figure on an adaptive
    cadence and enforces a per-day usage cap with two batch-file hooks.

    **Daily usage model** (usage = drop from a day-begin baseline, since
    the panel only reports a large rolling ``remaining`` figure, not
    daily usage):

    1. **Day begin** — the first tick of each local calendar day fetches
       the remaining quota, stores it as the day's baseline
       (``day_begin_quota_gb`` + ``day_begin_date``) and launches the
       configured ``mci_day_begin_bat``. Usage for the rest of the day is
       ``baseline − current_remaining`` (clamped at 0 if a top-up raises
       the remaining figure).

    2. **Adaptive polling** — normally every ``mci_interval_seconds``
       (default 3600 s). Once today's usage reaches
       ``mci_fast_poll_usage_gb`` (default 4.0 GB) polling accelerates to
       ``mci_fast_poll_interval_seconds`` (default 600 s) so the cap is
       caught promptly.

    3. **Cap reached** — when today's usage reaches
       ``mci_quota_reached_usage_gb`` (default 4.9 GB) the watcher
       launches ``mci_quota_reached_bat`` exactly once (``reached_date``
       guard) and then pauses polling until the next day-begin.

    **Auth** is JWT Bearer, refreshed via SMS-OTP whenever the panel
    returns 401/403 — an async flow decoupling "I need quota" from "OTP
    arrived":

      - ``fetch_quota`` HTTP 200 → success path (log, baseline/usage,
        cap-check, notifications).
      - HTTP 401/403 → clear cookies/bearer, set ``_pending_quota_check``,
        POST send-otp (subject to a per-OTP cooldown) and return; the
        loop stays free.
      - ``on_otp_received(code)`` (called from the Fetcher thread when an
        OTP SMS lands) verifies and, if a check was pending, runs the
        deferred fetch.
      - ``trigger_now()`` (tray *Check MCI quota*) forces an immediate
        re-run, bypassing the cap-pause and the once-per-day notify gate.

    OTP attempt lifecycle: each send-otp opens an active wait window of
    ``mci_otp_wait_seconds`` (default 60 s) during which the icon blinks.
    If the code arrives, verify → deferred fetch, and the attempt counter
    resets. If the window lapses, the blink stops and the watcher backs
    off per ``mci_otp_backoff_seconds`` (5 min → 30 min → 60 min → 6 h,
    last rung repeating forever) before requesting another OTP. Any
    successful auth/fetch resets the ladder.

    State persisted to ``mci_state.json`` extras:
      - ``day_begin_date`` (YYYY-MM-DD) / ``day_begin_quota_gb`` (float)
        — today's baseline.
      - ``reached_date`` (YYYY-MM-DD) — the day the cap fired (also the
        polling-paused marker).
      - ``notify_date`` (YYYY-MM-DD) — once-per-day notification cap.
    In-memory only (don't survive restart, which is fine): the
    pending-OTP / pending-day-begin / pending-notify flags, the OTP
    cooldown timestamp, and the last computed usage.

    Skipped entirely when ``mci_enabled`` is false. Errors inside
    tick() and on_otp_received() are caught and logged, so a broken
    MCI path never affects the Fetcher / Notifier / Relayer /
    UsageTracker / Telina threads."""

    # Lets the rest of the app initialise before the first possible
    # tick (especially the Fetcher, which may already have an unread
    # OTP SMS from a previous run sitting in sms/).
    STARTUP_GRACE_SECONDS = 20

    def __init__(self, cfg: dict, logger: logging.Logger,
                 quota_warner: "QuotaWarner | None",
                 on_busy_change=None):
        self.cfg = cfg
        self.logger = logger
        self.quota_warner = quota_warner
        # Called with True when an MCI operation begins (a tick's
        # fetch/auth attempt, or the *active* OTP-wait window) and False
        # once it ends. Drives the tray-icon blink. The OTP-wait window
        # is bounded by ``otp_wait_s`` (see _otp_wait_active): once it
        # lapses the blink stops even if the code never arrived — the
        # watcher then backs off and retries. Recomputed at the
        # boundaries of tick()/on_otp_received() and each run-loop wait
        # slice via _refresh_busy().
        self.on_busy_change = on_busy_change
        self._busy = False
        self._busy_active = False   # True while inside tick/on_otp body
        self.app = app_dir()
        self.quota_log_path = (
            self.app / cfg["usage_total_folder"]
            / cfg["mci_quota_log_filename"]
        )
        # Daily usage-cap parameters.
        self.daily_limit_gb = float(cfg.get("mci_daily_limit_gb", 5.0))
        self.reached_usage_gb = float(
            cfg.get("mci_quota_reached_usage_gb", 4.9)
        )
        self.fast_usage_gb = float(cfg.get("mci_fast_poll_usage_gb", 4.0))
        # Below this *remaining* figure pop the orange low-quota warning
        # (separate concern from the daily-usage cap).
        self.warn_below_gb = float(cfg.get("mci_quota_below_gb", 5.0))
        # Poll cadence (seconds): normal, and fast once near the cap.
        self.normal_interval_s = float(cfg.get("mci_interval_seconds", 3600))
        self.fast_interval_s = float(
            cfg.get("mci_fast_poll_interval_seconds", 600)
        )
        self.otp_wait_s = int(cfg.get("mci_otp_wait_seconds", 60))
        # Backoff ladder (seconds) between OTP attempts that timed out.
        # Last rung repeats forever. Sanitised to a non-empty list of
        # positive numbers.
        ladder = cfg.get("mci_otp_backoff_seconds") or [300, 1800, 3600, 21600]
        try:
            self.otp_backoff_s = [float(x) for x in ladder if float(x) > 0]
        except (TypeError, ValueError):
            self.otp_backoff_s = [300.0, 1800.0, 3600.0, 21600.0]
        if not self.otp_backoff_s:
            self.otp_backoff_s = [300.0, 1800.0, 3600.0, 21600.0]
        self.max_wait_slice_s = max(
            1.0, float(cfg.get("mci_max_wait_slice_seconds", 60))
        )
        # Batch-file hooks (resolved against the app dir if relative).
        self.day_begin_bat = str(cfg.get("mci_day_begin_bat", "") or "")
        self.quota_reached_bat = str(
            cfg.get("mci_quota_reached_bat", "") or ""
        )
        self.client = MciClient(
            username=str(cfg["mci_username"]),
            state_path=self.app / cfg["mci_state_file"],
            timeout=float(cfg.get("request_timeout_seconds", 15)),
            logger=logger,
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        # Serialises tick() against on_otp_received(). Both call into
        # the client (which itself shares one requests.Session), so a
        # second concurrent call could leak partial state. RLock so a
        # nested re-entry from within the same thread is safe.
        self._client_lock = threading.RLock()
        # In-memory state — does NOT survive restart, which is fine.
        # If the app dies while waiting for an OTP, the next start will
        # try fetch_quota again, get 401, and start a fresh OTP cycle.
        self._pending_quota_check = False
        self._last_otp_sent_at: "float | None" = None
        # OTP attempt lifecycle (all monotonic seconds / counts):
        #   _otp_deadline   — while now < this, the current OTP attempt is
        #                     "active" → icon blinks. None when idle.
        #   _otp_attempts   — consecutive timed-out attempts; indexes the
        #                     backoff ladder. Reset on any auth success.
        #   _backoff_until  — don't re-request an OTP before this instant.
        self._otp_deadline: "float | None" = None
        self._otp_attempts = 0
        self._backoff_until: "float | None" = None
        self._force_run = False
        # Whether the next decisive outcome (success or final error) of
        # the current auth/fetch attempt should pop a user-facing
        # notification. Set by tick() — true on manual trigger, or on
        # auto attempts when today != the persisted ``notify_date`` (so
        # the hourly poll notifies at most once per day). Reset after any
        # popup is enqueued. Sticky across the async OTP-wait window.
        self._pending_notify = False
        # Set by tick() on the first tick of a new calendar day; consumed
        # by _on_quota_success to establish today's baseline and launch
        # the day-begin hook once the fresh reading is in hand.
        self._pending_day_begin = False
        # Last computed usage (GB) — drives the adaptive wake interval.
        # -1 = unknown (no successful reading yet this run).
        self._last_usage_gb = -1.0
        # Drop stale keys from the previous slot-based scheduler.
        self._cleanup_old_state()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _otp_wait_active(self) -> bool:
        """True while inside the active OTP-wait window of the current
        attempt — i.e. we sent an OTP and are still within
        ``otp_wait_s`` of it. The icon blinks only during this window,
        NOT during the (potentially hours-long) backoff between attempts.
        Uses a monotonic deadline so a never-arriving OTP can't blink
        forever."""
        return (
            self._otp_deadline is not None
            and time.monotonic() < self._otp_deadline
        )

    def _refresh_busy(self):
        """Recompute the busy state and fire on_busy_change on edges.
        Busy = a tick/on_otp body is executing, OR we're inside the
        active OTP-wait window (see _otp_wait_active). Called at the
        boundaries of tick()/on_otp_received() and from the run loop
        each wait slice (so the blink turns off when the window lapses).
        Caller holds _client_lock."""
        busy = self._busy_active or self._otp_wait_active()
        if busy == self._busy:
            return
        self._busy = busy
        if self.on_busy_change:
            try:
                self.on_busy_change(busy)
            except Exception as e:
                self.logger.error(f"mci: on_busy_change failed: {e}")

    def trigger_now(self):
        """Tray-menu manual fire — re-run the tick now even if today's
        already done or we're before the scheduled fetch time.

        Resets the OTP retry cycle so a manual fetch always starts from
        attempt #1: clears any active OTP-wait window, the backoff, and
        the attempt counter. So if a manual fetch fails it re-enters the
        same 60 s-wait → 5 m/30 m/60 m/6 h ladder from the top, exactly
        like the first auto attempt of the day. Takes _client_lock so it
        doesn't race a tick/on_otp_received in flight; the icon edge is
        refreshed in case an active blink window was just cleared."""
        with self._client_lock:
            self._reset_otp_cycle()
            self._refresh_busy()
            self._force_run = True
        self._wake.set()

    def _next_wake_seconds(self) -> float:
        """Adaptive cadence. Always wake at the next local midnight +1 s
        so a new day's day-begin fires promptly. Otherwise:

        - if today's cap already fired (``reached_date`` == today), poll
          is paused — sleep until midnight only;
        - else poll every ``mci_interval_seconds``, accelerating to
          ``mci_fast_poll_interval_seconds`` once today's usage has
          reached ``mci_fast_poll_usage_gb``.

        Same precision-tick pattern as ``UsageTracker``."""
        now = datetime.now()
        next_midnight = datetime.combine(
            now.date() + timedelta(days=1), dtime.min,
        )
        secs_to_midnight = (next_midnight - now).total_seconds() + 1.0
        # An OTP auth cycle is in flight: wake promptly so the active
        # window can time out, or so the backoff retry can fire. Both are
        # capped by the slice in run(), but ask for the precise instant.
        if self._otp_deadline is not None:
            return max(1.0, self._otp_deadline - time.monotonic())
        if self._backoff_until is not None:
            return max(1.0, self._backoff_until - time.monotonic())
        # Cap reached for the day → idle until the new day begins.
        if self.client.get_extra("reached_date") == self._today_str():
            return max(1.0, secs_to_midnight)
        interval = self.normal_interval_s
        if self._last_usage_gb >= self.fast_usage_gb:
            interval = self.fast_interval_s
        return max(1.0, min(interval, secs_to_midnight))

    def run(self):
        if self._stop.wait(self.STARTUP_GRACE_SECONDS):
            return
        while not self._stop.is_set():
            # Clear before the tick so a trigger_now() during the tick
            # is captured by the subsequent wait (causing an immediate
            # next tick). Same pattern as Fetcher.run().
            self._wake.clear()
            try:
                self.tick()
            except Exception as e:
                self.logger.error(f"mci: tick error: {e}", exc_info=True)
            # Sleep until the next scheduled wake, but never in one block
            # longer than max_wait_slice_s — so a lapsed OTP-wait window
            # is detected (blink dropped, backoff scheduled) within that
            # bound even when a long backoff/midnight sleep is pending.
            remaining = self._next_wake_seconds()
            while remaining > 0 and not self._stop.is_set():
                slice_s = min(remaining, self.max_wait_slice_s)
                if self._wake.wait(slice_s):
                    break  # woken early (trigger / stop / async otp result)
                remaining -= slice_s
                # Close a lapsed OTP-wait window promptly between ticks.
                with self._client_lock:
                    self._check_otp_timeout()
                    if self._backoff_until is not None and \
                            time.monotonic() >= self._backoff_until:
                        break  # backoff elapsed → re-tick now to retry

    # ---- date tracking / housekeeping ----

    @staticmethod
    def _today_str() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _cleanup_old_state(self):
        """Drop keys written by the previous slot-based scheduler so the
        state file doesn't accrete dead fields after an upgrade."""
        for k in (
            "last_check_date", "last_notify_date",
            "check_date_early", "check_date_evening",
            "notify_date_early", "notify_date_evening",
        ):
            if self.client.get_extra(k) is not None:
                self.client.del_extra(k)

    def _run_bat(self, label: str, path_str: str):
        """Fire-and-forget launch of a configured batch file. Relative
        paths resolve against the app dir. Missing/empty paths are logged
        and skipped — never fatal (the watcher keeps polling)."""
        path_str = (path_str or "").strip()
        if not path_str:
            self.logger.warning(
                f"mci: {label} bat not configured (empty path); skipping"
            )
            return
        p = Path(path_str)
        if not p.is_absolute():
            p = self.app / p
        if not p.exists():
            self.logger.error(
                f"mci: {label} bat not found at {p}; skipping"
            )
            return
        try:
            subprocess.Popen(
                ["cmd", "/c", str(p)],
                cwd=str(p.parent),
                close_fds=True,
            )
            self.logger.info(f"mci: launched {label} bat: {p}")
        except Exception as e:
            self.logger.error(
                f"mci: failed to launch {label} bat ({p}): {e}"
            )

    # ---- tick (auto or tray-triggered) ----

    def tick(self):
        with self._client_lock:
            # Auto tick landing inside an OTP backoff window does no
            # network work (see _tick_locked) — don't even raise the busy
            # flag, or the icon would blink for the instant the skip
            # takes. A forced (manual) tick clears the backoff and runs.
            if not self._force_run and self._backoff_until is not None \
                    and time.monotonic() < self._backoff_until:
                self._tick_locked()
                return
            self._busy_active = True
            self._refresh_busy()
            try:
                self._tick_locked()
            finally:
                self._busy_active = False
                self._refresh_busy()

    def _tick_locked(self):
        """Body of tick(); caller holds _client_lock and manages the
        busy flag."""
        force = self._force_run
        self._force_run = False
        today = self._today_str()
        # First tick of a new calendar day (or first run ever): the
        # upcoming fetch establishes today's baseline and runs the
        # day-begin hook. A new day also implicitly clears yesterday's
        # cap/notify gating (they key off the date).
        new_day = self.client.get_extra("day_begin_date") != today
        if new_day:
            self._pending_day_begin = True
        if force:
            # Manual trigger: bypass the cap-pause, the OTP backoff, and
            # always notify. Clear the backoff so _maybe_request_otp will
            # send immediately.
            self._pending_notify = True
            self._backoff_until = None
            self.logger.info("mci: force-triggered tick")
        else:
            # Auto path: while inside an OTP backoff window, don't touch
            # the panel at all — a fetch would just 403 again and the
            # OTP re-send is gated anyway. Skipping avoids a wasted round
            # trip and the brief busy-flicker it causes. The run loop
            # wakes us when the backoff elapses.
            if self._backoff_until is not None and \
                    time.monotonic() < self._backoff_until:
                left = int(self._backoff_until - time.monotonic())
                self.logger.info(
                    f"mci: in OTP backoff ({left} s left); skipping tick"
                )
                return
            # Auto path: if today's cap already fired, polling is
            # paused until the next day-begin — skip (a new day is
            # never "already reached", so day-begin still runs).
            if not new_day and \
                    self.client.get_extra("reached_date") == today:
                return
            # Once-per-day notification cap: an outage that keeps the
            # hourly poll failing won't pop more than one popup/day.
            if self.client.get_extra("notify_date") != today:
                self._pending_notify = True
            self.logger.info(
                f"mci: tick (today={today}, new_day={new_day})"
            )
        self._try_fetch_or_request_otp()

    def _try_fetch_or_request_otp(self):
        """Caller must hold ``_client_lock``."""
        try:
            result = self.client.fetch_quota()
        except PermissionError as e:
            self.logger.info(f"{e}")
            self.client.clear_session()
            self._pending_quota_check = True
            self._maybe_request_otp()
            return
        except Exception as e:
            self.logger.error(f"mci: fetch_quota error: {e}")
            self._notify_error(f"Failed to fetch quota: {e}")
            return
        self._reset_otp_cycle()
        self._on_quota_success(result)

    def _reset_otp_cycle(self):
        """Clear the OTP attempt/backoff state after a successful auth or
        fetch. Caller must hold ``_client_lock``."""
        self._otp_deadline = None
        self._otp_attempts = 0
        self._backoff_until = None
        self._last_otp_sent_at = None
        self._pending_quota_check = False

    def _backoff_seconds_for(self, attempt: int) -> float:
        """Backoff for the given (0-based) consecutive-timeout count.
        Walks the ladder, then sticks on the last rung forever."""
        idx = min(attempt, len(self.otp_backoff_s) - 1)
        return self.otp_backoff_s[idx]

    def _maybe_request_otp(self):
        """POST send-otp and open a fresh OTP-wait window, unless we're
        still inside the active window of the previous attempt or inside
        the post-timeout backoff wait. Caller must hold ``_client_lock``.

        On success this sets ``_otp_deadline`` (drives the icon blink and
        the timeout) and stamps the send time (the re-send cooldown)."""
        now = time.monotonic()
        # Still waiting on the current attempt's SMS — let it run.
        if self._otp_deadline is not None and now < self._otp_deadline:
            left = int(self._otp_deadline - now)
            self.logger.info(
                f"mci: OTP attempt still active ({left} s left); "
                "not re-sending"
            )
            return
        # In the backoff gap between attempts — the run loop will wake us
        # when it elapses.
        if self._backoff_until is not None and now < self._backoff_until:
            left = int(self._backoff_until - now)
            self.logger.info(
                f"mci: OTP backoff active ({left} s left, "
                f"attempt #{self._otp_attempts + 1} next); not re-sending"
            )
            return
        try:
            self.client.request_otp()
            self._last_otp_sent_at = now
            self._otp_deadline = now + self.otp_wait_s
            self._backoff_until = None
            self.logger.info(
                f"mci: send-otp dispatched (attempt "
                f"#{self._otp_attempts + 1}); awaiting SMS for "
                f"{self.otp_wait_s} s "
                "(Fetcher will call on_otp_received when it lands)"
            )
        except Exception as e:
            # Treat a failed send like a timed-out attempt so we still
            # back off instead of hammering the endpoint every loop.
            self.logger.error(f"mci: request_otp failed: {e}")
            self._otp_deadline = None
            self._schedule_otp_backoff()
            self._notify_error(f"Failed to start auth: {e}")

    def _schedule_otp_backoff(self):
        """Advance the backoff ladder after a failed/timed-out attempt.
        Caller must hold ``_client_lock``."""
        wait_s = self._backoff_seconds_for(self._otp_attempts)
        self._otp_attempts += 1
        self._backoff_until = time.monotonic() + wait_s
        self.logger.warning(
            f"mci: OTP attempt #{self._otp_attempts} timed out; "
            f"backing off {int(wait_s)} s before retry "
            f"(attempt #{self._otp_attempts + 1})"
        )

    def _check_otp_timeout(self):
        """Called from the run loop each wait slice (holds the client
        lock). If an OTP-wait window has lapsed without on_otp_received,
        close it, drop the blink, and schedule the next backoff retry.
        One-time-per-day notification on the first timeout of the day."""
        if self._otp_deadline is None:
            return
        if time.monotonic() < self._otp_deadline:
            return
        self._otp_deadline = None
        self._refresh_busy()  # window closed → icon stops blinking
        # Notify once/day that auth is stalling (gated like other MCI
        # popups via _pending_notify, set by tick()).
        self._notify_error("MCI auth: OTP SMS did not arrive in time")
        self._schedule_otp_backoff()

    # ---- OTP callback from the Fetcher ----

    def on_otp_received(self, code: str):
        """Called from the Fetcher thread when an SMS matching the
        configured ``mci_otp_match_substring`` is saved. Verifies the
        code with MCI; if a quota check is pending (we triggered
        send-otp earlier), runs the deferred fetch_quota now and marks
        today done. Otherwise (spontaneous OTP from user's own browser
        login) just refreshes our session.

        Race-safe against tick() via ``_client_lock``: never two
        concurrent calls into ``self.client``."""
        if not code:
            return
        with self._client_lock:
            self._busy_active = True
            self._refresh_busy()
            try:
                self._on_otp_received_locked(code)
            finally:
                self._busy_active = False
                self._refresh_busy()

    def _on_otp_received_locked(self, code: str):
        """Body of on_otp_received(); caller holds _client_lock and
        manages the busy flag."""
        if True:
            self.logger.info(
                f"mci: OTP received (len={len(code)}); verifying"
            )
            try:
                self.client.verify_otp(code)
            except Exception as e:
                self.logger.warning(
                    f"mci: verify_otp failed: {e} "
                    "(may have been consumed elsewhere — fine)"
                )
                # Only surface a notification if a quota check was
                # actually depending on this verify. A failing verify
                # for a spontaneous (user-browser) OTP isn't actionable
                # for the user.
                if self._pending_quota_check:
                    self._pending_quota_check = False
                    self._notify_error(f"Verify failed: {e}")
                return
            # Successful verify ends the auth cycle: close the OTP-wait
            # window (stops the blink), reset the backoff ladder, and
            # allow a subsequent 401 to request a fresh OTP immediately.
            was_pending = self._pending_quota_check
            self._reset_otp_cycle()
            self._refresh_busy()
            if not was_pending:
                self.logger.info(
                    "mci: session refreshed opportunistically "
                    "(no quota check was pending)"
                )
                return
            try:
                result = self.client.fetch_quota()
            except Exception as e:
                self.logger.error(
                    f"mci: deferred fetch_quota after verify failed: {e}"
                )
                self._notify_error(
                    f"Failed to fetch quota after auth: {e}"
                )
                return
            self._on_quota_success(result)
            # This reading arrived asynchronously: the watcher loop sent
            # send-otp in tick() and then computed its sleep from the
            # PREVIOUS usage — so it may be parked for a full normal
            # interval even though usage just crossed into fast-poll (or
            # cap) territory. Wake it to reschedule against the fresh
            # usage. The re-tick's GET reuses the session we just
            # verified (this fetch returned 200), so it won't burn an OTP.
            self._wake.set()

    # ---- success path ----

    def _on_quota_success(self, result: dict):
        remaining_gb = result["unused_gb"]
        unit = result["unit"]
        today = self._today_str()
        self.logger.info(f"mci: remaining quota = {remaining_gb:.2f} {unit}")
        self._log_quota_line(remaining_gb)

        # --- day-begin: establish today's baseline + run the hook once ---
        if self._pending_day_begin:
            self._pending_day_begin = False
            self.client.set_extra("day_begin_quota_gb", remaining_gb)
            self.client.set_extra("day_begin_date", today)
            self.logger.info(
                f"mci: day-begin baseline set = {remaining_gb:.2f} GB"
            )
            self._run_bat("day-begin", self.day_begin_bat)

        # --- today's usage = baseline − current remaining ---
        baseline = self.client.get_extra("day_begin_quota_gb")
        try:
            baseline = float(baseline) if baseline is not None else None
        except (TypeError, ValueError):
            baseline = None
        if baseline is None:
            usage = 0.0
            self.logger.warning(
                "mci: no day-begin baseline yet; usage treated as 0"
            )
        else:
            usage = max(0.0, baseline - remaining_gb)
        self._last_usage_gb = usage
        self.logger.info(
            f"mci: today usage = {usage:.2f} GB / "
            f"{self.daily_limit_gb:.2f} GB cap (remaining={remaining_gb:.2f})"
        )

        # --- cap reached: run the hook once, then pause polling ---
        if usage >= self.reached_usage_gb and \
                self.client.get_extra("reached_date") != today:
            self.client.set_extra("reached_date", today)
            self.logger.warning(
                f"mci: daily usage cap reached "
                f"({usage:.2f} >= {self.reached_usage_gb:.2f} GB); "
                f"launching quota-reached bat, polling paused until "
                f"next day"
            )
            self._run_bat("mci-quota-reached", self.quota_reached_bat)
            if self.quota_warner is not None:
                self.quota_warner.enqueue_cap(
                    usage, remaining_gb, self.daily_limit_gb
                )
            self._pending_notify = False
            return

        # --- routine notifications (once/day, or on manual trigger) ---
        if self.quota_warner is None:
            if remaining_gb < self.warn_below_gb:
                self.logger.warning(
                    f"mci: {remaining_gb:.2f} GB remaining but no "
                    f"quota_warner is wired up"
                )
            self._pending_notify = False
            return
        if self._pending_notify:
            if remaining_gb < self.warn_below_gb:
                self.quota_warner.enqueue_warning(remaining_gb)
            else:
                self.quota_warner.enqueue_info(remaining_gb)
            self.client.set_extra("notify_date", today)
        self._pending_notify = False

    # ---- error notification ----

    def _notify_error(self, message: str):
        """Enqueue an error popup if this attempt was flagged
        notify-worthy by tick(). Always logs at the call sites.
        Clears ``_pending_notify`` and stamps ``notify_date`` so same-day
        retries don't spam."""
        if not self._pending_notify:
            return
        self._pending_notify = False
        if self.quota_warner is None:
            self.logger.warning(
                f"mci: would notify error but no quota_warner is wired up: "
                f"{message}"
            )
            return
        self.quota_warner.enqueue_error(message)
        self.client.set_extra("notify_date", self._today_str())

    # ---- file log ----

    def _log_quota_line(self, gb: float):
        """Insert or overwrite (keyed by timestamp prefix) one
        `ts  gb GB` line in the per-app quota log."""
        ts = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        value = f"{gb:.2f} GB"
        try:
            self.quota_log_path.parent.mkdir(parents=True, exist_ok=True)
            lines: list[str] = []
            if self.quota_log_path.exists():
                for raw in self.quota_log_path.read_text(
                    encoding="utf-8"
                ).splitlines():
                    if not raw.strip():
                        continue
                    if raw.startswith(ts + " "):
                        continue
                    lines.append(raw)
            lines.append(f"{ts}  {value}")
            lines.sort()
            tmp = self.quota_log_path.with_suffix(".txt.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(self.quota_log_path)
        except Exception as e:
            self.logger.error(
                f"mci: write {self.quota_log_path.name} failed: {e}"
            )


# ---------- tray icon image ----------

def make_icon_image(unread: int = 0, relaying: bool = False,
                    disabled: bool = False,
                    blink_off: bool = False) -> Image.Image:
    """Black 'S' on a filled circle, transparent background. Base
    states: yellow while a relay (FTP upload + outbound SMS) is in
    flight, red while toasts are awaiting acknowledgement, green when
    idle. Yellow wins over red because it is transient and meaningful
    (the user is actively being forwarded an SMS via the modem).

    The mobile-network "Disabled" tray toggle layers two extra states
    on top:
    - ``disabled=True`` greys the circle (gray fill) to convey that 4G
      internet is turned off, overriding the colour states above.
    - ``blink_off=True`` renders the dim frame of a blink cycle (used
      while a disable/enable transition is in flight): the circle and
      glyph are drawn faint so alternating with the normal frame on a
      timer reads as a blink. Honoured in both the enabled and disabled
      colourings."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if disabled:
        circle_fill = (150, 150, 150, 255)
    elif relaying:
        circle_fill = (240, 200, 0, 255)
    elif unread > 0:
        circle_fill = (210, 30, 30, 255)
    else:
        circle_fill = (0, 170, 60, 255)
    glyph_color = (0, 0, 0, 255)
    if blink_off:
        # Dim frame: fade the circle toward the transparent background
        # and lighten the glyph so the icon visibly pulses.
        r, g, b, _ = circle_fill
        circle_fill = (
            (r + 255) // 2, (g + 255) // 2, (b + 255) // 2, 90,
        )
        glyph_color = (90, 90, 90, 120)
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
        text, fill=glyph_color, font=font,
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


# Accent colours for MCI quota popups — chosen to be distinguishable
# both from each other and from the SMS toast's blue.
POPUP_ACCENT_WARN = "#e07a3a"   # orange  — low quota
POPUP_ACCENT_INFO = "#3aa84a"   # green   — normal quota reading
POPUP_ACCENT_ERROR = "#c93838"  # red     — fetch / auth failure


def show_quota_popup(parent: tk.Tk, kind: str, value, on_ack):
    """Bottom-right Tk popup for MCI quota events. ``kind`` is one of:

    - ``"warning"``: ``value`` is float GB; body reads
      ``"Internet quota is low: N.NN GB remaining."`` (orange).
    - ``"info"``:    ``value`` is float GB; body reads
      ``"Remaining quota: N.NN GB."`` (green).
    - ``"error"``:   ``value`` is a string message; body shows the
      error (red).

    Stacks alongside SMS toasts via the same TOAST_SLOTS. on_ack() is
    called exactly once when the popup is dismissed."""
    if kind == "warning":
        accent = POPUP_ACCENT_WARN
        header_text = "Internet quota low"
        body_text = f"Internet quota is low: {float(value):.2f} GB remaining."
    elif kind == "info":
        accent = POPUP_ACCENT_INFO
        header_text = "MCI quota update"
        body_text = f"Remaining quota: {float(value):.2f} GB."
    elif kind == "cap":
        accent = POPUP_ACCENT_WARN
        header_text = "MCI daily cap reached"
        if isinstance(value, dict):
            body_text = (
                f"Daily usage {float(value.get('usage', 0)):.2f} GB of "
                f"{float(value.get('limit', 0)):.2f} GB cap reached "
                f"({float(value.get('remaining', 0)):.2f} GB remaining). "
                f"Quota-reached action launched."
            )
        else:
            body_text = str(value)
    elif kind == "error":
        accent = POPUP_ACCENT_ERROR
        header_text = "MCI quota fetch failed"
        # Keep the body to a reasonable size — modem-side errors can be
        # long. The full message is in the log file.
        msg = str(value)
        if len(msg) > 240:
            msg = msg[:240].rstrip() + "…"
        body_text = msg
    elif kind == "net":
        # Mobile-network disable/enable result. `value` is a dict
        # {"header": ..., "body": ...}. Red header when the toggle
        # failed (header contains "failed"), else informational.
        header_text = str((value or {}).get("header", "4G internet"))
        body_text = str((value or {}).get("body", ""))
        accent = (
            POPUP_ACCENT_ERROR if "fail" in header_text.lower()
            else POPUP_ACCENT_INFO
        )
    else:
        accent = POPUP_ACCENT_INFO
        header_text = "MCI quota"
        body_text = str(value)

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

    def _close():
        if closed["v"]:
            return
        closed["v"] = True
        TOAST_SLOTS.release(slot)
        try:
            win.destroy()
        except Exception:
            pass
        try:
            on_ack()
        except Exception:
            pass

    bar = tk.Frame(win, bg=accent, width=4)
    bar.pack(side="left", fill="y")

    inner = tk.Frame(win, bg=POPUP_BG)
    inner.pack(side="left", fill="both", expand=True)

    header = tk.Frame(inner, bg=POPUP_BG)
    header.pack(fill="x", padx=10, pady=(8, 2))
    tk.Label(
        header, text=header_text,
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
    xbtn.bind("<Button-1>", lambda _e: _close())

    btns = tk.Frame(inner, bg=POPUP_BG)
    btns.pack(side="bottom", fill="x", padx=10, pady=(4, 10))
    tk.Button(
        btns, text="Dismiss", width=10, height=1, command=_close,
    ).pack(side="right")

    tk.Label(
        inner, text=body_text,
        bg=POPUP_BG, fg=POPUP_FG,
        font=("Tahoma", 10),
        wraplength=POPUP_W - 30, justify="left", anchor="nw",
    ).pack(fill="both", expand=True, padx=10, pady=2)


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

    def _copy(_event=None):
        # On a "disabled" Text the built-in <<Copy>> binding is a no-op,
        # so Ctrl+C / Ctrl+Insert never reach the clipboard. Read the
        # current selection ourselves and push it, falling back to the
        # whole body if nothing is selected.
        try:
            sel = txt.get("sel.first", "sel.last")
        except tk.TclError:
            sel = ""
        if not sel:
            sel = txt.get("1.0", "end-1c")
        win.clipboard_clear()
        win.clipboard_append(sel)
        return "break"

    txt.bind("<Control-a>", _select_all)
    txt.bind("<Control-A>", _select_all)
    txt.bind("<Control-c>", _copy)
    txt.bind("<Control-C>", _copy)
    txt.bind("<Control-Insert>", _copy)

    # --- RTL toggle: flips justification of the whole SMS body ---
    rtl = {"v": False}

    def _toggle_rtl():
        rtl["v"] = not rtl["v"]
        txt.config(state="normal")
        txt.tag_configure(
            "body", justify=("right" if rtl["v"] else "left"))
        txt.tag_add("body", "1.0", "end-1c")
        txt.config(state="disabled")
        rtl_btn.config(text=("RTL ✓" if rtl["v"] else "RTL"))

    btnbar = tk.Frame(win)
    btnbar.pack(pady=(2, 10))
    rtl_btn = tk.Button(btnbar, text="RTL", width=6, command=_toggle_rtl)
    rtl_btn.pack(side="left", padx=(0, 8))
    btn = tk.Button(btnbar, text="Close", width=12, command=_close)
    btn.pack(side="left")
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

    # Quota-low warner — fed by MciWatcher when a scrape returns below
    # threshold. Started only when MCI is enabled, since it's the sole
    # producer.
    quota_warner: "QuotaWarner | None" = None
    if cfg.get("mci_enabled", True):
        quota_warner = QuotaWarner(ui_queue, logger)
        quota_thread = threading.Thread(target=quota_warner.run, daemon=True)
        quota_thread.start()
        logger.info("quota: warner thread started")

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
    # disabled: 4G internet currently turned off (gray icon).
    # transitioning: a disable/enable POST is in flight (icon blinks).
    # mci_busy: an MCI quota fetch / OTP-wait operation is in flight
    #   (icon blinks). blink_off: current blink frame (toggled by the
    #   blink thread). The icon blinks whenever ANY busy flag is set.
    icon_state = {
        "unread": 0, "relaying": False,
        "disabled": False, "transitioning": False,
        "mci_busy": False, "blink_off": False,
    }
    icon_state_lock = threading.Lock()

    def _blink_active_locked() -> bool:
        # Caller holds icon_state_lock. Blink if any tracked background
        # operation is running (4G toggle transition or MCI fetch/OTP).
        return icon_state["transitioning"] or icon_state["mci_busy"]

    def _refresh_icon():
        # Gray (disabled) overrides the colour states; otherwise yellow
        # (relaying) wins over red (unread) wins over green (idle).
        # blink_off renders the dim frame while any background op is in
        # flight (4G toggle transition or MCI fetch/OTP). pystray
        # supports updating icon.icon and icon.title from any thread, but
        # bouncing through one helper keeps the state machine in one place.
        icon = icon_holder.get("icon")
        if icon is None:
            return
        with icon_state_lock:
            unread = icon_state["unread"]
            relaying = icon_state["relaying"]
            disabled = icon_state["disabled"]
            transitioning = icon_state["transitioning"]
            mci_busy = icon_state["mci_busy"]
            blink_off = icon_state["blink_off"] and _blink_active_locked()
        try:
            icon.icon = make_icon_image(
                unread=unread, relaying=relaying,
                disabled=disabled, blink_off=blink_off,
            )
            suffix = []
            if disabled:
                suffix.append("4G disabled")
            if transitioning:
                suffix.append("switching…")
            if mci_busy:
                suffix.append("checking quota…")
            if relaying:
                suffix.append("relaying")
            if unread:
                suffix.append(f"{unread} unread")
            icon.title = (
                f"{APP_NAME} ({', '.join(suffix)})" if suffix else APP_NAME
            )
        except Exception as e:
            logger.error(f"icon update failed: {e}", exc_info=True)

    def on_mci_busy_change(on: bool):
        with icon_state_lock:
            icon_state["mci_busy"] = on
        _refresh_icon()
        logger.info(f"icon: mci_busy={on}")

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

    # --- mobile-network "Disabled" tray toggle ---
    #
    # State the menu's checkbox reflects (checked == 4G internet disabled).
    # Default unchecked. A single worker at a time owns the modem POST; the
    # busy flag both serialises clicks and tells the menu to grey-out the
    # item while a transition is in flight.
    net_toggle = {"checked": False, "busy": False}
    net_toggle_lock = threading.Lock()

    # Blink driver: while any tracked background operation is in flight
    # (a disable/enable POST, or an MCI quota fetch / OTP-wait), alternate
    # the dim/normal frame on a timer so the tray icon visibly blinks.
    # Idle otherwise. This blinks the icon in the system tray for ANY
    # observer, not just this app's own windows.
    blink_stop = threading.Event()

    def _blink_loop():
        while not blink_stop.is_set():
            with icon_state_lock:
                active = _blink_active_locked()
                if active:
                    icon_state["blink_off"] = not icon_state["blink_off"]
                else:
                    icon_state["blink_off"] = False
            if active:
                _refresh_icon()
            blink_stop.wait(0.5)

    blink_thread = threading.Thread(target=_blink_loop, daemon=True)
    blink_thread.start()

    def _notify_net(kind: str, message: str):
        """Surface a disable/enable result to the user via the shared
        toast path (reuses show_quota_popup's plain rendering)."""
        ui_queue.put((
            "show_quota_popup",
            {"kind": "net", "value": {"header": kind, "body": message},
             "on_ack": None},
        ))

    def _apply_net_disabled(want_disabled: bool):
        """Worker: start the icon blinking, POST the new Mobile-Network
        Enabled state to the modem (Enabled = not disabled), then settle
        the icon to gray (disabled) or normal (enabled). On any failure,
        revert the menu toggle, stop blinking, restore the prior icon and
        notify the user (see AskUserQuestion: "Revert toggle + notify")."""
        with icon_state_lock:
            icon_state["transitioning"] = True
        _refresh_icon()
        ok = False
        try:
            fetcher.modem.set_mobile_network_enabled(not want_disabled)
            ok = True
        except Exception as e:
            logger.error(
                f"net-toggle: failed to set Enabled={not want_disabled}: "
                f"{e}", exc_info=True,
            )
        with net_toggle_lock:
            if ok:
                net_toggle["checked"] = want_disabled
            else:
                # Revert to the state the modem is actually in.
                net_toggle["checked"] = not want_disabled
            net_toggle["busy"] = False
            settled_disabled = net_toggle["checked"]
        with icon_state_lock:
            icon_state["transitioning"] = False
            icon_state["blink_off"] = False
            icon_state["disabled"] = settled_disabled
        _refresh_icon()
        # Menu checkmark/enabled-state is recomputed by pystray from the
        # callables on next open; force an immediate redraw too.
        try:
            icon = icon_holder.get("icon")
            if icon is not None:
                icon.update_menu()
        except Exception:
            pass
        if ok:
            state = "disabled" if want_disabled else "enabled"
            logger.info(f"net-toggle: 4G internet {state}")
            _notify_net(
                "4G internet " + state,
                f"Mobile Network Enabled checkbox is now "
                f"{'OFF' if want_disabled else 'ON'}.",
            )
        else:
            _notify_net(
                "4G toggle failed",
                "Could not change the modem's Mobile Network Enabled "
                "setting; the toggle was reverted. See the log for "
                "details.",
            )

    def on_net_toggle(_icon, _item):
        with net_toggle_lock:
            if net_toggle["busy"]:
                logger.info("net-toggle: ignored — transition in flight")
                return
            want = not net_toggle["checked"]
            net_toggle["busy"] = True
        logger.info(
            f"tray: Disabled toggled -> want_disabled={want}"
        )
        threading.Thread(
            target=_apply_net_disabled, args=(want,), daemon=True,
        ).start()

    def _net_toggle_checked(_item):
        with net_toggle_lock:
            return net_toggle["checked"]

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

    # MCI panel watcher — reads the remaining-quota figure off the
    # my.mci.ir API once per day, auto-logging-in via SMS-OTP when the
    # server returns 401/403. Flow is asynchronous: tick() requests an
    # OTP and returns immediately; the Fetcher dispatches the digits
    # via mci.on_otp_received() once the SMS lands in sms_del/. Same
    # isolation principle as Telina — exceptions in tick() are caught
    # so a broken MCI poll never affects the main flow.
    mci: "MciWatcher | None" = None
    if cfg.get("mci_enabled", True):
        mci = MciWatcher(
            cfg, logger, quota_warner=quota_warner,
            on_busy_change=on_mci_busy_change,
        )
        # Wire the OTP callback path: Fetcher saves an MCI OTP SMS →
        # extracts the digits → calls mci.on_otp_received(code).
        fetcher.mci_watcher = mci
        mci_thread = threading.Thread(target=mci.run, daemon=True)
        mci_thread.start()
        logger.info("mci: watcher thread started")
    else:
        logger.info("mci: disabled in config")

    def on_log(_icon, _item):
        logger.info("tray: Log clicked")
        ui_queue.put("show_log")

    def on_check_mci(_icon, _item):
        logger.info("tray: Check MCI quota clicked")
        if mci is None:
            logger.warning(
                "tray: Check MCI quota ignored — mci_enabled is false"
            )
            return
        mci.trigger_now()

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
                elif cmd == "show_quota_popup":
                    on_ack = (payload or {}).get("on_ack")
                    kind = str((payload or {}).get("kind", "info"))
                    value = (payload or {}).get("value")
                    try:
                        show_quota_popup(
                            root, kind=kind, value=value, on_ack=on_ack,
                        )
                    except Exception as e:
                        logger.error(
                            f"opening quota popup failed: {e}", exc_info=True
                        )
                        # Free the warner's _showing flag so the queue
                        # can keep draining.
                        if on_ack:
                            try:
                                on_ack()
                            except Exception:
                                pass
                elif cmd == "exit":
                    logger.info(f"--- {APP_NAME} stopping ---")
                    fetcher.stop()
                    notifier.stop()
                    usage.stop()
                    relayer.stop()
                    blink_stop.set()
                    if mci is not None:
                        mci.stop()
                    if quota_warner is not None:
                        quota_warner.stop()
                    try:
                        icon_holder["icon"].stop()
                    except Exception:
                        pass
                    root.destroy()
                    return
        finally:
            root.after(100, pump_ui)

    root.after(100, pump_ui)

    def _net_toggle_enabled(_item):
        # Grey the item out (non-clickable) while a transition is mid-flight.
        with net_toggle_lock:
            return not net_toggle["busy"]

    menu_items = [pystray.MenuItem("Log", on_log)]
    if mci is not None:
        menu_items.append(pystray.MenuItem("Check MCI quota", on_check_mci))
    # Checkable "Disabled" — unchecked by default. Checking it disables the
    # modem's 4G internet (unchecks Mobile Network → Enabled); unchecking
    # re-enables it. The tray icon blinks during the transition, then goes
    # gray when disabled / back to normal colour when enabled.
    # default=True makes a double-click on the tray icon invoke this same
    # toggle (pystray routes icon activation to the default item).
    menu_items.append(pystray.MenuItem(
        "Disabled", on_net_toggle,
        checked=_net_toggle_checked,
        enabled=_net_toggle_enabled,
        default=True,
    ))
    menu_items.append(pystray.MenuItem("Exit", on_exit))

    # Seed the "Disabled" toggle + gray-icon state from the modem's live
    # Mobile Network → Enabled checkbox, BEFORE building the icon, so the
    # first rendered frame and menu checkmark already reflect reality
    # (4G off => checked + gray). Runs ahead of the first MCI fetch (which
    # is behind a 20 s startup grace). A probe failure is non-fatal: leave
    # the defaults (enabled / not gray) and let the toggle work as before.
    try:
        net_enabled = fetcher.modem.get_mobile_network_enabled()
        with net_toggle_lock:
            net_toggle["checked"] = not net_enabled
        with icon_state_lock:
            icon_state["disabled"] = not net_enabled
        logger.info(
            f"net-toggle: initial Mobile Network Enabled={net_enabled} "
            f"(Disabled toggle {'checked' if not net_enabled else 'unchecked'})"
        )
    except Exception as e:
        logger.warning(
            f"net-toggle: could not read initial Enabled state "
            f"({e}); assuming enabled (not disabled)"
        )

    icon = pystray.Icon(
        APP_NAME,
        make_icon_image(disabled=icon_state["disabled"]),
        APP_NAME,
        menu=pystray.Menu(*menu_items),
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
        blink_stop.set()
        if mci is not None:
            mci.stop()
        if quota_warner is not None:
            quota_warner.stop()
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
