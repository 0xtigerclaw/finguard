# Finguard

![Finguard checking a memo. Two numbers are ringed in red as wrong, and the filing page beside them shows the cell code read: the memo says a net loss of $8.7bn, and the filing shows -$80.7bn.](docs/img/check.png)

**A finance harness inspired by DeepSeek Harness, running on Meta Muse.**

Finguard is an open-source harness for finance, built on the idea at the heart of DeepSeek Harness: everything is a plugin. It adds one rule that finance needs. Every plugin must state, in code, whether it reads or acts, whether a person must approve it, and how its result is verified. A plugin that acts with no approval rule stops the program at start.

Its first plugins do one job: every number in a memo gets a receipt from the filing, or a refusal. Muse proposes, code verifies, and a person releases. **Then you add your own:** describe a finance check in a sentence and Muse writes the plugin, or write one in Python. See [Add your own finance plugin](#add-your-own-finance-plugin).

**Try it:** https://finguard-157951168487.asia-south1.run.app

**Built with:** Meta Muse Spark 1.3 at runtime. Claude Opus 5.5 wrote and reviewed the code. See [How it was built](#how-it-was-built).

## Inspired by DeepSeek Harness

Finguard takes three ideas from DeepSeek Harness's public design, and carries each into finance. No code is shared.

| DeepSeek Harness established | Finguard adds, for finance |
| --- | --- |
| **Everything is a plugin.** | Every plugin also declares its trust rules: reads or acts, who must approve, how it is verified. The checks, follow-ups, house rules, releases and Creator Mode all run as plugins through one door. |
| **If the model saw it, it is a record.** | Every Muse request can be rebuilt byte for byte from a hash-chained ledger, and a released memo is sealed with its record. |
| **Record and replay.** | Real Muse replies are saved and replayed in tests, so anyone can rerun the published score with no API key. |

DeepSeek Harness is a general agent harness. Finguard is narrower on purpose: one domain, where a fluent wrong number is the most expensive kind.

## Add your own finance plugin

A fund's edge is its own checks, so adding one is the point. There are two ways, and both run under the same rules.

| | Describe it | Write it |
| --- | --- | --- |
| **Who** | An analyst, on the Create page | A developer, in Python |
| **What you give** | A sentence, such as "Net debt as of June 30, 2026" | One file in `finguard/plugins/` |
| **Who writes it** | Muse writes a definition: data, never code | You |
| **What runs it** | Finguard's own code, which reads each cell and does the sum | Your code, with Finguard's filing reader and door |
| **What must happen first** | A dry run on a real filing, then a person approves the install | It must declare its trust rules, or the program will not start |
| **What you get** | `custom.<name>`, read only | Any name, such as `desk.net_debt` |

Either way, the plugin appears on the Rules page, every run goes on the ledger, and the page and the command line can both call it.

### Describe it

Open **Create**, pick a prompt or type your own, and press **Write the plugin**. Five examples are saved, and each number below was worked out by code from the filing's cells:

| Prompt | Filing | Result |
| --- | --- | --- |
| Free cash flow | Anthropic, six months | $3.4bn |
| Net debt | Nebius, June 30, 2026 | $503.6m |
| Gross margin check | ASML, nine months | 55.05%, tying to the printed 55.0% |
| Capex intensity | Nebius, six months | 828.52% |
| Adjusted vs reported gap | Anthropic, six months | $2.3bn |

Muse never gives these values. A formula may hold only its input names, plain numbers, `+ - * /` and parentheses, and code parses it itself. If a row is missing or unclear, it says so and estimates nothing. These results are not part of the measured score.

### Write it

Copy [`examples/plugins/net_debt.py`](examples/plugins/net_debt.py) into `finguard/plugins/` and restart. Any module there with a `register` function loads with no other change. The trust rules sit in the declaration, where anyone can read them:

```python
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
```

Run it from the terminal:

```bash
.venv/bin/finguard do desk.net_debt filing=nebius page=5 "as_of=June 30, 2026"
```

That returns `503.6`, with each printed cell it read. A plugin that acts must set `approval="person"` and say what the approval binds to, or the program stops at start with a plain error. `tests/test_example_plugin.py` proves all of this on a real filing. `finguard rules` prints every plugin's rules from the live registry.

## The problem

Memos are now drafted with AI, and they read well whether or not the numbers are right. The dangerous error is not the number that looks wrong. It is the true number from the wrong line, the wrong period or the wrong definition.

**Example 1: three true answers to one question.** Did Anthropic make money? Its S-1 gives three true lines for the same six months:

| Line, six months to June 30, 2026 | Amount |
| --- | ---: |
| Net loss | -$80.7bn |
| Operating loss | -$693m |
| Adjusted operating income, as Anthropic presents it | +$1.6bn |

A memo that picks one of these has made a call for the fund, without saying so.

**Example 2: a real number from the wrong quarter.** ASML's filings report these for total net sales:

| Three months ended | Total net sales |
| --- | ---: |
| June 28, 2026 | €9.3bn |
| September 27, 2026 | €11.7bn |

A memo saying second quarter sales were €11.7bn quotes a real figure from a real filing. Only a check of the row and the column catches it. Finguard refuses it.

## The approach

Each kind of step goes to the party able to guarantee it:

| Kind | Performed by | Guarantee |
| --- | --- | --- |
| **Code** | The door and the checks | It always runs, or the action does not run. It reads the row, column, units, currency, sign, period and the words around the number. |
| **Judgement** | Muse, called as a function | None, by design. It lists claims and points to cells. Code verifies what it says. |
| **Approval** | A person | Tied to the exact content, and works once. A model cannot give one. |

Which line belongs in the memo is the fund's method, and the method is data that code applies. Under the illustrative house rule HR-1, stock compensation is a cost, so the same cells give **-$567m** rather than +$1.6bn. A request to add it back is refused before any Muse call.

## Three design principles

### 1. Muse never decides

Muse proposes where a number is. Code decides whether it is true. A verdict comes only from the PDF's own text and plain rules, so a wrong or hostile reply can cause a refusal, but it cannot write the answer.

### 2. Muse is never told the number

When Muse points to a cell, it gets the metric and the period, and not the memo's value. It cannot lean toward the memo. If its box picks out no number or several, it gets one second look at an enlarged crop, never more.

### 3. The interface is a plugin

The checks, follow-ups, house rules and Creator Mode all go through one door, and the page and the command line both call it. Neither has authority of its own. In Creator Mode, Muse writes a new plugin as data and never as code, and a person approves the install.

## How a memo flows

```mermaid
flowchart TB
    M["Memo"]:::q
    L["<b>Muse lists the claims</b><br/>each one must be word for word in the memo"]:::model
    S["<b>Code finds any number left out</b><br/>and marks it not checked"]:::code
    P["<b>Muse points to the cell</b><br/>from page images, never told the memo's number"]:::model
    C["<b>Code reads the PDF's own text</b><br/>row, column, units, currency, sign, period, wording"]:::code
    M2["<b>Match</b><br/>with the page and box as the receipt"]:::code
    W["<b>Wrong</b><br/>and the receipt says what the filing shows"]:::deny
    R["<b>Refused</b><br/>the reason is named, and a person must look"]:::deny
    H["<b>A person approves</b><br/>tied to the exact content"]:::human
    Z["<b>Sealed release</b><br/>a bundle of hashes"]:::code

    M --> L --> S --> P --> C
    C --> M2
    C --> W
    C --> R
    M2 -->|every number matches| H --> Z

    classDef q fill:#ffffff,stroke:#5f6570,color:#15171c
    classDef model fill:#f3edfb,stroke:#6b3fa0,stroke-width:2px,color:#15171c
    classDef code fill:#eef3fd,stroke:#2456c8,stroke-width:2px,color:#15171c
    classDef human fill:#eaf6f0,stroke:#15704b,stroke-width:2px,color:#15171c
    classDef deny fill:#fdeeed,stroke:#b3261e,color:#15171c
```

Purple is judgement, blue is code, green is a person, red is a verdict against the memo.

## What to try in the demo

Saved runs replay real Muse replies, and the page labels them. A new memo is a live check, limited per server.

1. **Check:** run the Anthropic test memo, which has errors planted on purpose. Two numbers get red rings. Pick one to see the cell code read and where Muse pointed.
2. **Load the corrected memo.** All 6 match, and Request release appears.
3. **Ask:** "Did Anthropic make money in the first half of 2026?" Muse writes an answer, and every number is checked like any memo. Follow its suggested next questions.
4. **House rule:** the "Our method" chip asks for Anthropic's operating result, and gives -$567m, worked out by code with no Muse call.
5. **Refusal:** the "Add back stock compensation." chip is refused with the rule's text.
6. **Create your own plugin:** pick Net debt. Muse writes a definition, and code works out $503.6m with each cell boxed. Approve it, and it becomes a plugin you can run.
7. **Rules, Record, Score:** every capability's declared rules, the hash-chained ledger, and the measured results.

## Screens

**Check.** Wrong numbers are ringed, and the proof shows the cell code read beside where Muse pointed.

![The Check page with two wrong numbers ringed in red and the filing cell boxed](docs/img/check.png)

**Corrected.** Every number matches, so the memo can go to a person.

![The corrected memo with six ticks and a Request release button](docs/img/corrected.png)

**Ask.** A conversation about a filing. Every turn is checked again from scratch.

![The Ask page with a question, Muse's answer with numbers marked, and suggested next questions](docs/img/ask.png)

**House rules.** The same cell reads differently under a method a person approved.

![The Ask page showing -$567m worked out by code under rule HR-1, with the cells it read](docs/img/house.png)

**Create.** Describe a check in plain words. Muse writes a definition, and code does the sum.

![The Create page with a gallery of finance plugins and a boxed filing cell](docs/img/create.png)

**Rules.** Rendered from the live registry.

![The Rules page with house rules and a table of capabilities](docs/img/rules.png)

**Score.** The measured results, including the bad one.

![The Score page leading with the final test on Alphabet's filing](docs/img/score.png)

## The plugins

| Plugin | What it does | The rule it enforces |
| --- | --- | --- |
| `memo.check` | Every number in a memo gets a receipt | Code gives the verdict from the PDF's text. A number left off Muse's list shows as not checked. |
| `memo.draft` | Muse answers a question from the filing, with follow-ups | Its memo is stored exactly as written and checked like any other. Nothing carries over as verified. |
| `memo.release` | Seals a checked memo as a bundle of hashes | Refused unless every number matches. A person must approve, and a changed rule needs a new check. |
| `rules.list`, `rules.apply`, `rules.change` | House rules as data | Muse never sees a rule. A change needs a person's approval and goes on the ledger. |
| `plugins.create`, `plugins.install`, `plugins.list`, `custom.*` | Creator Mode | Muse writes data, never code. Code parses the formula itself. Only a definition from a real Muse run can be installed, with approval. |
| `filings.list`, `filings.page`, `filings.add` | The filings | Adding one needs approval. PDF only, with a size limit and a recorded hash. |

## Check it yourself, without a model

The rules hold whether or not Muse behaves. These tests feed the checker the wrong quarter, the wrong currency, a hedged sentence and a moved box, with no key and no network, and each must not pass:

```bash
.venv/bin/python -m pytest tests/test_audit.py tests/test_check.py -q
```

The score reruns from saved Muse replies, with no API:

```bash
PYTHONPATH=. .venv/bin/python eval/score.py --round final
```

## Quickstart

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m uvicorn finguard.web:app --port 8790
```

Then open http://localhost:8790. Saved runs need no key. Live checks need `MODEL_API_KEY`, and approving on the web needs `FINGUARD_OPERATOR_TOKEN`. From the terminal:

```bash
.venv/bin/finguard rules
.venv/bin/finguard ledger verify
```

## Engine

Muse Spark 1.3 through the Meta Model API, the only model at runtime. The code uses its image reading, its 0-1000 box grid and its schema-locked JSON replies. Each pointing call sends up to 6 page images, and each draft up to 8.

## Evaluation

Measured on live `muse-spark-1.3`, against printed values read on the filing pages. Read the first row.

| Test | Altered numbers passed | Invented metrics passed | True numbers matched |
| --- | --- | --- | --- |
| **Final test: Alphabet's Q3 2026 release, an issuer and layout first used after the engine was frozen** | **0 of 216, and 0 of 428 harder ones** | **0 of 2** | **19 of 25**, 6 refused, 0 called wrong |
| Earlier frozen test: NVIDIA Q1 fiscal 2027, kept as measured | 0 of 144, and 0 of 20 harder ones | 0 of 2 | 1 of 20, 19 refused, 0 called wrong |
| Older held-out round. No longer a fresh test, because an audit used these filings | 0 of 273, and 0 of 558 harder ones | 0 of 3 | 25 of 31, 6 refused, 0 called wrong |
| Repeat run on the filings used to build it. This flatters the code | 0 of 575, and 0 of 1,295 harder ones | 0 of 5 | 59 of 62, 3 refused, 0 called wrong |

The bad row is why the good one exists. An audit first proved 20 ways a false number could pass, and all are now closed with tests. The NVIDIA test then showed Finguard refusing almost everything, because Muse's box landed beside the number and our PDF conversion split tables. Both were fixed before the final test.

The score measures the attacks it lists, not every possible error, and the final test is one public filing. Every step is in [docs/evaluation.md](docs/evaluation.md).

## Follow-ups and house rules

**Follow-ups.** Muse sees the last 4 turns as data, with what the check found for each number, and offers up to 3 next questions. None runs until a person picks it. A conversation stays on one filing, stops at 8 turns, and comes back after a reload.

**House rules.** The one rule shipped, HR-1, is **illustrative**: "Stock-based compensation is an operating cost and is never added back". On Anthropic's six months, 1,618 - 2,185 = -567. Code then ties it to the table: the printed operating loss of (693) plus 126 of non-cash charitable contributions, which HR-1 leaves in. A house note never changes a verdict, because $1.6bn is true to the filing. The refusal matches question patterns, so a reworded question can get past it, and its numbers are still checked.

## Honest limitations

- **It refuses true numbers:** 6 of 25 on the final test, including "Year To Date" headings it does not read as nine months.
- **HR-1 is illustrative.** A fund's real rules replace it.
- **Fiscal periods are refused** when the column is a calendar date that does not end in December.
- **Tables only, for numbers.** A number in running prose is refused, not read.
- **One filing per check.** A memo drawing on two filings cannot be checked as one.
- **Wording.** The word before a number must plainly state it, so harmless wording can be refused. It checks numbers, not the sense of a sentence.
- **Shared years.** Where two columns share a year, a memo must say which date, or it is refused.
- **The demo keeps state on one instance.** A restart clears its runs. Saved runs replay recorded replies and say so.
- **Forgery.** Anyone who can write to the state folder could forge it whole. The chain catches edits, not that.

## Docs

| | |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | How it is put together. |
| [docs/evaluation.md](docs/evaluation.md) | Every test, in order, with the classes of altered number. |
| [AGENTS.md](AGENTS.md) | The rules for anyone working in this repo. |

## How it was built

The code was written and reviewed by Claude Opus 5.5, working as several agents in parallel, with a separate audit agent trying to break the checks. Separately, at runtime, Finguard calls one model as a function: Meta Muse Spark 1.3 lists claims, points to cells, writes drafts and writes plugin definitions.

## A new project

Built during the Meta Global AI Developer Hackathon, and all code in this repository is new. The idea, the look and the house rule idea come from Finrail (github.com/0xtigerclaw/finrail), the owner's earlier project, and no Finrail code is used. DeepSeek Harness (github.com/deepseek-ai/deepseek-harness, MIT) inspired the record and replay design, and no code is shared. The filings in `data/` are public documents, and `data/sources.json` lists each source and hash.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
