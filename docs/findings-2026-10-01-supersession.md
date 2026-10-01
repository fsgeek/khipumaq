# Findings 2026-10-01: how often instances open episodes that were already revised

Question (Tony, after qhaway added supersession tracking): can khipumaq tell a
reader when earlier information was contradicted or superseded later?

## Method

From the `queries` log (A23): 37 recalls of episodes that exist, by 13+
sessions, 2026-09-25..10-01. Median age at recall 4 days; 16 older than a week.
For the week-old ones (14 distinct after merging one session's five recalls),
a subagent read each episode, searched the same project between the episode's
date and the recall date, and judged: REVISED (a specific later statement
changes or replaces a main claim, before the recall), HOLDS, or UNCLEAR.
Strict: more work on the same topic is not a revision.

## Result

| Verdict | n |
|---|---|
| REVISED | 4 |
| HOLDS | 8 |
| UNCLEAR | 2 (a key truncated in the prompt by me; a session whose recalled turns share one prefix) |

The four revisions:

| Episode | Revised by | Where | Marked? |
|---|---|---|---|
| hamutay cb1c1f0a, 08-29 | 1cdef66f, one minute later | same session | explicit: "I stand corrected" |
| explorefs 7bf17523, 08-27 | 807f9d16, minutes later | same session | explicit: "withdrawn as the frame" |
| explorefs 807f9d16, 08-27 | 8657b2da, 25 minutes later | same session | implicit |
| qhaway 019f4cb3, 07-14 | 019f8af3, 07-22 | same project, other machine | implicit |

In every case the revised episode itself carries nothing that points forward;
a reader who opens it alone sees the withdrawn claim. Most HOLDS are plans a
later turn carried out, not facts that could go stale.

Explicit correction language is rare across the store (2026-10-01, 47,284
episodes): "I was wrong" 80, "correction:" 89, "that was wrong" 17, "no
longer true" 6, "I retract" 3.

## Consequences (spec A28)

- Three of four revisions were later in the same session: `recall` shows the
  next turns of the session (`then`). No judgement, nothing stored.
- One of four crossed sessions, implicitly. That is what edges are for, with
  their provenance; not built yet.
- Edges from explicit correction phrases alone would miss most revisions.
- These 14 judgements are the seed of an evaluation set for any later
  revision detector.
