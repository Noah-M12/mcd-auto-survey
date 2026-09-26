# mcdvoice survey map

Recorded 2026-09-09 by walking survey `19386-13560-...` end to end (validation
code `3406213`). Everything below was observed on the live site, not guessed.

## The headline finding: positions are NOT stable

The original plan assumed "every button is always in the same location." That is
false, and building on it would produce wrong answers silently.

- **Answer options are shuffled per page load.** "How did you place your order?"
  rendered as `[Mobile app, kiosk, employee]` on one load and
  `[Mobile app, employee, kiosk]` on the next.
- **Matrix rows are shuffled too.** The six-row satisfaction grid came back in a
  different row order on the second run.
- What *is* stable: the `name` of a radio group, the `id` (`name` + `.` + value),
  and the value→meaning mapping within a question.

So: **match on label text, never on position or index.**

## Element structure

| thing | selector | notes |
|---|---|---|
| code entry boxes | `input[type=text]` (6 visible, in order) | 5-5-5-5-5-1 digits |
| start | `button` "Start" | |
| radio input | `input[type=radio]` | `id` = `R000455.3`, `name` = `R000455` |
| the clickable target | `label[for="<id>"]` | **the input itself is not clickable** |
| next | `input#NextButton.NextButton` | an `<input type=submit>`, not a `<button>` |
| checkboxes | `input[type=checkbox]` | one `name` per box, value always `1` |
| comment box | `textarea` | 1200 char limit, optional |
| demographics | `select` | has a "Prefer not to answer" option |

### Three traps

1. **Dotted IDs.** `#R001000.5` is invalid CSS — the dot reads as a class.
   Use `input[id="R001000.5"]`.
2. **Hidden inputs.** The radio is visually replaced by a styled label. Clicking
   the input does nothing at all — it silently fails to select. Click the label.
3. **Filler characters.** Matrix option labels contain only a zero-width joiner
   (`&zwj;`), and column headers separate words with `&nbsp;`. Strip the
   zero-width chars but convert nbsp to a *space* — deleting it yields
   `HighlySatisfied`, which matches nothing.

Option text for a matrix cell isn't on the label at all; it comes from the
column header — walk up to the `<td>`, take its index, then find the first row
whose cell at that index has text and no `<input>`.

## Question flow

Order and progress % from the observed run. Progress jumps unevenly.

| % | question | type | how it was answered |
|---|---|---|---|
| 1 | How did you place your order? | radio | **asked Noah** → With an employee |
| 1 | Visit type | radio | **asked Noah** → Carry out |
| 1 | Overall satisfaction | matrix 1×5 | Highly Satisfied |
| 2 | MyMcDonald's Rewards member? | Yes/No | **asked Noah** → No |
| 4 | 6 attributes (taste, ease, temp, quality, friendliness, speed) | matrix 6×5 | Highly Satisfied |
| 5 | 3 attributes (cleanliness, accuracy, value) | matrix 3×5 | Highly Satisfied |
| 5 | Was your order accurate? | Yes/No | Yes — follows from the accuracy rating |
| 10 | Which categories did you order? | checkboxes | from the receipt items |
| 21 | Which Burger/Chicken/Fish items? | checkboxes | from the receipt items |
| 29 | Which Beverage items? | checkboxes | from the receipt items |
| 67 | Quality of each item ordered | matrix N×5 | Highly Satisfied |
| 91 | Did you experience a problem? | Yes/No | No |
| 94 | Return / recommend in 30 days | matrix 2×5 | **asked Noah** → Highly Likely |
| 96 | What did you like best? | textarea | **left blank** — optional, and not mine to write |
| 98 | Visits in past 30 days | radio | Four or more (12 receipts on 09/08 alone) |
| 99 | Favorite fast food restaurant | radio | **asked Noah** → McDonald's |
| 99 | "McDonald's is a brand I trust" | agree matrix | **asked Noah** → Strongly Agree |
| 100 | Household income | select | **Prefer not to answer** |
| — | Finish | | `Validation Code: 3406213` |

### Scales seen

- `Highly Satisfied / Satisfied / Neither Satisfied nor Dissatisfied / Dissatisfied / Highly Dissatisfied` (value 5→1)
- `Highly Likely / Likely / Somewhat Likely / Not Very Likely / Not At All Likely`
- `Strongly Agree / Agree / Neither Agree nor Disagree / Disagree / Strongly Disagree`
- `Yes / No` — **note the polarity flips**: Yes is value 1 here, whereas the
  satisfaction top box is value 5. Another reason to match text, not value.

## Which questions a rating can't answer

A blanket "highly satisfied" covers the matrix pages and nothing else. These
need the person every time:

- how the order was placed, and visit type
- rewards membership
- which items were bought (derivable from the receipt, if you have it)
- favorite restaurant, brand-trust agreement
- the free-text comment

Yes/No on *accuracy* and *problem* follow from the satisfaction answers; visit
count follows from the receipts. Everything else is a genuine ask.

## Session timeout — the thing that ate the first attempt

The first walkthrough reached **94%** and then bounced to the Welcome page,
losing everything. Cause: long pauses while questions were put to Noah.

The code was **not** consumed — re-entering it started a fresh survey at 1%,
which then completed. But the work is lost, so:

- Gather the per-visit answers *before* entering the code.
- Don't leave a survey idle waiting on a person.
- If it does bounce, just re-enter the code; nothing is burned.

## Validation codes collected

| code | validation |
|---|---|
| 19386-13540-90826-00472-00153-7 | 2230213 |
| 19386-13560-90826-00527-00266-1 | 3406213 |

## Monthly limit

Five surveys per month per restaurant, enforced server-side against the
household. `19386-13290-...` hit it and returned `Block.aspx` before the first
question. All twelve codes are store 19386, so the cap is shared across them.
