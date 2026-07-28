---
target: the folders card on the dashboard
total_score: 15
max_score: 32
na_heuristics: 3,5
p0_count: 2
p1_count: 3
timestamp: 2026-07-28T05-22-51Z
slug: oxeiosync-ui-dashboard-py
---
Method: dual-agent (A: design review · B: detector + measured evidence). Disclosure: the two ran in parallel and B returned first, so detector evidence was in the parent context before A's review arrived. A itself was never shown B's output, so A's judgement is unanchored; the ordering invariant was broken at the parent, not at the assessment.

## Design Health Score

| # | Heuristic | Score | Key issue |
|---|-----------|-------|-----------|
| 1 | Visibility of system status | 2 | A scanning folder is counted "up to date" in the chips while also appearing as an attention row — two totals on one card. No "as of" on a 30s heartbeat. |
| 2 | Match system / real world | 3 | "Up to date / paused / scanning" are the user's words. Undercut by one number given two nouns ("2,310 errors" / "2,310 files") and by naming a tab rather than an action. |
| 3 | User control and freedom | n/a | Read-only surface; initiates nothing, holds no state to leave. Absence of controls is real but belongs to #7. |
| 4 | Consistency and standards | 2 | Four left edges in one card; attention rows carry no status dot while every healthy row does; chip vocabulary is a strict subset of row vocabulary. |
| 5 | Error prevention | n/a | No input, no commitment, no destructive path on this card. |
| 6 | Recognition over recall | 2 | 372px name-to-size gap; row-major fill makes an alphabetical grid read sideways; `path` never shown, so identically-labelled folders are distinguishable only from memory. |
| 7 | Flexibility and efficiency | 1 | Nothing clickable, hoverable, focusable, filterable or collapsible. Chips look exactly like filter controls and are inert labels. |
| 8 | Aesthetic and minimalist | 2 | Typographically restrained, but the same count is stated three times in 90px and the block declared unimportant takes most of the card's height. |
| 9 | Recognise, diagnose, recover | 1 | Recognise: only at 3.2:1. Diagnose: no filename, no reason, though the code already builds that string. Recover: no route anywhere. |
| 10 | Help and documentation | 2 | The only inline guidance ("No folders yet") fires while connecting and while the engine is stopped, when it is false. |
| **Total** | | **15/32** | **Poor — 47%** |

## Design Specificity Verdict

**LLM assessment.** The taxonomy is authored for a file-sync tool; the composition is not. Ranking paused *below* scanning is a real domain judgement — a pause is a human decision, a scan is the machine mid-thought — and most generic dashboards would float paused up as "degraded". The state vocabulary maps onto the engine's own folder states, and a meter appears only where there is progress to show. But title + right-aligned summary + chip row + attention list + collapsed name/metric grid is the canonical status-card pattern; nothing in the layout knows what a folder is. Replace "folder" with "job", "host" or "queue" and it survives unchanged. What is missing is what only a sync tool would want and the data nearly exists for: the folder's path (the tray menu shows it; the full-screen dashboard does not), conflicts (detected and emitted, represented nowhere), and any notion of *up to date with whom* — a folder whose only peer has been offline six days renders identically to one that synced eight seconds ago.

**Deterministic scan.** Detector run on `dashboard.py` directly: exit 0, no findings — and that scan genuinely ran (proved with a planted positive control). Run on `oxeiosync/ui/` as a directory: **vacuous** — `.py` is outside the scannable extension list, so zero files were examined. Reported as "not applicable", not as clean. The mockup HTML scanned genuinely clean. The honest reading: the 59 rules are CSS/HTML-shaped, so almost none are reachable in PySide6 where colours are `palette.qcolor(...)` calls; the clean result is close to *no evidence*, not evidence of quality.

**Visual overlays.** None. The target is a native Qt window — no URL, no DOM, no injection. No server was started and no overlay exists. Substituted with programmatic pixel measurement of a real 1500x980 screenshot.

## Overall Impression

The information design is right and the rendering betrays it. The card's thesis — answer "is anything wrong" before listing anything — is sound, and `_sort_folders` encodes it as a tested invariant. But the one rectangle that makes a folder-in-trouble read as a contained, urgent object never paints, so the visual mass runs exactly backwards: the healthy block takes most of the card and the 2,310-error row is a bare name and a whispered pill on the same flat plane. Biggest opportunity: make the attention row real, and give the healthy grid's numbers back to their names.

## What's Working

1. **`_sort_folders` is the authored core and it holds.** Pure, static, and pinned by tests including the two cases that matter — a folder both erroring and behind, and the twenty-healthy-one-broken shape that started this. Design intent surviving as an executable invariant is rare.
2. **Zero-suppression in the chips.** "0 with errors" is a string you must read before you can dismiss it. The counts are also a genuine partition, which is exactly why the scanning leak below is a bug and not a compromise.
3. **State is never carried by colour alone.** Every pill carries a word, and the reasoning for a pill over a coloured edge — a border reads as decoration and cannot hold the word that has to be there anyway — is correct and the code follows it.

## Priority Issues

**[P0] The attention row's background never renders.** `_AttentionRow._paint` sets `background:` by stylesheet on a plain `QWidget`; Qt honours that only with `WA_StyledBackground` set or a `paintEvent` drawing it. `WA_StyledBackground` appears exactly once in the whole UI package — set to `False`, in `charts.py:636`. Measured: 49,957 of 49,992 pixels in the row band are byte-identical to the card surface; the 29 outliers are the pill, which renders because `QLabel` paints its own background. *Why it matters:* every hierarchy claim the redesign makes rests on this rectangle. *Fix:* set `WA_StyledBackground` in `_AttentionRow.__init__`, or hand-paint a rounded rect as `Card` does; add a `widget.grab()` pixel assertion.
**Suggested command:** `/impeccable polish`

**[P0] The healthy grid binds every size to the wrong folder.** Measured: "Aman" ends at x=97, its "450 KiB" starts at x=469 — 372px — while the *next* folder's dot starts 13px later. A 28:1 proximity inversion caused by giving the name column the stretch and right-aligning the size. *Why it matters:* at that ratio the reading is not ambiguous, it is wrong; the list's one job is pairing a name with a size. It worsens on wider windows because the column count is a constant. *Fix:* put the stretch in a trailing spacer column so the size sits immediately right of its name.
**Suggested command:** `/impeccable layout`

**[P1] Chips and rows disagree — a scanning folder is counted twice.** The chips re-derive their counts independently of `_sort_folders`, so a scanning folder lands in the attention list *and* in "up to date". With 12 folders and one scanning, the chip reads "12 up to date" and the heading 40px below reads "Up to date · 11". There is no scanning chip at all. *Why it matters:* the commit that introduced this card argued that two totals on one screen means one of them is wrong. It now violates its own rule. *Fix:* derive the chip counts from `_sort_folders`' output; add a scanning chip.
**Suggested command:** `/impeccable clarify`

**[P1] Three of four state colours fail WCAG AA on their own grounds.** Computed: error pill 3.2:1, syncing pill 3.8:1, up-to-date chip 4.35:1 — all at ~8pt, so 4.5:1 is the right bar. Only the paused pill passes (7.6:1), and only because the code already swaps in `ink_secondary` for that one case. Had the missing row tint rendered, the two worst would get worse still (2.9:1, 3.4:1). *Why it matters:* the two states a user most needs to catch are the least legible, in the smallest type on the card. The fix already exists in the file and is applied to the one colour that did not need it. *Fix:* carry the hue in a dot or edge and set the label in `ink`/`ink_secondary`.
**Suggested command:** `/impeccable colorize`

**[P1] Every question the card raises is a dead end.** No tooltip, focus policy, cursor, or mouse handler anywhere in `dashboard.py`. The folder's path is never shown though the tray menu shows it. The error row names a tab and offers no route to it, and the first error detail — already built as `"path: error"` in the state layer — is spent on a transient toast and discarded. The chips are styled as filter controls and are inert. *Why it matters:* on an Operate surface this is the line between a status display and a dead end, and it also leaves the card unreachable by keyboard. *Fix, in order:* make the attention row focusable and clickable through to Configuration; tooltip the path on every name; carry the first error string on `FolderState` and show it; either make the chips filter or stop styling them as if they do.
**Suggested command:** `/impeccable harden`

## Persona Red Flags

**Alex (power user, 40 folders).** The chips have the exact silhouette of filter chips — tinted ground, 8px radius, hug-content sizing — and do nothing; no cursor change, no feedback. Forty folders become fourteen permanently expanded grid rows, with three columns on an ultrawide and an ~800px name-to-size gap, no collapse, no cap, no sort by what is actually behind. The alphabet runs sideways because the grid fills row-major. The tray menu knows the folder's path and the full-screen dashboard does not. Nothing says how stale a "38%" is on a 30-second heartbeat.

**Sam (accessibility-dependent).** Thirty-three unlabelled labels in a grid with no accessible names and no dot-name-size relationship, read in creation order — so a screen reader mis-binds the sizes exactly as the eye does. The status dot is a bare widget with no accessible name at all. No widget has a focus policy, so the whole card is mouse-and-eyes only. The two states Sam most needs are the two least legible. The dot is `setFixedSize(12)`, so at 200% Windows text scaling every other element grows and the only non-text indicator does not. Credit where due: the never-colour-alone rule is honoured throughout — but the dots are spent on eleven healthy rows under a heading that already says "up to date", while the attention rows that would earn a redundant channel have none.

## Minor Observations

- No eliding exists anywhere, despite a comment claiming names elide at ~20 characters. A 60-character name widens the column, pushes the size past the viewport, and the horizontal scrollbar is disabled — so it is gone, not clipped.
- The header total silently under-reports: the snapshot worker skips folder status for paused folders, so a paused folder always has zero bytes. One large paused archive makes the total badly wrong with no indication.
- A paused folder is the only row with no size shown — arguably the one whose size matters most when deciding whether to resume it.
- `show_syncing`'s "Bringing this folder up to date" fallback is unreachable: completion is forced to 100 whenever nothing is needed.
- A syncing row carries no rate and no ETA, though the sampler holds per-device rates on the same page.
- The empty state is wrong at the worst moment: with the engine stopped or still connecting, a user with twelve folders is told "No folders yet — add one in Configuration."
- Widget pools grow monotonically and are never freed.

## Questions to Consider

1. If healthy folders do not deserve ink, why do all eleven still get a dot, a name and a number — and why does that block take more of the card than everything else? The redesign went from twenty tall rows to twenty short rows, not from twenty rows to a summary.
2. Up to date *with whom*? Is "up to date" the right phrase for "I have everything I have been told about"?
3. Does a scanning folder deserve a row at all, when it resolves in seconds with no human involvement, while a pause is a decision a person made?
4. Why show the count and not the cause, when the cause is already computed one field away?
5. `_sort_folders` is tested; the rendering is not. Every failure in this review lives in the untested half. What would one `widget.grab()` pixel assertion have caught?
