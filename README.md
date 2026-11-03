# Finguard

Every number in a memo gets a receipt from the filing, or a refusal.

Finguard is a finance harness built on Meta's Muse. A model can propose, only code can verify,
and only a person can release.

Muse lists the claims in a memo and points to where a filing reports each metric. It is never
told the memo's number while pointing, so it cannot lean toward it. Plain code then reads the
PDF's own text at that spot, checks the row, column, units, currency, direction and period, and
gives the verdict. Muse can also write a draft, which is checked like any other memo.

| Verdict | Meaning |
|---|---|
| `match` | The filing shows this number. The page and box are the receipt |
| `wrong` | The filing shows something else, and the receipt says what |
| `refused` | The source is not clear, so nothing is assumed and a person must look |
| `unchecked` | A number in the memo that was not listed as a claim |

## The score

Measured on live `muse-spark-1.3`, against numbers hand-checked on the filing pages.

| Test | Altered numbers passed | Invented metrics passed | True numbers matched |
|---|---|---|---|
| **Held out: three filings first used after the code was frozen** | **0 of 273** | **0 of 3** | **23 of 31**, 8 refused, 0 called wrong |
| Repeat run: fresh replies on the filings used to build it | 0 of 575 | 0 of 5 | 58 of 62, 4 refused, 0 called wrong |

Read the held-out row. The other filings were used to fix bugs, so their result flatters the code.
Finguard is cautious: about a quarter of true numbers in the held-out test were refused for a
person to check. These are public filings we chose, so this measures the method on them and is
no guarantee on others.

### What the score tests

The score tests particular kinds of altered number. It is a measure of those, not of every
possible error. An altered number is a true one changed in one of these ways:

| Kind of change | Held-out count |
|---|---|
| Ten times too big | 31 |
| 10% more | 31 |
| Last digit one higher | 31 |
| The year before | 31 |
| Another kind of period, such as a quarter for six months | 31 |
| The other currency, dollars for euros or euros for dollars | 31 |
| Another column's value from the same row | 75 |
| A loss called a profit, or a profit called a loss | 12 |

Of the 273, code called 161 wrong and refused 112. None passed. An invented metric is one the
filing does not report, such as Adjusted EBITDA on an ASML filing. The score does not test:

- Muse's listing of claims. The scorer starts from an answer key, so only the pointing step and
  the verdict are measured.
- A number taken from another row, or a wrong word in the prose around a number.
- A live question about the wrong period. Muse's reply for the true period is reused for each
  altered version, so the year and period tests ask whether code spots the mismatch.
- The period rule, follow-up questions, house rules or Creator Mode.

### Changes after the held-out run

These came after the held-out run:

- A negative percentage now reads as a percentage.
- The sign check: a memo's sign must agree with the filing's on a percentage or a figure the memo
  writes as negative.
- The column heading fix: a heading is read from the year above the number, even where two
  columns' headings almost touch.
- The period rule: a memo that never states a number's period is refused. The scorer does not
  reach this rule, so it cannot change the score.
- Labels were tidied, and an altered number that rounds back to the true one no longer counts as
  false.

Rescoring the same saved replies with the corrected code gave the numbers now in the files. No
call to Muse was made. On the held-out filings no verdict changed, so these fixes did not lift that
result. On the repeat run one true number moved from called wrong to matched, a negative percentage.
Results are in `eval/`, and `python eval/score.py --round heldout` rescores the saved replies with
no API. A fresh claim about accuracy would need a filing the code has never seen.

## Follow-up questions

`memo.draft` can continue a conversation. The first answer returns a `thread_id`, and a follow-up
passes it back.

- Each answer is a new run, and code checks every number in it from scratch. A number that matched
  earlier does not count as verified now.
- Muse sees the last 4 turns as data, with what the check found for each number. It is told not to
  build on a number marked wrong, refused or unchecked.
- Muse offers up to 3 next questions. They are text for a person to pick from, and none runs by itself.
- A conversation stays on one filing and stops at 8 turns.

Each built-in filing has a saved conversation that replays with no key. The Anthropic one has three
turns, with 3 matched and 1 refused, then 2 matched and 1 refused, then 6 of 6 matched. A question
off the saved path needs a live key.

## House rules

The same filing cell can mean different things under different house rules. A fund's method is data
in `rules/house_rules.json`, and plain code applies it. Muse never sees a rule and never decides one.

- `rules.list` shows the active rules, their hash, and whether they are the ones a person approved.
- `rules.apply` answers a method question from the filing's cells, with no Muse call. It also
  refuses a question a rule forbids.
- `rules.change` turns a rule on or off. A person must approve the exact result, and the hash goes on
  the ledger. A rules file that disagrees with the ledger is refused.

The one rule shipped is **HR-1, and it is illustrative**: "Stock-based compensation is an operating
cost and is never added back". It is an example written to show how house rules work. It is not any
fund's real method, and a fund replaces it with its own.

A worked example, from `rules.apply` on the Anthropic S-1 with the question "Under our method, what
was the operating result for the six months ended June 30, 2026?":

| Row, on PDF page 167 | Printed, in $ millions |
|---|---|
| Adjusted Operating Income (Loss) | 1,618 |
| Stock-based compensation expense | 2,185 |
| **HR-1 result: 1,618 - 2,185** | **-567, or -$567m** |

Code then ties that to the table. The printed operating loss is (693). The gap of 126 is non-cash
charitable contributions, which the filing also adds back and HR-1 leaves in. -693 + 126 = -567.

The same result shows as a house note beside the `$1.6bn` receipt in a memo. A note never changes a
verdict, because $1.6bn is true to the filing.

Ask for the add-back and it is refused before any Muse call. "What is operating income for the first
half of 2026 with stock-based compensation added back?" gets: "This question goes against that rule,
so it was refused. Nothing was sent to Muse."

The refusal matches question patterns, so a reworded question can get past it. A question that gets
past it to `memo.draft` still has every number checked. Of the three built-in filings, only the
Anthropic S-1 prints a row HR-1 applies to.

## Creator Mode

A person describes a finance check in plain words. Muse writes a definition, not code. A person
approves it before it becomes a capability. Creator Mode is not part of the measured score: nothing
above tests it.

A definition is data: row names, a formula, a unit and one period. Plain code in
`finguard/plugins/creator.py` then does the rest.

- `plugins.create` asks Muse for a definition, checks every field, and does a dry run on the filing.
  Nothing is installed.
- The formula may hold only its input names, plain numbers, `+ - * /` and parentheses. Code parses it
  itself and never runs it as code.
- Code reads each row from the filing by position and does the sum in Decimal. If a row is missing or
  unclear, it says so and estimates nothing.
- `plugins.install` needs a person's approval, tied to the definition's hash. The new capability is
  `custom.<name>`, and it can only read.
- `plugins.list` shows what is installed. A definition file that no longer matches its approved hash
  is not loaded.

Results say `scored: false`, so a reader can tell them from the measured checks.

A saved example, on the Anthropic S-1. The request asks for free cash flow for the six months ended
June 30, 2026. Muse names two rows and writes the formula `operating_cash + capex`. Code reads 3,929
and (482) from one column, and works out 3,447, or $3.4bn. Muse never gives that value. Gallery prompts
with saved replies replay with no key. Any other request needs a live key.

## How it is built

Everything goes through one door. A plugin declares each capability in code: whether it reads or
acts, whether a person must approve, and how its result is verified. The page and the command
line call the same door. `finguard rules` prints the table, and the program will not start if an
acting capability has no approval rule.

- **Approvals** are tied to the exact content a person saw, and work once. A model cannot give one.
- **The ledger** is append only and hash chained. Each Muse request can be rebuilt byte for byte.
- **A release** is refused in code unless every number matches, then sealed as a bundle of hashes.

See `docs/architecture.md`.

## Run it

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m uvicorn finguard.web:app --port 8790
.venv/bin/finguard rules
```

Saved memos and drafts replay real Muse replies with no key. Live checks need `MODEL_API_KEY`.
Approving on the web needs `FINGUARD_OPERATOR_TOKEN`. Tests never call the API.

## Known limits

- Tables only. A number in running prose is refused, not read.
- It checks numbers, not the words around them. "A business turning the corner" gets no receipt.
- A held-out refusal rate of 8 in 31. In 6 of the 8, Muse's box did not land on exactly one number
  in the row it named. In the other 2, code could not read the column heading.
- Where two columns share a year, as on a balance sheet, the year alone cannot tell them apart.
- A plain amount with no sign matches a negative cell unless the memo calls it a profit, because
  filings print costs in brackets.
- A saved run replays recorded replies, and the page labels it as saved.
- Anyone who can write to the state folder could forge it whole. The chain catches edits, not that.

## Where this came from

Built during the Meta Global AI Developer Hackathon. All code in this repository is new.

- The idea, the look and the house rule idea come from Finrail (github.com/0xtigerclaw/finrail), the owner's earlier project. No Finrail code is used.
- The record and replay design is inspired by DeepSeek Harness (github.com/deepseek-ai/deepseek-harness, MIT). No DeepSeek Harness code is used.
- The filings in `data/` are public documents. `data/sources.json` lists each source and hash. The Anthropic and NVIDIA web filings are turned into PDFs, and each keeps its own hash.
