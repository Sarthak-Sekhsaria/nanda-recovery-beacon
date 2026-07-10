"""Operator CLI.

    python -m app.cli create-key --agent-id planner-1 --admin
    python -m app.cli list-keys
    python -m app.cli revoke-key --agent-id planner-1
    python -m app.cli seed
    python -m app.cli reap [--loop]
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.db import session_scope
from app.logging_config import configure_logging
from app.models import ApiKey
from app.security import create_api_key


def cmd_create_key(args: argparse.Namespace) -> int:
    with session_scope() as db:
        existing = db.execute(select(ApiKey).where(ApiKey.agent_id == args.agent_id)).scalar_one_or_none()
        if existing is not None:
            print(f"error: agent '{args.agent_id}' already has a key. Revoke it first.", file=sys.stderr)
            return 1
        raw = create_api_key(db, agent_id=args.agent_id, label=args.label, is_admin=args.admin)
    print(f"agent_id : {args.agent_id}")
    print(f"is_admin : {args.admin}")
    print(f"api_key  : {raw}")
    print("\nStore this now. Only its SHA-256 hash is saved; it cannot be shown again.")
    return 0


def cmd_list_keys(_: argparse.Namespace) -> int:
    with session_scope() as db:
        rows = db.execute(select(ApiKey).order_by(ApiKey.created_at)).scalars().all()
    if not rows:
        print("no api keys")
        return 0
    print(f"{'agent_id':<32} {'prefix':<14} {'admin':<6} {'active':<7} created_at")
    for key in rows:
        print(
            f"{key.agent_id:<32} {key.key_prefix:<14} {str(key.is_admin):<6} "
            f"{str(key.active):<7} {key.created_at:%Y-%m-%d %H:%M:%S}"
        )
    return 0


def cmd_revoke_key(args: argparse.Namespace) -> int:
    from app.db import utcnow

    with session_scope() as db:
        key = db.execute(select(ApiKey).where(ApiKey.agent_id == args.agent_id)).scalar_one_or_none()
        if key is None:
            print(f"error: no key for agent '{args.agent_id}'", file=sys.stderr)
            return 1
        key.active = False
        key.revoked_at = utcnow()
    print(f"revoked key for {args.agent_id}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from seed import seed  # noqa: PLC0415

    seed(reset=args.reset)
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    from app.reaper import run_forever, run_sweep  # noqa: PLC0415

    if args.loop:
        run_forever(args.interval)
        return 0
    print(run_sweep().as_dict())
    return 0


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="app.cli", description="NANDA Recovery Beacon operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-key", help="Mint an API key for an agent.")
    create.add_argument("--agent-id", required=True)
    create.add_argument("--label", default=None)
    create.add_argument("--admin", action="store_true")
    create.set_defaults(func=cmd_create_key)

    listing = sub.add_parser("list-keys", help="List API keys (hashes are never shown).")
    listing.set_defaults(func=cmd_list_keys)

    revoke = sub.add_parser("revoke-key", help="Deactivate an agent's key.")
    revoke.add_argument("--agent-id", required=True)
    revoke.set_defaults(func=cmd_revoke_key)

    seed_cmd = sub.add_parser("seed", help="Insert realistic sample workflows.")
    seed_cmd.add_argument("--reset", action="store_true", help="Delete existing sample data first.")
    seed_cmd.set_defaults(func=cmd_seed)

    reap = sub.add_parser("reap", help="Run failure detection.")
    reap.add_argument("--loop", action="store_true")
    reap.add_argument("--interval", type=int, default=None)
    reap.set_defaults(func=cmd_reap)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
