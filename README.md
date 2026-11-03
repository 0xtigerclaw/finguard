# Finguard

Every number in a memo gets a receipt from the filing, or a refusal.

Finguard is a finance harness built on Meta's Muse. A model can propose, only code can verify,
and only a person can release.

Muse lists the claims in a memo and points to where a filing reports each metric. It is never
told the memo's number while pointing, so it cannot lean toward it. Plain code then reads the
PDF's own text at that spot, checks the row, column, units, currency, direction, period and the words
around the number, and gives the verdict. Muse can also write a draft, which is checked like any other
memo.

If Muse's box picks out no number or several, Muse gets one second look, at an enlarged crop of that part
of the page. It gets one retry, never more, and it is still not told the number.

| Verdict | Meaning |
|---|---|
| `match` | The filing shows this number. The page and box are the receipt |
| `wrong` | The filing shows something else, and the receipt says what |
| `refused` | The source is not clear, so nothing is assumed and a person must look |
| `unchecked` | A number in the memo that was not listed as a claim |

## The score

Measured on live `muse-spark-1.3`, against printed values read on the rendered filing pages. Results
are in `eval/`. Read the first row.

| Test | Altered numbers passed | Invented metrics passed | True numbers matched |
|---|---|---|---|
| **Final test: Alphabet's Q3 2026 earnings release, an issuer and layout first used after the engine was frozen at commit bf56ab0** | **0 of 216, and 0 of 428 in the new classes** | **0 of 2** | **19 of 25**, 6 refused, 0 called wrong |
| Frozen test: NVIDIA Q1 fiscal 2027 (commit 6a5cb87), kept as measured | 0 of 144, and 0 of 20 in the new classes | 0 of 2 | 1 of 20, 19 refused, 0 called wrong |
| Older held-out round: three filings. No longer a fresh test, because an audit used them | 0 of 273, and 0 of 558 in the new classes | 0 of 3 | 25 of 31, 6 refused, 0 called wrong |
| Repeat run on the filings used to build it. This flatters the code | 0 of 575, and 0 of 1,295 in the new classes | 0 of 5 | 59 of 62, 3 refused, 0 called wrong |

The final test is one public filing and 25 true numbers. It measures the method on that filing and is
no guarantee on others.

### What happened, in order

1. **The held-out round.** Three filings, first used after the code was frozen, gave 23 of 31 true
   numbers matched, 8 refused, and 0 of 273 altered numbers passed (commit 83c047e).
2. **The audit.** A separate audit then proved 20 ways a false number could pass. The old score did not
   test any of them, which is how it could say 0 passed while they existed. All 20 are closed with tests
   in `tests/test_audit.py`, and the score gained new classes of altered number (commit aff6b9e). The
   audit used the held-out filings, so that round is no longer a fresh test. Rescoring its saved replies
   under today's code gives 25 of 31.
3. **The NVIDIA test.** Code frozen, on a filing first used then, though its issuer's layout was already
   known. 1 of 20 true numbers matched and 19 were refused. No false number passed (commit 6a5cb87). There
   were two causes. Our own PDF conversion split a table across pages, which lost its units or headings. And
   Muse's box landed beside the number.
4. **The fixes.** Code now uses the box to find the row and reads the closest number there, unless two are
   about as close. The column heading is still checked. Web filings keep statements whole (commit
   bf56ab0). The NVIDIA PDF was left as it was, so rescoring that round still gives 1 of 20.
5. **The final test.** Engine frozen at bf56ab0, on an issuer and layout not used before. The first run
   printed 18 matched and 1 called wrong, because the scorer wrote a per-share claim as $2.12m. That was a
   bug in the scorer, not the engine. The scorer was fixed and the saved replies rescored, with no new
   calls (commit 1d8e77a). That gives the first row. The engine's hash did not change. The commit
   message says 404 for the new classes. The results file says 428, and a replay reproduces 428.

### What the score tests

The score measures these attacks, not every possible error. Each altered number is a true one changed.

Old classes, with final test counts:

| Change | Count |
|---|---|
| Ten times too big | 25 |
| 10% more | 25 |
| Last digit one higher | 25 |
| The year before | 25 |
| Another kind of period, such as a quarter for six months | 25 |
| The other currency, dollars or euros | 25 |
| Another column's value from the same row | 59 |
| A loss called a profit, or a profit called a loss | 7 |

New classes, from `new_false_by_class`, with final test counts. They start only from numbers that
matched, so 19 of the 25 here:

| Attack | Classes and counts |
|---|---|
| The memo's words say another period | `memo_says_fiscal` 18, `memo_says_other_quarter` 13, `memo_says_other_date` 6 |
| The words say it is not a plain level | `memo_says_growth` 19, `memo_says_a_bound` 19, `memo_says_a_forecast` 19 |
| Another row's name | `another_row_name` 19, `qualifier_added` 19, `opposite_word` 5 |
| Another currency, or none | `currency_pound` 19, `currency_yen` 19, `currency_canadian` 19, `currency_code` 19, `no_currency` 19 |
| Units and size | `money_as_percent` 19, `thousands_for_millions` 18, `rough_rounding` 1 |
| Sign and direction | `sign_flipped` 18, `loss_on_a_positive_row` 14 |
| The box moved to the next cell, with the true row's name | `wrong_cell_next_column` 12, `wrong_cell_previous_column` 16, `wrong_cell_next_row` 19, `wrong_cell_previous_row` 16 |
| The box moved, with that cell's own row name | `wrong_cell_next_column_own_label` 12, `wrong_cell_previous_column_own_label` 16, `wrong_cell_next_row_own_label` 19, `wrong_cell_previous_row_own_label` 16 |

Five more classes apply only to other rows and appear in other rounds: `memo_says_other_half`, `zero`,
`percent_as_money`, `qualifier_dropped` and `unsigned_on_a_negative_row`.

On the final test, code called 122 of the 216 wrong and refused 94. It called 124 of the 428 wrong and
refused 304. None passed. An invented metric is one the filing does not report, such as Adjusted EBITDA
on Alphabet's release. A separate memo path runs the four saved memos through Muse's claim list and the
period rule, then tries 16 memos built to slip through. None passed.

Wording checks now require the word before a number to plainly state it. So harmless wording can be
refused too, and a person must then read it. That is a real cost of closing these attacks.

The score does not test:

- Muse's listing of claims on new memos. The main counts start from an answer key.
- A live question about the wrong period. One saved reply serves a true number and its altered versions.
  The moved box attacks are written by the scorer, not by Muse.
- Follow-up questions, house rules or Creator Mode.
- An error of a kind not listed above.

To rescore with no API, run `python eval/score.py --round final`. Other rounds are `2`, `heldout` and
`fresh`. `--round fresh` writes its results and then exits with an error: 9 saved second looks from the
frozen run are no longer asked for under the fixed code.

## Follow-up questions

`memo.draft` can continue a conversation. The first answer returns a `thread_id`, and a follow-up
passes it back.

- Each answer is a new run, and code checks every number in it from scratch. A number that matched
  earlier does not count as verified now.
- Muse sees the last 4 turns as data, with what the check found for each number. It is told not to
  build on a number marked wrong, refused or unchecked.
- Muse offers up to 3 next questions. They are text for a person to pick from, and none runs by itself.
- A conversation stays on one filing and stops at 8 turns.

Each built-in filing has a saved conversation that replays with no key, second looks included. Anthropic
has three turns: 3 matched and 1 refused, then 2 matched and 1 refused, then 6 of 6. Nebius has two: 5 of
5, then 5 matched and 1 refused. ASML has two: 4 of 4, then an answer that says the pages do not explain
it and gives no number. A question off the saved path needs a live key.

The page keeps the conversation's id in the browser, so a conversation comes back after a reload.

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
- `plugins.install` refuses a definition that no `plugins.create` run produced. It needs a person's
  approval, tied to the definition's hash. The new capability is `custom.<name>`, and it can only read.
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
- **Rate limits and caps** protect the web API. By default, one IP gets 60 POST calls a minute. New work is
  refused at 1,000 stored runs or 200 stored approval requests. Each can be set by an env var.
- **CI** runs the tests on each push and pull request, in `.github/workflows/tests.yml`, with no API key.

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

- **It refuses true numbers.** 6 of 25 on the final test. Four were the right cell under a heading code
  would not accept. Two "Year To Date" headings were not read as nine months, and two cash flow columns
  were read as just "2026". The other two landed on the 2025 column, one of them also under "Year To Date".
- **Fiscal periods.** A memo that says "fiscal" is refused when the column is a calendar date that does
  not end in December. Code does not map fiscal years to calendar dates.
- **Tables only, for numbers.** A number in running prose is refused, not read.
- **One filing per check.** A memo that draws on two filings cannot be checked as one.
- **Wording.** The word before a number must plainly state it, so harmless wording can be refused. Beyond
  that, it checks numbers, not the sense of a sentence. "A business turning the corner" gets no receipt.
- **Signs.** A plain amount with no sign still matches a bracketed cost or loss row, because filings print
  those in brackets.
- **Shared years.** Where two columns share a year, as on a balance sheet, the year alone cannot tell them
  apart.
- **Saved runs** replay recorded replies, and label themselves saved.
- **Forgery.** Anyone who can write to the state folder could forge it whole. The chain catches edits, not
  that.

## Where this came from

Built during the Meta Global AI Developer Hackathon. All code in this repository is new.

- The idea, the look and the house rule idea come from Finrail (github.com/0xtigerclaw/finrail), the owner's earlier project. No Finrail code is used.
- The record and replay design is inspired by DeepSeek Harness (github.com/deepseek-ai/deepseek-harness, MIT). No DeepSeek Harness code is used.
- The code is under the Apache-2.0 licence, in `LICENSE`.
- The filings in `data/` are public documents. `data/sources.json` lists each source and hash. The Anthropic and NVIDIA web filings are turned into PDFs, and each keeps its own hash.
