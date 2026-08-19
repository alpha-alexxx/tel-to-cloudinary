"""Import a v1 `migrated_ids.json` into the SQLite database.

Only needed if you ran the single-account version of this tool and want its
history to keep suppressing re-uploads. Records are attributed to one account,
so sign that account in first.

    cd backend && python import_legacy.py --list
    cd backend && python import_legacy.py --account 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from config import ConfigError, load_settings
from db import Database


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", type=int, help="Account id to attribute records to")
    parser.add_argument("--list", action="store_true", help="List accounts and exit")
    parser.add_argument("--file", type=Path, help="Path to migrated_ids.json")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 1

    db = Database(settings.db_path)
    db.connect()
    try:
        if args.list or not args.account:
            accounts = await db.list_accounts()
            if not accounts:
                print("No accounts yet. Sign in through the dashboard first.")
                return 1
            print("Accounts:")
            for account in accounts:
                print(f"  {account['id']:>3}  {account['display_name']}"
                      f"  ({account['migrated_total']} records)")
            if not args.account:
                print("\nRe-run with --account <id>.")
                return 0

        source = args.file or settings.legacy_state_file
        if not source.exists():
            print(f"Nothing to import: {source} does not exist.", file=sys.stderr)
            return 1

        raw = json.loads(source.read_text(encoding="utf-8"))
        chats = raw.get("chats", raw)

        imported = 0
        for chat_id, entries in chats.items():
            items = entries.items() if isinstance(entries, dict) else ((m, {}) for m in entries)
            for message_id, record in items:
                await db.mark_migrated(
                    args.account,
                    int(chat_id),
                    int(message_id),
                    filename=record.get("filename", ""),
                    public_id=record.get("public_id", ""),
                    secure_url=record.get("secure_url", ""),
                    resource_type=record.get("resource_type", ""),
                    bytes_uploaded=int(record.get("bytes") or 0),
                )
                imported += 1

        print(f"Imported {imported} record(s) into account {args.account}.")
        print(f"You can now delete {source} if you like — it is no longer read.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
