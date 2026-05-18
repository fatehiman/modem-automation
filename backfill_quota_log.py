"""
backfill_quota_log.py — one-shot pass over sms_del/ to populate
usage_total/remained_quota_sms.txt from historical SMSes.

Reads smsFetcher.conf for `quota_warn_sender`, `quota_warn_pattern`,
`notified_folder` and `usage_total_folder`, walks every JSON in
sms_del/, applies the same sender+regex parse the live Fetcher uses,
and writes each matched (timestamp, MB) pair into the log file via
the shared `write_quota_line` helper. Safe to re-run — entries with
the same timestamp are overwritten, not duplicated.

Usage:
    python backfill_quota_log.py

Run with smsFetcher.exe stopped to avoid a (very unlikely) race on
the log file.
"""
from __future__ import annotations

import json
import re
import sys

from smsFetcher import (
    QUOTA_LOG_FILENAME,
    app_dir,
    load_or_create_config,
    parse_quota_received_at,
    quota_log_path,
    write_quota_line,
)


def main() -> int:
    cfg, cfg_path = load_or_create_config()
    print(f"using config: {cfg_path}")

    want_sender = str(cfg.get("quota_warn_sender") or "").strip()
    pattern = str(cfg.get("quota_warn_pattern") or "")
    if not want_sender:
        print("error: quota_warn_sender is empty in config")
        return 1
    if "x" not in pattern:
        print("error: quota_warn_pattern has no 'x' placeholder")
        return 1
    before, after = pattern.split("x", 1)
    regex = re.compile(re.escape(before) + r"(\d+)" + re.escape(after))

    del_dir = app_dir() / cfg["notified_folder"]
    if not del_dir.is_dir():
        print(f"error: {del_dir} does not exist or is not a directory")
        return 1

    log_path = quota_log_path(cfg)
    print(f"scanning {del_dir}")
    print(f"writing  {log_path}")

    scanned = sender_match = parsed = written = 0
    for f in sorted(del_dir.glob("*.json")):
        scanned += 1
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  skip {f.name}: cannot read json ({e})")
            continue
        if str(data.get("sender") or "").strip() != want_sender:
            continue
        sender_match += 1
        body = str(data.get("body") or "")
        m = regex.search(body)
        if not m:
            continue
        try:
            mb = int(m.group(1))
        except (ValueError, IndexError):
            continue
        parsed += 1
        ts = parse_quota_received_at(str(data.get("received_at") or ""))
        try:
            write_quota_line(log_path, ts, mb)
            written += 1
            print(f"  {ts}  {mb:>7} MB  <- {f.name}")
        except Exception as e:
            print(f"  failed to write line for {f.name}: {e}")

    print(
        f"done: scanned={scanned} sender_match={sender_match} "
        f"parsed={parsed} written={written}"
    )
    print(f"output: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
