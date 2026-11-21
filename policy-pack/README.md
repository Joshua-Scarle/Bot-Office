# Bot-Office policy pack

The repo-tracked source of Joshua's standing rules for every Bot-Office bot.

- `RULES.md`: the full text. It is installed as `data/shared/RULES.md` (the canonical copy) and as a
  managed block in `data/shared/memory.md`, which every job gets as `shared-memory.md`.
- `templates/pending-acks.md`: the layout bots use to track major reports awaiting Joshua's acknowledgement.
- `tests/test_policy_pack.py` checks the text (cost-saving mode OFF, no stale "ON" line, the 21 November
  rules) and the installer.

## What changed since the 15 November install

Cost-saving mode OFF and the $2 cap off (S51, S56, S4); Claude-first engine order with Sonnet 5.5 medium as
the Bot-Office default and Printer Writing Bot on terra medium as a per-bot note (S16, S18, S70); no unasked
Claude Duet delegation (S57, S58); re-ask unanswered questions (P9); pending-acknowledgement tracking with a
Bot-Office template (P10); bot-to-bot chatter cut, in `bot-msg` and assignment terms (S64); significant claims
need Joshua's review (S46); cost changes must cost no productivity, no markitdown (S54); declined approvals are
final and only his own words approve (P2, P5); a list of the guards Bot-Office enforces in code
(any git push, print start, outbound messages, phone calls, secrets). The 14 and 15 November rules are kept as
they were.

## Installing (not done in the slice that added this pack)

    python -m botoffice.policy_pack install --data-dir data            # dry run: prints what would change
    python -m botoffice.policy_pack install --data-dir data --apply    # writes, after a backup

Run the apply during a deploy or while few jobs run. The installer:

- backs up both files to `data/backups/policy-pack-<stamp>/` first;
- replaces `data/shared/RULES.md` with the pack's `RULES.md`;
- in `data/shared/memory.md`, replaces the previously installed copy of the rules (or an earlier managed
  block) with a marked block, leaves everything else in that file alone, and, if it cannot find the old copy,
  puts the block at the top and says so (review the file: an old ported copy may remain below it);
- is idempotent: a second run changes nothing;
- takes the shared-memory lock, and does nothing to per-bot `instructions.md` or `memory.md`.

Each bot's next turn then carries the "read shared-memory.md" notice, fresh or resumed (the rules snapshot in
`workspace.py` changes when shared memory does).

## Not in this pack (deferred, needs Bot-Office Lead)

Per-bot instructions (B1 to B20), owner `allow` rules for saved Allows A4, A17, A19, A20 and broad nohup/setsid,
the S10 wording, and the ambiguous items in `rules-plan.md` section 4. Grok-only mechanics are left out
(see the 15 November `excluded.md` kept with the earlier pack).
