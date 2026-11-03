# Finguard architecture

A finance harness built around one rule: a model can propose, only code can verify, and
only a person can release.

Everything happens through one door. A capability is declared by a plugin, and it states
in code whether it reads or acts, whether a person must approve it, and how its result is
verified. The CLI and the web page call the same door, so they cannot differ.

## Layout

    finguard/
      figures.py page.py check.py muse.py record.py errors.py   the checking engine
      pointers.py         changing a number: a saved example's claims with the memo's numbers in them
      core/plugin.py      Capability, Registry, plugin discovery, load-time rules
      core/door.py        dispatch(): the one door
      core/approvals.py   approval requests, bound to exact content
      core/ledger.py      the workspace ledger, hash chained
      core/context.py     what a capability receives
      plugins/filings.py  filings.list, filings.page, filings.add
      plugins/memos.py    memo.check
      plugins/release.py  memo.release
      plugins/draft.py    memo.draft, and the threads that hold its follow-ups
      house.py            house rules: load them, apply them to cells, answer in code. No model
      plugins/rules.py    rules.list, rules.apply, rules.change
      plugins/creator.py  plugins.create, plugins.install, plugins.list, custom.<name>
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
- A plugin may add capabilities once the state folder is open, through `registry.at_start`. These
  rules apply to them too.

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
   `refused`, `failed`. A capability can add its own lines, such as `release_sealed`,
   `rules_changed` or `filing_added`.

## Approvals

Each request holds `id`, `capability`, `params`, `params_sha256`, `subject_sha256`,
`status` (`pending`, `approved`, `rejected`, `used`), `requested_by`, `decided_by`,
`reason`, `created_at`, `decided_at`. Requests live in the state folder as JSON.

Only an operator can decide. The terminal is an operator. On the web, the decision needs
`FINGUARD_OPERATOR_TOKEN`, compared in constant time; if that variable is unset, the web
cannot decide at all. A model can never decide. Every decision goes on the ledger.

### The demo reviewer

So a visitor to the public demo can see a release through, `FINGUARD_DEMO_REVIEWER`, when set to anything,
lets `POST /api/approvals/{id}/decide` take `{"decision":"approve","reason":"...","demo":true}` with no token.
Plain code in `core/approvals.py` allows this only when:

- the request's capability is `memo.release`, and
- its check run is a finished `memo.check` or `memo.draft` whose kept result has `saved` or `saved_pointers`
  true, and that result's hash matches the ledger.

Such a memo had no live call. Every number in it must still match, as for any release.

It may also approve `plugins.install`, only for a definition a finished `plugins.create` run replayed from a recorded
gallery prompt, with that kept result's hash matching the ledger. The plugin it installs can only read, its `verifies`
and results say the demo reviewer approved it, and a restart checks the same limit again.

Everything else still needs the real token: `rules.change`, `filings.add`, any other `plugins.install`, rejecting, and
any release of a live run. Those get 403 in plain words. A valid token wins over `demo`, and a real operator
stays `web_operator`. With the variable unset, `demo` does nothing.

`decided_by` becomes `demo_reviewer`, which is not in `OPERATORS`. The ledger's `approval_decided` and
`release_sealed` lines and the bundle's `manifest.json` (`approved_by`) all record it, so a release can never
pass as a real person's.

## Ledger

`state/ledger.jsonl`, append only and hash chained, with the same chain rule as
`finguard/record.py`. Each check keeps its own run folder under `state/runs/<run_id>/`,
holding the Muse record, and the ledger stores that run's last hash. `finguard ledger verify`
checks the ledger and every run folder.

State lives in `FINGUARD_STATE`, defaulting to `./state`, which git ignores. It holds `ledger.jsonl`,
`runs/`, `approvals`, `releases/`, `threads/`, `rules/`, `plugins/`, `filings/` and `images/`.

## Capabilities

| Name | Kind | Approval | What it does |
|---|---|---|---|
| filings.list | read | none | The filings, with fingerprints |
| filings.page | read | none | One page image |
| filings.add | act | person | Add a PDF from an https URL. PDF only, 30 MB limit, hash recorded, added after approval |
| memo.check | read | none | Every number gets a receipt |
| memo.draft | read | none | Muse writes a short memo from the filing to answer a question or a follow-up, then every number is checked like any other memo's |
| memo.release | act | person | Release a checked memo as a sealed bundle |
| rules.list | read | none | The active house rules, their hash, and whether they are the ones a person approved |
| rules.apply | read | none | Answer a method question from the filing's cells, or refuse a forbidden one. No Muse call |
| rules.change | act | person | Turn a rule on or off. The subject is the sha256 of the proposed rules |
| plugins.create | read | none | Muse writes a plugin's idea, as data, and maps it to each filing. Code does each sum. Nothing is installed |
| plugins.install | act | person | Install a definition a plugins.create run produced. The subject is its sha256, which holds no row |
| plugins.list | read | none | The gallery prompts, the installed plugins, and whether each is loaded |
| custom.<name> | read | none | Run an installed idea on any filing, with the person's number if it takes one. Plain code does the sum |

`memo.release` takes `check_run_id`. Code refuses it, before asking for approval and again
when running, unless that run's record holds and every receipt is `match`, with none
`wrong`, `refused` or `unchecked`. Its subject is the sha256 of the memo plus the receipts. The
bundle goes in `state/releases/<id>/`: `memo.txt`, `receipts.json`, `run.jsonl` and
`manifest.json` holding every file's hash, and a release id. `finguard release verify <id>`
checks it.

## Pointing and the second look

`check.locate` sends Muse up to 6 page images, a metric and a period. It sends no value and no quote.
Muse replies with a page, a box on a 0-1000 grid, and a row label copied as printed.

`Filing.numbers_near` treats the box as a pointer. It keeps numbers on a row printed with that label,
within 32 grid units up or down of the box. A number inside the box's left and right edges wins. If none
is inside, the closest within 130 grid units is taken, unless the next is about as close: the closest
must be under 0.8 of the next one's distance. `decide` then checks the column heading against the claim's period,
so a wrong column is refused.

If that picks out no number or several, `check._locate_again` makes one second look, never more. It sends
one image: the box's band across the full width, with 80 grid units above and below, at twice the DPI. The
words and schema are the same. The reply's box is put back on the page's grid. If that reply cannot be
used, or the call fails, the first box goes on to its refusal.

`decide` also reads the words before the number. The last word must be on a short list that plainly
states a level, such as "was" or "reached", or the number must start its sentence or follow a colon.
Anything else is refused for a person to read, even when it is
harmless.

## Changing a number

A memo that differs from a saved example, including the corrected memo, only in its numbers needs no live
call. `finguard/pointers.py` compares the two with `numbers_in_memo`: the same count of numbers, in the same
order, and identical text between them. Any other edit, including one word or one space, is a normal live check.

- **Claims.** Muse did not read this memo, so none is asked of it. The claims are the saved listing reply for
  the example, with the new value and a quote rebuilt from the new memo. `list_claims` then holds them to the
  usual tests against the new memo.
- **Pointing.** The pointing request holds a metric, a period and page images, and never the memo's number, so
  the example's saved replies still apply. They are read with `ReplayTransport`. No saved reply means a refusal,
  as for any failed call.
- **The check.** `check_memo` runs unchanged, with the second look and `decide`, on every number, changed or not.
  A new number that cannot be read gets the normal refusal. A number no claim covers is `unchecked`.
- **The result.** `saved` is false, `saved_pointers` is true and `base_memo` names the example, such as
  `anthropic_memo_corrected`. `saved_pointers` is false and `base_memo` null on every other check.
- **The record.** A `saved_pointers` line follows `start`. It holds `base_memo`, `base_memo_sha256`, `changes`
  as `[{from, to}]`, the listing request's and reply's hashes, and a plain note. The saved listing call is not
  written as a `muse_call`, since Muse was not called. Verdicts still come only from code.
- **Cost.** No key and no live budget.

A number with over 12 decimal places is refused for any new memo, before any call, because comparing it with a
cell runs out of decimal precision.

## Threads

A thread is one conversation about one filing. `memo.draft` starts one when it gets no `thread_id`,
and returns the new id with the answer. A follow-up passes that id back.

- **Where it lives.** `state/threads/<thread_id>.json`, one file per thread, written whole or not at
  all. An id must look like `thr_` plus 16 hex characters before it reaches the disk.
- **What each turn holds.** `turn`, `question`, `memo`, `run_id`, `counts`, `next_questions`, `saved`
  and `numbers`, each number with its value, metric and verdict. The thread file itself is not hash
  chained. Each turn's run is, in `state/runs/<run_id>/`.
- **The route.** `GET /api/threads/{thread_id}` shows the thread, leaving out `saved` and `numbers`.
  A turn goes through the door like any other call: `POST /api/do/memo.draft` with
  `{filing, question, thread_id}`.
- **What Muse sees.** The last 4 turns as JSON, marked as data. Each earlier memo is cut at 1,200
  characters and its numbers at 12. The check's verdicts go with them, and the prompt says not to
  build on a number marked wrong, refused or unchecked.
- **The check starts over.** Earlier verdicts never reach the check. Each answer is a new memo, and
  the pointing calls still carry only a metric and a period.
- **Next questions.** Muse offers up to 3, each up to 200 characters. Code drops anything else. They
  are untrusted text for a person to choose from, and none runs by itself.
- **Page search.** A follow-up may lean on the last 4 questions for its year, period and topic. The
  newest wins. No model is involved in the search.

It refuses a follow-up on another filing, an unknown id, a ninth turn, and a turn that lost a race
with another answer. A rule's refusal comes first, before any page search or Muse call.

A turn replays only when every question so far is the saved one, in the saved order. Saved turns are
in `examples/draft_<filing>.json`, with replies in `tests/recordings/draft_<filing>/`. Any other
question needs a live key.

## House rules

The same filing cell means different things under different house rules. A fund's method is
written as data in `rules/house_rules.json`, labelled ILLUSTRATIVE until a fund supplies its own.
A person changes it through `rules.change`, which keeps the result in `state/rules/` and puts the
hash and who approved it on the ledger. A rules file that disagrees with the ledger is refused.

Plain code in `finguard/house.py` applies the rules. Muse never sees a rule and never decides one.

- **A house note** sits beside a receipt on a row a rule interprets. Code reads the row to remove
  from the same column of the same table, works in Decimal, and checks the table adds up from its
  starting row. It says `differs` with the working, or `unavailable` with a plain reason. It never
  estimates, and it never changes the verdict.
- **A method question** to `rules.apply`, such as "under our method", is answered from cells with
  no memo and no Muse call. Its result `status` is `differs`, `unavailable`, `no_rows`,
  `needs_period` or `no_rule`.
- **A forbidden question** is refused with the rule's text, before any Muse call.
  `house.refuse_forbidden(ctx, question)` does this for any caller. It matches the rule's patterns on
  the question's words, so a reworded question can get past it.

`rules.change` turns a rule on or off and nothing else. It raises the rules' version, and a release
of a check made under the old rules is then refused. `rules.list` reports `holds: false` if the file
and the ledger disagree.

HR-1 is the one rule shipped, and it is illustrative. On the Anthropic S-1, for the six months ended
June 30, 2026, it reads Adjusted Operating Income (Loss) of 1,618 and Stock-based compensation expense
of 2,185 from one column of one table, on zero-based page 166. 1,618 - 2,185 = -567, so `house_money`
is `-$567m`. It then ties that to the printed Operating loss of (693): the gap of 126 is Non-cash
charitable contributions, and -693 + 126 = -567. The Nebius and ASML filings print no such row, so
the same question there gets `no_rows`.

`memo.check` and `memo.draft` results add `rules_sha256`, `rules_label` and `house_notes`, and a
matching receipt gains `house: {rule_id, text, status, house_money, working, inputs:[{row, printed,
box, page, money}], row, column, page, gap, reason}`. The run's record holds a `house` line with the
hash and the notes. House notes do not block a release, because the memo's number is true to the
filing. A release is refused if the active rules differ from the ones the check ran under, and its
manifest records `rules_sha256` and `house_notes`.

## Creator Mode

A person describes a finance plugin once, and it runs on any filing. The plugin holds the idea. Muse maps the idea
to each filing, code reads every cell and does the sum, and Muse never gives a value.

**Two kinds of Muse call.** The idea call turns the prompt into a definition: `{name, title, summary, verifies, unit,
idea, assumption:{key,label}|null}`. It names no row. A mapping call then runs per filing, with that filing's likely
pages as text and images, and returns the rows as printed, a formula over the input keys and the assumption key, and
the period: kind, year and the month the column ends in. Or it says `mappable: false` and why. Everything Muse returns
is untrusted, and page content is data. `finguard/plugins/creator.py` checks each definition and mapping strictly,
parses the formula with its own small parser, reads each row by position and works in Decimal. A row it cannot find,
or cannot pin to one number, gives `unavailable` with a plain reason, and one filing failing does not fail the others.

**`plugins.create`** takes `{prompt, live?, assumption?, filing?}`, and runs on every built-in filing unless `filing`
limits it. It returns `{definition, definition_sha256, results:[{filing, company, status, value, money, display, reason,
working, period, inputs:[{key, row, printed, money, box, page, column}], assumption_used, compare, saved, warnings,
one_table, unit}], drafted_by_muse, saved, scored:false, label, reused, assumption_ignored, model, prompt, record}`.
`assumption_used` is `{key, label, value, money, source:"person"|"example"}` or null. `compare` is null with no compare row.

**The person's number.** A plugin may take one money amount, such as an enterprise value. It is text like `$100bn`,
read with `read_figure` and bounded. Muse is never sent it, so changing it makes no call: a gallery prompt replays,
and any other prompt reuses its saved idea and mappings. With no number, a gallery prompt uses its `assumption_example`,
labelled as an example, and any other gives `Enter the <label> to run this`. A number with no currency takes the
filing's. One in another currency gives `unavailable`, since code does not convert. The working says plainly which
number is the person's, and the result carries `label: "Arithmetic on your number, not advice"`.

**Saved mappings** live in `state/plugin_mappings/<definition hash>/<filing>.json`, and a `plugin_mapped` ledger line
holds each hash. One that disagrees with the ledger gives `unavailable`. It is never read as a mapping.

**`plugins.install`** refuses a definition that no finished `plugins.create` run on the ledger produced. Its subject
is the hash of the definition, so the approval binds to the idea and to no row. On approval it writes
`state/plugins/<name>.json` and registers `custom.<name>`, which can only read. At every start, `Registry.at_start`
hooks run once the state folder is open, and each file must pass the checks again and match the approved hash on the
ledger. One that fails stays out, and `plugins.list` says why. If `filing` is named, its saved mapping must give a
result there.

**`custom.<name>`** takes `{filing, assumption?}`. It uses the saved mapping if there is one, then the gallery
recording that wrote the definition, then live Muse under the live limits.

**The gallery** in `examples/creator_prompts.json` has a `group`, Metrics or Valuation methods, and names no company
or date. Replies in `tests/recordings/creator_*` replay with no API. They are keyed by the exact request, including
the page text and images, so every page they use is pinned in `data/pinned_pages/`.

**A written workflow.** `examples/plugins/memo_pack.py` is a plain Python plugin: it checks a memo through the door,
applies a named method, and requests release, which stops at `needs_approval` until a person decides.

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
    GET  /api/start                            {filings:[{id,company,document,sha256,pages,memo,corrected,
                                                conversation}], live, max_memo, operator_required, demo_reviewer,
                                                score}
    GET  /api/rules                            {capabilities:[{name,summary,kind,approval,verifies,params}]}
    POST /api/do/{capability}                  body {params:{}, approval_id?}
                                               -> {status, run_id, result, approval, why}
    GET  /api/runs                             {runs:[{run_id,capability,status,created_at,counts,filing}]}
    GET  /api/runs/{run_id}                    {run_id, capability, status, result, record:{lines,last_hash,holds}}
    GET  /api/threads/{thread_id}              {thread_id, filing, turns:[{turn,question,memo,run_id,counts,
                                                next_questions}]}; 404 if there is no such thread
    GET  /api/approvals                        {approvals:[...]}
    POST /api/approvals/{id}/decide            body {decision:"approve"|"reject", reason, operator_token}
                                               or {decision:"approve", reason, demo:true}
                                               -> the approval; 403 with no valid token, or past what the
                                               demo reviewer may do
    GET  /api/ledger?limit=100                 {lines:[...], holds:true, problems:[]}
    GET  /api/releases/{id}                    {manifest, memo, receipts, holds}

In `/api/start`, each filing's `memo` is its saved memo, `corrected` is the same memo with its errors
fixed, and `conversation` is the list of saved questions, in order. Each is empty or null if the
filing has none. `demo_reviewer` says whether `FINGUARD_DEMO_REVIEWER` is set. `score` is the final test's
summary, with the repeat run inside it as `repeat` and the NVIDIA test as `earlier`.

The page keeps the current thread's id in `localStorage` and rebuilds the conversation after a reload,
from `/api/threads/{thread_id}` and `/api/runs/{run_id}`.

Limits on the web API, each set by an env var:

- `FINGUARD_RATE_LIMIT` POST calls, 60 by default, per `FINGUARD_RATE_WINDOW_SECONDS`, 60 by default, from
  one caller's address. Over that, a POST gets 429 with `Retry-After`, and nothing reaches the door.
- `FINGUARD_MAX_RUNS`, 1,000 by default. At the cap, `POST /api/do` gets 503.
- `FINGUARD_MAX_THREADS`, 200 by default. Despite its name, this caps stored approval requests. A POST
  that would ask for a new one at the cap gets 503.
- `FINGUARD_LIVE_LIMIT` live calls per instance, 20 by default, and `FINGUARD_LIVE_PER_IP` per caller each hour,
  5 by default. A caller is found as for the rate limit: the connection address, or the last forwarded address
  with `FINGUARD_BEHIND_PROXY`. Over either, the door refuses in plain words. Only a memo, draft or plugin that
  really goes to Muse counts. Saved examples and changing a number do not. The counts are per instance and held
  in memory. The terminal has only the instance's limit.

Caps refuse new work and never evict, since a deleted run would leave ledger lines pointing at nothing.

`memo.check` result:

    {receipts:[{verdict, why, value_text, quote, metric, filing_sha256, page, box, muse_box,
                source_text, source_money, row, column, start, end}],
     saved: bool, saved_pointers: bool, base_memo: str|null, model, filing, filing_sha256, memo,
     counts:{match,wrong,refused,unchecked}, record:{lines,last_hash,holds}}

`memo.draft` result: the same, plus `question`, `drafted_by_muse: true`, `thread_id`, `turn` and
`next_questions`.

Saved memos in `examples/`, with replies in `tests/recordings/`, replay with no API, and so does the same memo
with only its numbers changed. Live calls need `MODEL_API_KEY` and stop at the limits above.

## The score

`eval/score.py --round <name>` works from an answer key. Rounds are `final`, `fresh`, `heldout`, `2` and
`1`. For each row, it finds candidate pages, asks Muse where the metric is, makes one second look if needed,
and passes the true number and its altered versions to `check.decide()`. Muse is never told the number, so
one reply serves them all.

The results keep two sets apart. `old_false_by_class` holds the first classes, unchanged, so rounds stay
comparable. `new_false_by_class` holds the classes from the audit. They run only on numbers that matched.
They say the number with a memo's wording, or move the box to a neighbouring cell.

`memo_path` runs the four saved memos through `list_claims`, `skipped_numbers`, `period_stated` and the
check, then tries 16 memos built to slip through. The score does not touch threads, house rules or plugins.

With no `--live`, it replays `eval/recordings/round<name>/` with no API. It writes the results file, then
fails if a saved reply went unused. `--round fresh` fails that last step, because the frozen run's 9 extra
second looks are no longer asked for under the fixed code. Its results still match the file.

`engine_sha256` is the hash of `check.py`, `page.py` and `figures.py`. It is the same for every round now,
and the same as at commit bf56ab0, where the final test was frozen. See the README for the order of events.

## CI

`.github/workflows/tests.yml` runs `pytest` on each push and pull request, with `MODEL_API_KEY` empty.
