"""A finance plugin, written by hand. Copy this file into finguard/plugins/ and restart."""
from finguard.core.plugin import Capability, Refused, Registry


def _net_debt(ctx, params):
    filing = ctx.filings.get(params["filing"])

    def read(row):
        for cell in filing.cells_on(params["page"]):
            if cell.row.endswith(row) and params["as_of"] in cell.column:
                return cell
        raise Refused(f'No "{row}" under {params["as_of"]} on this page, so nothing was worked out.')

    current, non_current, cash = read("Debt, current"), read("Debt, non-current"), read("Cash and cash equivalents")
    return {
        "net_debt_in_millions": str(current.amount + non_current.amount - cash.amount),
        "read": [{"row": c.row, "printed": c.text, "column": c.column} for c in (current, non_current, cash)],
    }


def register(registry: Registry) -> None:
    registry.add(Capability(
        name="desk.net_debt",
        summary="Net debt on a balance sheet date, from the filing's own cells.",
        kind="read",
        approval="none",
        verifies="Code reads each printed cell from the named column and does the sum in Decimal. A missing row is refused.",
        params={"type": "object", "additionalProperties": False, "required": ["filing", "page", "as_of"], "properties": {
            "filing": {"type": "string", "maxLength": 64},
            "page": {"type": "integer"},
            "as_of": {"type": "string", "maxLength": 40},
        }},
        run=_net_debt,
    ))
