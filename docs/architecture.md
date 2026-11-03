# Finguard architecture

A finance harness built around one rule: a model can propose, only code can verify, and
only a person can release.

Everything happens through one door. A capability is declared by a plugin, and it states
in code whether it reads or acts, whether a person must approve it, and how its result is
verified. The CLI and the web page call the same door, so they cannot differ.

## Layout

    finguard/
      figures.py page.py check.py muse.py record.py errors.py   the checking engine
      core/plugin.py      Capability, Registry, plugin discovery, load-time rules
      core/door.py        dispatch(): the one door
      core/approvals.py   approval requests, bound to exact content
      core/ledger.py      the workspace ledger, hash chained
      core/context.py     what a capability receives
      plugins/filings.py  filings.list, filings.page, filings.add
      plugins/memos.py    memo.check
      plugins/release.py  memo.release
      plugins/draft.py    memo.draft
      cli.py              the `finguard` command
      web.py              the API and the page
      static/             the page

## Capability

    @dataclass(frozen=True)
    class Capability:
        name: str               # "memo.check"
        summary: str            # one plain sentence
        kind: str               # "read" or "act"
        approval: str           # "none" or "person"
        verifies: str           # one plain sentence on how the result is checked by code
        params: dict            # JSON Schema for params
        run: Callable[[Context, dict], dict]
        subject: Callable[[Context, dict], str] | None = None
                                # the exact content an approval binds to, as a sha256 hex string

A plugin is a module in `finguard/plugins/` with `register(registry)`.

Load-time rules, which stop the program at start if broken:
- An `act` capability must have approval "person" and a `subject`.
- Every capability must state `verifies`.
- Names are unique and look like `area.verb`.

## The door

    dispatch(name, params, actor, ctx, approval_id=None) -> Outcome

`Outcome` has `status`: `done`, `needs_approval`, `refused` or `failed`, plus `run_id`,
`result`, `approval` and `why`.

1. An unknown capability or params that fail the schema: `refused`.
2. Approval "person": with no `approval_id`, create a pending request and return
   `needs_approval` without running. With one, it must be approved, for this capability,
   for the same params hash and the same subject hash now, and unused. Otherwise `refused`.
   Using it marks it used, so it cannot run twice.
3. Run. An exception becomes `failed` with a plain reason, never a raw traceback in `why`.
4. Every step goes on the ledger: `requested`, `approval_created`, `approval_used`, `done`,
   `refused`, `failed`.

## Approvals

Each request holds `id`, `capability`, `params`, `params_sha256`, `subject_sha256`,
`status` (`pending`, `approved`, `rejected`, `used`), `requested_by`, `decided_by`,
`reason`, `created_at`, `decided_at`. Requests live in the state folder as JSON.

Only an operator can decide. The terminal is an operator. On the web, the decision needs
`FINGUARD_OPERATOR_TOKEN`, compared in constant time; if that variable is unset, the web
cannot decide at all. A model can never decide. Every decision goes on the ledger.

## Ledger

`state/ledger.jsonl`, append only and hash chained, with the same chain rule as
`finguard/record.py`. Each check keeps its own run folder under `state/runs/<run_id>/`,
holding the Muse record, and the ledger stores that run's last hash. `finguard ledger verify`
checks the ledger and every run folder.

State lives in `FINGUARD_STATE`, defaulting to `./state`, which git ignores.

## Capabilities

| Name | Kind | Approval | What it does |
|---|---|---|---|
| filings.list | read | none | The filings, with fingerprints |
| filings.page | read | none | One page image |
| filings.add | act | person | Add a PDF from an https URL. PDF only, 30 MB limit, hash recorded, added after approval |
| memo.check | read | none | Every number gets a receipt |
| memo.draft | read | none | Muse writes a short memo from the filing, then memo.check checks it like any other |
| memo.release | act | person | Release a checked memo as a sealed bundle |

`memo.release` takes `check_run_id`. Code refuses it, before asking for approval and again
when running, unless that run's record holds and every receipt is `match`, with none
`wrong`, `refused` or `unchecked`. Its subject is the sha256 of the memo plus the receipts. The
bundle goes in `state/releases/<id>/`: `memo.txt`, `receipts.json`, `run.jsonl` and
`manifest.json` holding every file's hash, and a release id. `finguard release verify <id>`
checks it.

## CLI

    finguard rules
    finguard do <capability> key=value ... [--approval ID]
    finguard approvals
    finguard approve <id> [--reason TEXT]
    finguard reject <id> --reason TEXT
    finguard ledger [--limit N]
    finguard ledger verify
    finguard release verify <id>

## API

JSON unless stated. Errors use FastAPI's `{"detail": "plain words"}`.

    GET  /                                     the page
    GET  /page/{filing}/{page}.png             a page image
    GET  /api/start                            {filings:[{id,company,document,sha256,pages,memo}],
                                                live, max_memo, operator_required, score}
    GET  /api/rules                            {capabilities:[{name,summary,kind,approval,verifies,params}]}
    POST /api/do/{capability}                  body {params:{}, approval_id?}
                                               -> {status, run_id, result, approval, why}
    GET  /api/runs                             {runs:[{run_id,capability,status,created_at,counts,filing}]}
    GET  /api/runs/{run_id}                    {run_id, capability, status, result, record:{lines,last_hash,holds}}
    GET  /api/approvals                        {approvals:[...]}
    POST /api/approvals/{id}/decide            body {decision:"approve"|"reject", reason, operator_token}
                                               -> the approval; 403 with no valid token
    GET  /api/ledger?limit=100                 {lines:[...], holds:true, problems:[]}
    GET  /api/releases/{id}                    {manifest, memo, receipts, holds}

`memo.check` result:

    {receipts:[{verdict, why, value_text, quote, metric, filing_sha256, page, box, muse_box,
                source_text, source_money, row, column, start, end}],
     saved: bool, model, filing, filing_sha256, memo, counts:{match,wrong,refused,unchecked},
     record:{lines,last_hash,holds}}

`memo.draft` result: the same, plus `question` and `drafted_by_muse: true`.

Saved memos in `examples/`, with replies in `tests/recordings/`, replay with no API. Live
calls need `MODEL_API_KEY` and stop at `FINGUARD_LIVE_LIMIT` per instance.
