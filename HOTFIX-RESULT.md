# Hotfix 0.12.18: guard escapes B8 and B11 (branch hotfix/guard-escapes)

Commits on top of 0.12.17 (`7d86c6a` = origin/master), not pushed and not deployed:

- `56ded97` watchdog: outage duration counts from the last good heartbeat, not detection
- `f732d3c` guard: nested agent CLIs with permissions bypassed ask; scheduled runs never pre-authorised
- `ec1c173` 0.12.18: version bump, README release note, docs/APPROVALS.md
- `960662f` fix round 2 (Codex review, FIX-FIRST): see "Fix round 2" below

## Files changed

| File | Change |
| --- | --- |
| `src/botoffice/nested_agents.py` (new) | `bypass_launch(command)`: finds an agent CLI started with a bypass flag. Stdlib only; reuses `git_guard.split_words`, `_heredocs`, `inline_code`. |
| `src/botoffice/rules.py` | Adds the `nested-agent` preset to `PRESETS`, puts it first in `presets_hit` (so the card names it), and makes `outbound`/`destructive` preset rules also match it. In `evaluate`, runs nobody watches also get the other scope's `never`/`ask` rules (B11). Docstring updated. |
| `src/botoffice/store.py` | `PRESET_LABELS["nested-agent"] = "Nested agent with permissions bypassed"` |
| `src/botoffice/api.py` | `GET /v1/rules` describes the `nested-agent` preset. |
| `src/botoffice/workspace.py` | `SCHEDULED_RULE`: a line added to `_reply_rule` for wake-up and routine runs. Resume turns, which have `origin_job_id`, don't get it. |
| `ops/watchdog/src/engine.js` | `outageStart()`. `last_good_hb` is stored on every healthy heartbeat. Incidents and alerts store `started`. Both the recovery path ("lasted about") and the already-recovered path use it, falling back to `inc.since`/`e.at`. |
| `ops/watchdog/test/engine.test.js` | 3 tests: recovery duration, already-recovered duration, and the fallback. The first two fail on the old code. |
| `tests/test_guard_escapes.py` (new) | 110 tests (below) |
| `pyproject.toml`, `src/botoffice/__init__.py`, `tests/test_rename_compat.py`, `tests/test_crewdeck.py` | 0.12.17 → 0.12.18 |
| `README.md`, `docs/APPROVALS.md` | 0.12.18 release note. New sections "Scheduled runs start with no approval" and "Nested agent CLIs", plus a "Which rules apply" paragraph. |

## Design choices

- **B8 is a rule preset, not part of the git guard.** An owner `allow` rule must be able to open it, and the git guard can't be opened. A bypass launch counts as both `outbound` and `destructive` for rule matching, so with existing `ask` rules for those presets the card appears and the launch is refused until approved (then it runs once, in the resume turn). `allow` runs it and `never` refuses it. A dedicated `preset nested-agent` rule also works. With no rule, attended and full-profile runs allow it (unchanged), and strict-profile runs (triggers, gated bots) ask through the existing "hits and unattended" default.
- **Detection is structural, not a regex over the text.** The detector parses the command and only flags an agent name in program position: directly, by path, after wrappers (env, nohup, setsid, timeout, nice, ionice, stdbuf, time, xargs, sudo, doas, exec, command, npx, bunx, pnpx, `pnpm|yarn|bun dlx`, uvx, `pipx run`), and inside `sh -c`/`bash -c`/`eval`, `env -S`, `npx -c`, `$( )`/backticks, a heredoc fed to a shell, and inline interpreter code (string literals and literal lists). Anything else is data, so `grep`, `echo`, `man`, `rg`, a heredoc written to a file, or a commit message never trigger it. If parsing fails or nests too deep, a simpler word-level check runs instead.
- **Flags:** only `bypassPermissions` counts for claude; `acceptEdits` and `plan` do not. codex `-c` matches `approval_policy=never` and `sandbox_mode=danger-full-access`, with or without quotes. I also counted cursor-agent `--force`/`-f` and aider `--yes-always`/`--yes`.
- **Inheritance: no new PATH shims.** I checked that children already inherit PATH with `bin/` first, the `BOT_OFFICE_*` variables and the `GIT_CONFIG_*` pairs (`core.hooksPath`) from `CliAdapter._env`; a test pins this. Changing those variables, `env -i` and `env -u` already ask (git guard `_hook_env`), and a test pins that too. The gate hook itself is passed per invocation (claude `--settings`, codex `-c hooks...`), so a nested claude or codex doesn't inherit it. Making it inherit would mean modifying or wrapping the CLI, which is out of bounds, so this is documented and the card on the bypass launch is the check.
- **B11: no carry-over existed, and that is now pinned by tests.** `consume_grant`, `_push_grant_waiting` and `_push_allowed` need `origin_job_id` and `resume_job_id == job`. `_approved`, `_passes` and the tickets are keyed by job id. A woken job has no origin. The real gap was rule scope. Runs in `UNATTENDED_SOURCES` (routine, wakeup, assignment, inbox, trigger) now also take the *other* scope's `never` and `ask` rules. The other scope's `allow` rules are deliberately not added: a gated bot's attended `allow` would loosen its strict profile, and an earlier version that took the max of two full evaluations broke the gated-bot "unattended allow opens the preset" behaviour. So the strictest result wins and the run is never looser than before.
- **Note on resume turns:** a resume turn of a wake-up job inherits `source="wakeup"`, so it is judged by the same rules, and its legitimate grant is still consumed in `decide` after `evaluate` says ask.
- **Watchdog:** "heartbeat missing" counts from the last heartbeat (`s.hb.received_at`, or `first_seen` if none ever arrived). "Controller down" and "unhealthy" count from the last healthy heartbeat (`last_good_hb`). Backup incidents, and cases where no time is known, count from detection.

## Tests

- New `tests/test_guard_escapes.py`: **110 passed**.
  - 29 direct bypass forms and 32 wrapper forms, each detected as `nested-agent` first.
  - 26 false-positive and no-flag forms, none flagged.
  - ask/allow/never behaviour, with ask via the outbound, destructive or nested-agent preset showing the card label; ask then approve runs once.
  - No-flag launches and no-rule launches are untouched; strict trigger runs ask.
  - Child env inheritance, and stripping the guard env asks.
  - B11 end to end (4 cases): job A approves an action, A's resume turn schedules a wake, it fires job B, and the same action in B raises a fresh card with no grant, ticket or pass. Run for both the send and the rule-only `git push`, with the grant used and unused.
  - Unattended-scope rules apply to full bots' wakeup, routine, assignment and inbox runs; strictest scope wins; the system-line text.
  - 72 of the 110 fail on the 0.12.17 `rules.py`.
- Full suite (`PYTHONPATH=src nice /workspace/venvs/bo-git-guard/bin/pytest -n 4 -q`): **2388 passed, 6 skipped, 3 failed.**
  - `test_usage_budgets::test_digest_is_built_from_the_database_once_a_day`: known.
  - `test_healthz::test_a_stuck_health_thread_cannot_block_interpreter_shutdown`: environmental. It runs `python -I`, which ignores PYTHONPATH, and this venv has no botoffice install (`ModuleNotFoundError: botoffice`).
  - One xdist flake per run, different each time (`test_question_cards::test_blocking_answer_after_job_finished_becomes_user_message`, then `test_file_uploads::test_unsent_uploads_are_pruned_even_for_a_bot_with_no_images`). Both pass when run alone.
  - `test_usage_notice::test_switch_provider_endpoint` passed.
- Watchdog (`node --test test/*.test.js` in `ops/watchdog`): **51 passed**, 3 of them new.

## What is NOT covered

- **Bypass launches the detector can't see:**
  - from a script file the bot wrote and then runs (`bash run.sh`, `python job.py`);
  - through a renamed, copied or aliased CLI binary, or a shell function;
  - with flags built at run time (`f=--yolo; codex $f`);
  - through a bypass set in the child CLI's own config (`~/.claude/settings.json` `permissions.defaultMode`, `~/.codex/config.toml` `approval_policy`) or in environment variables;
  - from a tool other than the shell (a connector, or a `Task`-style sub-agent tool).
  Combined short-flag clusters (e.g. gemini `-yp`) are not parsed.
- **A nested claude or codex started without a bypass flag** doesn't run the parent's gate hook for its own tool calls. It relies on its own headless permission defaults, the shims on PATH (active only where `BOT_OFFICE_GUARD_SHIM=1`, i.e. engines without a hook) and git's pre-push check.
- **Defence in depth, not a sandbox.** Bots keep full CLI access as the same Unix user. The airtight design (a per-run sandbox or Unix user) is separate work.
- **B11 wording:** the system line is guidance only. Enforcement is the gate at fire time, which judges a wake's prompt like any other prompt (by source, never by text). Bot-to-bot inbox and assignment runs get the stricter rule scope but not the system line.
- **Not done:** no live drill on the server, no deploy, no `scripts/probe_gate.sh` run. The watchdog change is not deployed to the Worker.


## Fix round 2 (commit `960662f`, answers the Codex review of attempt 2)

Changed: `src/botoffice/nested_agents.py`, `src/botoffice/rules.py` (one line), `ops/watchdog/src/engine.js`, `docs/APPROVALS.md`, `tests/test_guard_escapes.py`, `ops/watchdog/test/engine.test.js`. No CLI executable, provider invocation or token handling touched.

1. **P1 scripts and wrappers.** `bypass_launch(command, cwd)` now reads a script a command runs (`bash|sh|zsh|dash|busybox sh <f>`, `. f`, `source f`, `./f` or any path to a file, `python3 f.py`, `node f.js`) when it exists, is text (no NUL in the first 8 KiB) and is at most 256 KiB, and runs the same detection on it (shell scripts as commands, python/node scripts as code; chosen by interpreter or shebang). Followed two script levels deep, with a seen-set against loops. A script written and run in one command (here-document or `echo ... > f`, then `bash f`) is read from the command. Wrappers added: busybox sh/ash, chrt, taskset, flock (incl. `-c`), watch, parallel, `script -c`/`-qc` (command, doas, exec, sudo, stdbuf, ionice were already there). An unrecognised wrapper followed later in argv by an agent name and a bypass flag is a hit (also a quoted command string). Words of known data programs (grep, echo, git, man, cat, ...) are skipped. Alias bodies and function bodies defined in the command that launch an agent with a bypass flag are a hit.
2. **P1 long commands.** Over 32 KiB the command is not parsed; any agent CLI name plus any bypass-flag token anywhere (comment padding included) is a nested-agent hit (`_coarse`), and `presets_hit` now calls the detector for every length. Note: a command over `APPROVAL_LIMIT` (4000) cannot be shown on a card, so `decide` already refuses it ("too long to review", effect never). So for these the outcome is a refusal, not a card; the tests pin deny with the nested-agent preset.
3. **P1 env stripping.** An agent CLI under `env -i`, `env -`, `--ignore-environment`, `env -u|--unset PATH|BOT_OFFICE_*|GIT_CONFIG_*`, `env PATH=|BOT_OFFICE_X=|GIT_CONFIG_X=`, prefix assignments of those, `exec -c`, `env -i bash -c '...'` (carried into the nested shell), or inline python/node/perl code that clears or rewrites its environment (`os.environ.clear()`, `env={}`, `%ENV = ()`, exec*e...) and starts an agent CLI is a nested-agent hit even with no bypass flag. Beyond text matching, and documented as such in docs/APPROVALS.md and here: a compiled program, or a script written in one command and run in a later one that strips the environment itself before exec'ing an agent CLI (the script is read when the run command is checked, but a script that builds its launch at run time is not). Defence in depth; the airtight design (per-run sandbox or Unix user) is the real fix.
4. **P2 interpreter string false positives.** Left as fail-safe (a card). Documented in docs/APPROVALS.md and pinned by a test.
5. **P2 watchdog.** `heartbeat()` stamps `inc.ended` on an open incident at the first heartbeat that clears it (any heartbeat for heartbeat_missing, a healthy one for controller_down/unhealthy; a later failing heartbeat clears the stamp). The recovery mail ("lasted about") and the already-recovered text use `outageEnd(inc, now)`, falling back to the tick time. New test: last good heartbeat minute 0, detected 20, healthy at 45, tick at 49: "lasted about 45 min". Plus the already-recovered variant (4 to 9, tick at 12: 5 min) and a healthy-blip-then-down case.
6. **Ticket-isolation test** (`test_github_ticket_and_push_pass_stay_with_their_job`) now issues a real ticket in job A with `rules.tickets.issue`, shows job B (the woken job) cannot redeem it (with or without a repo), and A redeems it once and not twice.
7. A regex in the first draft of this round was quadratic on 30 KB inputs and failed `test_gate_hardening` bounded-time; fixed (anchored, length-capped, match-capped) and a bounded-time test added for the new patterns.

### Counts (round 2)

- `tests/test_guard_escapes.py`: **199 passed** (110 before).
- Full suite (`PYTHONPATH=src nice /workspace/venvs/bo-git-guard/bin/pytest -n 4 -q`): **2477 passed, 6 skipped, 3 failed**. The 3: `test_usage_budgets::test_digest_is_built_from_the_database_once_a_day` (known), `test_healthz::test_a_stuck_health_thread_cannot_block_interpreter_shutdown` (known, venv has no botoffice install under `python -I`), and `test_file_uploads::test_unsent_uploads_are_pruned_even_for_a_bot_with_no_images` (the random parallel flake; passes alone, 34 passed for the file). An earlier full run in this round also showed `test_gate_hardening` [8],[9] (the regex above) and `test_public_hygiene` (a `/home/u/...` path in a test string, changed to `/usr/local/bin/claude`); both fixed and pass.
- Watchdog (`node --test test/*.test.js` in `ops/watchdog`): **54 passed** (51 + 3 new).

### Still not covered

Everything in "What NOT covered" above that was not listed in this round, and in addition: aliases or functions defined in an earlier command or an rc file, scripts changed between check and run, scripts over 256 KiB or nested deeper than two, and the fail-safe false positives (interpreter string literals, an agent name and a flag in the argv of an unknown program, e.g. `npm i aider --yes`). Not pushed, not deployed.


## Fix round 3 (final; answers the Codex review of attempt 3)

Principle: simple fail-closed coarse checks over more parsing; a false card is accepted, a miss is not. Changed: `src/botoffice/nested_agents.py`, `ops/watchdog/src/engine.js`, `docs/APPROVALS.md`, `tests/test_guard_escapes.py`, `ops/watchdog/test/engine.test.js`. No CLI, provider invocation or token handling touched.

1. **P1 long commands.** `_coarse` now tokenises the text and, for every agent CLI name found as a word or path basename (so `/usr/bin/claude` counts), runs the same `bypass_flag` the normal parser uses (new `strict=False`: no stop at `--`) over all words. Short and fused forms are therefore found (`-anever`, `-a never`, `agent -f`, `--force`, `-y`, `--yolo`). The separate regex flag list is gone, so the lists cannot drift. Tests: 15 forms padded past 32 KiB both sides, plus a flag-list test. Note: over `APPROVAL_LIMIT` the outcome is still a refusal, not a card.
2. **P1 scripts.** (a) `cd` / `pushd` are followed inside the command (`_chdir`); an unresolvable directory leaves the cwd unknown, so nothing is read from disk and (b) applies. (b) `_writes_and_runs`: a command that writes (`>`, `>>`, `<<`, `tee`, `cp`/`mv`/`install`/`dd`, `open(...,'w')`, `write_text`, `writeFile`, `.write(`) and runs a shell, interpreter, `source`/`.` or a path, and whose text has an agent name and a bypass flag anywhere, is a hit. Tests cover both reviewer examples and the negatives (no write, no run, no flag).
3. **P2 watchdog.** `controller_down` is stamped ended by the first heartbeat that reports the controller reachable (healthy or not); `unhealthy` still needs a healthy one. Any incident still present at a tick loses its `ended` stamp, so a heartbeat_missing that misses again is not closed early. Two new node tests (reachable-but-unhealthy at 10, tick at 15: "about 10 min"; heartbeat at 25 then missing again at 60, recovery at 70: "about 70 min").
4. **docs/APPROVALS.md** lists what is and is not caught, including the blunt write-and-run check and its false cards.

### Counts (round 3)
- Guard tests (`test_guard_escapes.py`, `test_gate_hardening.py`): 277 passed.
- Full suite: 2505 passed, 6 skipped, 2 failed, both known: `test_usage_budgets::test_digest_is_built_from_the_database_once_a_day`, `test_healthz::test_a_stuck_health_thread_cannot_block_interpreter_shutdown` (`python -I` ignores PYTHONPATH). No xdist flake this run.
- Watchdog node tests: 56 passed (2 new).
- Bounded time: a 1 MB coarse scan took 0.6 s; other adversarial 40-720 KB inputs under 0.1 s.
- Still not caught (see docs): compiled programs, run-time-built launches, renamed binaries, earlier-command aliases, child CLI config, non-shell tools, and a script written in one command and run in a later one that no longer shows the name and the flag.
- Not pushed, not deployed.
