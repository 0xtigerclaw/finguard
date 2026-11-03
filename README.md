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

Measured on live `muse-spark-1.3`, against numbers hand-checked on the filing pages. Altered
numbers are true ones changed: ten times too big, 10% more, one digit off, another column's value,
the wrong year, period or currency, or a loss called a profit.

| Test | Altered numbers passed | Invented metrics passed | True numbers matched |
|---|---|---|---|
| **Held out: three filings first used after the code was frozen** | **0 of 273** | **0 of 3** | **23 of 31**, 8 refused, 0 called wrong |
| Repeat run: fresh replies on the filings used to build it | 0 of 575 | 0 of 5 | 57 of 62, 4 refused, 1 called wrong |

Read the held-out row. The other filings were used to fix bugs, so their result flatters the code.
Finguard is cautious: about a quarter of true numbers in the held-out test were refused for a
person to check. These are public filings we chose, so this measures the method on them and is
no guarantee on others.

Three changes came after the held-out run: a negative percentage now reads as a percentage, a memo
that never states a number's period is now refused, and labels were tidied. The held-out result
was not rerun on them. Results are in `eval/`, and `python eval/score.py --round heldout` rescores
the saved replies with no API.

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
- Where two columns share a year, as on a balance sheet, the year alone cannot tell them apart.
- A saved run replays recorded replies, and the page labels it as saved.
- Anyone who can write to the state folder could forge it whole. The chain catches edits, not that.

## Where this came from

Built during the Meta Global AI Developer Hackathon. All code in this repository is new.

- The idea and the look come from Finrail (github.com/0xtigerclaw/finrail), the owner's earlier project. No Finrail code is used.
- The record and replay design is inspired by DeepSeek Harness (github.com/deepseek-ai/deepseek-harness, MIT). No DeepSeek Harness code is used.
- The filings in `data/` are public documents. `data/sources.json` lists each source and hash. The Anthropic and NVIDIA web filings are turned into PDFs, and each keeps its own hash.
