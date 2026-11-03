# Finguard

Every number in a memo gets a receipt from the filing, or a refusal.

Meta's Muse does two jobs. It lists the claims in a memo, and it points to where a filing
reports each metric. It is never told the memo's number while pointing. Plain code then
reads the PDF's own text inside that box, checks the row, column, units, currency and
direction, and gives the verdict.

| Verdict | Meaning |
|---|---|
| `match` | The filing shows this number, with the page and box as the receipt |
| `wrong` | The filing shows something else, and the receipt says what |
| `refused` | The source is not clear, so nothing is assumed |
| `unchecked` | A number in the memo that was not listed as a claim |

## Status

In progress. The check runs on real Anthropic, Nebius and ASML filings, and the tests use a
scripted stand-in for Muse. It has not yet run against live Muse, and no accuracy score has
been measured.

## Run the tests

```bash
pip install -e ".[dev]"
pytest
```

## Where this came from

Built during the Meta Global AI Developer Hackathon. All code in this repository is new.

- The idea comes from Finrail (github.com/0xtigerclaw/finrail), an earlier project. No Finrail code is used.
- The record and replay design is inspired by DeepSeek Harness (github.com/deepseek-ai/deepseek-harness, MIT). No DeepSeek Harness code is used.
- The filings in `data/` are public documents. `data/sources.json` lists each source and hash.
