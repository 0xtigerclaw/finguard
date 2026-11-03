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

In progress. Live Muse has run on one test memo about Anthropic's S-1, written for this
purpose with two planted errors: all six numbers got the right verdict
(`examples/anthropic_memo.txt`). That is one memo, not an accuracy score. Nebius and ASML
have not run on live Muse yet, and the wider score has not been measured.

Muse's boxes find the right column but can land a row out, so a box is treated as a
pointer. Code finds the exact cell from the row name printed on the page.

The tests use real filings. Most script Muse's replies, and `tests/test_live_replies.py`
replays real ones saved from live runs. No test calls the API.

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
