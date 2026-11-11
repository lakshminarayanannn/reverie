---
name: reconcile
version: 2
---
You compare one new memory note with existing notes that may overlap it.

{{preamble}}

For the most closely related existing note, choose a relation:
- "duplicate": same claim (possibly worded differently).
- "refines": same subject; the new note adds precision without conflicting. Give merged_text: one or two short sentences that keep the actionable rule and every exact value, and drop incidental details (test counts, example invocations, timings). It must not be much longer than the longer of the two notes.
- "contradicts": the two cannot both be true.
- "updates": the old note was true earlier but the situation changed (the new note supersedes it).
- "unrelated": none of the existing notes is about the same thing.

Each note shows the date of its newest evidence. Do not decide which note is right from wording alone; say which note's evidence looks stronger in "stronger": "new", "old", or "unclear".

Reply with only a JSON object:
{"relation": "duplicate | refines | contradicts | updates | unrelated", "target_id": "n-... or null", "merged_text": "string or null", "stronger": "new | old | unclear", "reason": "one short sentence"}
