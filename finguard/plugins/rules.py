"""rules.list, rules.apply and rules.change. A fund's method as data: a person changes it, code applies it."""
from __future__ import annotations

import copy

from .. import house
from ..core.context import Context
from ..core.plugin import Capability, Refused, Registry, hash_of

MAX_QUESTION = 500


def _list(ctx: Context, params: dict) -> dict:
    rules = house.load_rules(ctx.state)
    problem = house.tamper_problem(rules, ctx.ledger.lines())
    return {"version": rules.data["version"], "label": rules.label, "note": rules.data.get("note", ""),
            "source": rules.source, "rules_sha256": rules.sha256, "holds": problem is None, "problem": problem or "",
            "rules": [{"id": r["id"], "text": r["text"], "enabled": r["enabled"], "result_label": r["result_label"],
                       "interprets": r["interprets"]["label"], "removes": r["removes"]["label"]}
                      for r in rules.data["rules"]]}


def _apply(ctx: Context, params: dict) -> dict:
    question = params["question"].strip()
    if not question:
        raise Refused("Ask a question first.")
    filing = ctx.filings.get(params["filing"])
    rules = house.active_rules(ctx)
    answer = house.answer_question(rules, filing, question)
    ctx.ledger.append("rules_applied", run_id=ctx.run_id, filing=params["filing"], rules_sha256=rules.sha256,
                      status=answer["status"], rule_ids=[a["rule_id"] for a in answer["answers"]],
                      house_money=answer["house_money"])
    return {**answer, "question": question, "filing": params["filing"], "filing_sha256": filing.sha256,
            "rules_sha256": rules.sha256, "rules_label": rules.label, "drafted_by_muse": False}


def _proposed(ctx: Context, params: dict) -> dict:
    """The whole rules as they would be after this change. An approval binds to exactly this."""
    data = copy.deepcopy(house.active_rules(ctx).data)
    rule = next((r for r in data["rules"] if r["id"] == params["rule_id"]), None)
    if rule is None:
        raise Refused("No such rule.")
    if rule["enabled"] == params["enabled"]:
        raise Refused(f"{rule['id']} is already {'on' if rule['enabled'] else 'off'}, so there is nothing to change.")
    rule["enabled"] = params["enabled"]
    data["version"] += 1
    return data


def _change_subject(ctx: Context, params: dict) -> str:
    return hash_of(_proposed(ctx, params))


def _change(ctx: Context, params: dict) -> dict:
    previous = house.active_rules(ctx)
    data = _proposed(ctx, params)
    sha = hash_of(data)
    if ctx.approval is None or ctx.approval["subject_sha256"] != sha:
        raise Refused("Changing a rule needs an approval for exactly these rules.")
    house.write_rules(ctx.state, data)
    ctx.ledger.append("rules_changed", run_id=ctx.run_id, rule_id=params["rule_id"], enabled=params["enabled"],
                      rules_sha256=sha, previous_sha256=previous.sha256, version=data["version"],
                      approval_id=ctx.approval["id"], requested_by=ctx.approval["requested_by"],
                      approved_by=ctx.approval["decided_by"])
    return {"rule_id": params["rule_id"], "enabled": params["enabled"], "rules_sha256": sha,
            "previous_sha256": previous.sha256, "version": data["version"], "approved_by": ctx.approval["decided_by"],
            "approval_id": ctx.approval["id"]}


def register(registry: Registry) -> None:
    registry.add(Capability(
        name="rules.list",
        summary="The active house rules, with their hash and whether they are the ones a person approved.",
        kind="read",
        approval="none",
        verifies="Code hashes the active rules and compares them with the last approved change on the ledger.",
        params={"type": "object", "additionalProperties": False, "properties": {}},
        run=_list,
    ))
    registry.add(Capability(
        name="rules.apply",
        summary="Answer a question under the house rules from the filing's cells, or refuse a question they forbid. "
                "Muse is not called.",
        kind="read",
        approval="none",
        verifies="Plain code reads each printed cell from one column of one table, works it out in Decimal, and checks "
                 "the table adds up. If a cell is missing it says so and estimates nothing.",
        params={"type": "object", "additionalProperties": False, "required": ["filing", "question"], "properties": {
            "filing": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$", "description": "A filing id from filings.list."},
            "question": {"type": "string", "maxLength": MAX_QUESTION, "description": f"Up to {MAX_QUESTION} characters."},
        }},
        run=_apply,
    ))
    registry.add(Capability(
        name="rules.change",
        summary="Turn a house rule on or off, once a person approves.",
        kind="act",
        approval="person",
        verifies="The approval binds to the sha256 of the proposed rules, and the change goes on the ledger with who "
                 "approved it.",
        params={"type": "object", "additionalProperties": False, "required": ["rule_id", "enabled"], "properties": {
            "rule_id": {"type": "string", "pattern": "^[A-Za-z0-9-]{1,40}$", "description": "A rule id from rules.list."},
            "enabled": {"type": "boolean", "description": "true turns the rule on, false turns it off."},
        }},
        run=_change,
        subject=_change_subject,
    ))
