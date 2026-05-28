"""
backfill_quota_log.py — one-shot pass over sms_del/ to populate
usage_total/remained_quota_sms.txt from historical SMSes.

This script targets historical operator quota-status SMSes — the
"HAMRAHAVAL" sender + "برابر با N مگابایت است" pattern that smsFetcher
used to parse at receipt time (replaced in 2026-05 by the my.mci.ir
panel scrape; see the README's "Remaining-quota check via MCI panel"
section). The values are hardcoded here so this script keeps working
after those config keys were removed from smsFetcher.conf. It walks
every JSON in sms_del/ and writes each matched (timestamp, MB) pair
into `usage_total/remained_quota_sms.txt`. Safe to re-run — entries
with the same timestamp are overwritten, not duplicated. The output
file is distinct from the new MCI-panel log (`remained_quota.txt`)
so the two histories stay separate.

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


# Historical operator constants — used to be in smsFetcher.conf as
# `quota_warn_sender` / `quota_warn_pattern`. The live app no longer
# parses operator SMSes for quota; this script remains as a one-shot
# extractor for the historical SMS archive.
HISTORICAL_QUOTA_SENDER = "HAMRAHAVAL"
HISTORICAL_QUOTA_PATTERN = "برابر با x مگابایت است"


def main() -> int:
    cfg, cfg_path = load_or_create_config()
    print(f"using config: {cfg_path}")

    want_sender = HISTORICAL_QUOTA_SENDER
    pattern = HISTORICAL_QUOTA_PATTERN
    if "x" not in pattern:
        print("error: HISTORICAL_QUOTA_PATTERN has no 'x' placeholder")
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
