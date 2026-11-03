# Evaluation

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

## What happened, in order

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

## What the score tests

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
`fresh`. `--round fresh` notes 9 saved second looks from the frozen run that the fixed code no longer asks for.

## After the final test

Four later changes touched the engine files, and none changes a verdict. A refusal's wording was reworded, a scorer note replaced an exit error, saved-demo page images are pinned, so replays match across systems and open faster, and one helper was added to write worked sums in millions. Rescoring the saved replies gave the same counts in every round. The engine's fingerprint in `eval/results_*.json` is the code that did the rescoring. The live final test itself ran on commit bf56ab0.
