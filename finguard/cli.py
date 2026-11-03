"""The `finguard` command. The terminal counts as an operator."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core.approvals import CannotDecide
from .core.context import Context
from .core.door import dispatch
from .core.plugin import LoadError, Refused
from .plugins.misses import all_cases, export_case, mark_fixed, replay_case
from .plugins.release import verify_release

ACTOR = "terminal"


def _ctx() -> Context:
    return Context.build()


def _parse_pairs(ctx: Context, name: str, pairs: list[str]) -> dict:
    """key=value into params. Numbers follow the capability's schema, and key=@path reads a file."""
    cap = ctx.registry.get(name)
    props = cap.params.get("properties", {}) if cap else {}
    params: dict = {}
    for pair in pairs:
        key, eq, value = pair.partition("=")
        if not eq or not key:
            raise ValueError(f'"{pair}" is not in the form key=value.')
        kind = props.get(key, {}).get("type")
        if kind == "boolean":
            if value not in ("true", "false"):
                raise ValueError(f"{key} must be true or false.")
            params[key] = value == "true"
            continue
        if value.startswith("@") and kind in (None, "string"):
            try:
                value = Path(value[1:]).read_text(encoding="utf-8")
            except OSError:
                raise ValueError(f"Could not read the file {value[1:]}.") from None
        try:
            params[key] = int(value) if kind == "integer" else float(value) if kind == "number" else value
        except ValueError:
            raise ValueError(f"{key} must be a number.") from None
    return params


def _shorten(value):
    """A page image is too long for a terminal, so show its size instead."""
    if isinstance(value, dict):
        return {k: f"({len(v):,} characters, not shown)" if k == "png_base64" else _shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten(v) for v in value]
    return value


def _show_problems(problems: list[str]) -> int:
    if not problems:
        print("Holds.")
        return 0
    print("Does not hold:")
    for problem in problems:
        print(f"- {problem}")
    return 1


def _rules(ctx: Context, args) -> int:
    for rule in ctx.registry.rules():
        print(f"{rule['name']}  {rule['kind']}  approval: {rule['approval']}")
        print(f"  {rule['summary']}")
        print(f"  Verified: {rule['verifies']}")
    return 0


def _do(ctx: Context, args) -> int:
    try:
        params = _parse_pairs(ctx, args.capability, args.pairs)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    outcome = dispatch(args.capability, params, ACTOR, ctx, approval_id=args.approval)
    print(json.dumps(_shorten(outcome.to_dict()), indent=2, ensure_ascii=False))
    return 0 if outcome.status in ("done", "needs_approval") else 1


def _approvals(ctx: Context, args) -> int:
    requests = ctx.approvals.all()
    if not requests:
        print("No approval requests.")
    for r in requests:
        print(f"{r['id']}  {r['status']}  {r['capability']}  asked by {r['requested_by']}  {json.dumps(r['params'], ensure_ascii=False)}")
    return 0


def _decide(ctx: Context, approval_id: str, decision: str, reason: str) -> int:
    try:
        request = ctx.approvals.decide(approval_id, decision, ACTOR, reason)
    except CannotDecide as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{request['id']} is now {request['status']}.")
    return 0


def _ledger(ctx: Context, args) -> int:
    if args.action == "verify":
        return _show_problems(ctx.ledger.verify(deep=True))
    for e in ctx.ledger.lines(limit=args.limit):
        print(f"{e.get('seq')}  {e.get('at', '')}  {e.get('kind')}  {e.get('capability') or ''}  "
              f"{e.get('actor') or e.get('decided_by') or ''}  {e.get('run_id') or e.get('approval_id') or ''}  {e.get('why') or ''}".rstrip())
    return 0


def _release(ctx: Context, args) -> int:
    return _show_problems(verify_release(ctx, args.id))


def _misses(ctx: Context, args) -> int:
    try:
        if args.action == "export" and args.id:
            print(export_case(ctx, args.id))
            return 0
        if args.action == "fixed" and args.id and args.commit:
            print(mark_fixed(ctx, args.id, args.commit))
            return 0
    except Refused as e:
        print(e, file=sys.stderr)
        return 1
    if args.action:
        print("Say which case, and for `fixed` which commit.", file=sys.stderr)
        return 2
    cases = all_cases(ctx)
    if not cases:
        print("No cases.")
    for case in cases:
        replayed = replay_case(case, ctx)
        print(f"{case['id']}  {replayed['status']}  {case['value_text']} is {replayed['verdict']}, a person said {case['should_be']}: {case['reason']}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finguard", description="A model can propose, only code can verify, and only a person can release.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("rules", help="What every capability may do, and how it is verified.")
    do = commands.add_parser("do", help="Call a capability through the door.")
    do.add_argument("capability")
    do.add_argument("pairs", nargs="*", metavar="key=value")
    do.add_argument("--approval", metavar="ID")
    commands.add_parser("approvals", help="Every approval request.")
    approve = commands.add_parser("approve", help="Approve a pending request.")
    approve.add_argument("id")
    approve.add_argument("--reason", default="")
    reject = commands.add_parser("reject", help="Reject a pending request.")
    reject.add_argument("id")
    reject.add_argument("--reason", required=True)
    ledger = commands.add_parser("ledger", help="Show the ledger, or `ledger verify` to check it.")
    ledger.add_argument("action", nargs="?", choices=["verify"])
    ledger.add_argument("--limit", type=int, default=50)
    release = commands.add_parser("release", help="`release verify <id>` checks a bundle.")
    release.add_argument("action", choices=["verify"])
    release.add_argument("id")
    misses = commands.add_parser("misses", help="Replay every case from use. `export <id>` or `fixed <id> <commit>` changes one.")
    misses.add_argument("action", nargs="?", choices=["export", "fixed"])
    misses.add_argument("id", nargs="?")
    misses.add_argument("commit", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ctx = _ctx()
    except LoadError as e:
        print(f"Finguard cannot start: {e}", file=sys.stderr)
        return 2
    if args.command == "approve":
        return _decide(ctx, args.id, "approve", args.reason)
    if args.command == "reject":
        return _decide(ctx, args.id, "reject", args.reason)
    return {"rules": _rules, "do": _do, "approvals": _approvals, "ledger": _ledger, "release": _release, "misses": _misses}[args.command](ctx, args)


if __name__ == "__main__":
    sys.exit(main())
