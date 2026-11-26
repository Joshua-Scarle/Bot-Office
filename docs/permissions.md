# Bot-Office permissions (0.12.45, 0.12.47): the Grok-Bot-style approval policy

The owner, Nov 26 2026: "I only ask for approval for things like send emails or irreversible outbound
actions that are risky ... or other costly or highly risky actions ... commit should never be blocked."
Bots keep `cli_access: full` (never YOLO). Bot-Office stops a tool call for the owner's card only for the
classes below. The code is `src/botoffice/policy.py` (one place), called from `rules.Rules.evaluate`.

Judged by the **commands that would run**, never by prose: here-document bodies written to files,
`echo`/`printf` text, grep patterns, commit messages, report text and bot-to-bot messages are data.
A here-document fed to `bash`/`sh` is commands; one fed to `python`/`node` is code (its HTTP writes and
the shell commands it spells out as string literals are judged).

## Order (strictest first)

1. **Secrets** (never, every run): the controller token, the database, the bridge config, the private
   dir, other bots' `bots/<uuid>` dirs. Exfiltration stays blocked.
2. **Never rules** (`action_rules`, effect never).
3. **Git guard** (`git_guard.py`, locked, above everything): push to the default branch, force-push,
   deleting branches/tags/releases/repos, repo settings, PR merges (unless the 0.12.31 auto-merge check
   passes), deletes outside the bot's scope, switching off the push check or the credential path, and
   (new) **any push to a public GitHub repo or one whose visibility Bot-Office could not verify**
   (acme/office is public). The controller asks GitHub for visibility (cached 1 h).
4. **Send class** (locked): email, texts, DMs, chat messages to people (mail programs, connector send
   tools, known sending endpoints).
5. **The policy's ask classes** (below).
6. The owner's own ask rules, the sudo auto-review (0.12.28, its hard-stop list), allow rules, the
   approval classes in Settings (now word-level: `grep sudo` or `desk help 2>&1` no longer ask), and the
   strict profile for watcher (trigger) runs.

## Ask (a card for the owner)

| Class | What |
|-------|------|
| `post` | Issue comments and new issues on **any** repo (people read issues: decided Nov 26, consistent with the Grok Bot rule "outbound to people asks"); PR comments/reviews/edits/new PRs on public or other people's repos; `gh api` writes other than the PR workflow on the owner's private repos; GraphQL mutations; new repos, forks, labels, rulesets, secrets |
| `message` | `desk grok send` other than a plain `--kind result|fyi` report (questions and blockers go to the owner) |
| `http-write` | curl/wget/httpie POST/PUT/PATCH/DELETE/uploads to any host but this machine; inline code that posts |
| `remote` | ssh, scp, sftp, rsync to another host |
| `publish` | npm/pypi/cargo/gem publish, docker/podman push, GitHub releases, gists |
| `deploy` | deploy scripts, `make deploy`, railway up, kubectl apply, terraform apply, helm, vercel --prod, `gh workflow run`, systemctl start/stop/restart, systemd-run, crontab edits, `at` |
| `devices` | `desk type` of what may be a secret or password (a variable, `$(...)`, a file read, a secret word, a token prefix, one word mixing letters with digits or symbols, no visible text), desk input on a shared pool desktop (`--slot`, or pool mode), and 3D printer control. Input on the bot's own sandbox desktop runs (0.12.47) |
| `connector` | an MCP tool that changes something, or reads secrets (`list_variables`); reads pass |
| `credentials` | deleting, moving or overwriting a credential or key file |
| `scope` | writes outside the bot's scope: the Bot-Office live checkout (use a worktree in `~/bo-work`), another job's workspace of another bot, another bot's named dir in `~/bot-office-work` / `~/bot-office-reports`, `~/.ssh`, shell profiles, `~/.config/{systemd,gh,bot-office}`, `/etc`, `/usr` |
| `live-checkout` | git commands that change the live checkout (checkout, reset, merge, commit, stash ...), including from a job workspace (it sits inside the live repo) |
| `system` | shutdown, reboot, user accounts, disks (sudo goes to the sudo auto-review) |
| `nested-agent` | claude/codex/… with permission checks off, run outside the workspace and the work roots, or in a directory not known |

A saved specific allow rule (an anchored `^...$` shell rule for the whole command, or a tool rule) lets
`deploy`, `nested-agent`, `remote`, `http-write` and `connector` actions run: that is how the owner's
routine deploys stay automatic. It never opens `post`, `message`, `credentials`, `scope`,
`live-checkout`, `publish`, `devices`, `system`, the git guard or the send class.

## A card never ends the work (0.12.47)

The owner, Nov 26: when a command is denied, blocked or waiting on a card, a bot does not give up or end
its turn saying it needs approval. Every refusal from the gate says so, and so do rule 10, the system
prompt and RULES-FOR-ALL-BOTS (defaults for every install):

- **Re-run once approved.** Approving a card queues a resume turn (also after the asking turn ended) with a
  one-time grant for exactly that command; the resume prompt says: check it is still needed, run it, carry
  the task forward. A retry while the grant waits for its resume turn is not carded twice; a resume turn
  that skips the step leaves a chat line and an audit row ("approval unused").
- **Meanwhile, another allowed way.** The bot finishes the rest of the task and uses any other action its
  permissions allow that reaches the same goal; it never repackages the refused action.
- **Only then, say so plainly**: which command, why it needs approval, that it runs once approved.
- **Deny** wakes the bot when the task is still open (follow-up turn, the owner's optional note); the same
  command is refused there without a new card. Approvals and denies are rows in `policy_audit`
  (`approval approve`, `approval deny`, source `owner`).

## Desktop input and web actions (0.12.47)

`desk click/type/key` on the bot's own sandbox desktop run without a card (audited as before). Typing
what may be a secret or password, desk input on a shared pool desktop and printer control keep the card.
There is no desk path to the owner's own computer. Leaving the box through the browser is a web action:
the bot adds `--sends "<what>"` to a click or key that sends, posts, publishes or buys, and that asks
like any send or post (class `post`). This is the bot's own marking: Bot-Office cannot see what a click
does, so it is an instruction plus a tripwire, not enforcement (accepted residual, Codex r1 High). What
bounds it: a bot's own desktop starts with no logins (no browser-state copying), and typing a password or
token asks, so a send as a person needs an account the owner logged in or approved on that desktop; after
the owner approves such a login, clicks on that site run without a card. Anonymous web forms are reachable.
The owner can still switch on the "Controlling printers and the screen" class to card every desk input.

## Allow (no card)

Reads of any kind (`gh pr/issue list/view/status/checks`, `gh api` GET, `git fetch/pull/clone/ls-remote`,
`curl` GET), local edits and writes in the job workspace, the bot's older workspaces and the work roots
(`~/bo-work`, `~/bot-office-work`, `~/bot-office-reports`, `/tmp`); git commit, merge, rebase,
cherry-pick in worktrees; feature-branch pushes to private repos; PR create/edit/ready/review/comment and
labels on the owner's own private repos; PR merges that pass the auto-merge check; nested `claude -p` /
`codex exec` runs in the work roots; build-artefact cleanup (`__pycache__`, `.pytest_cache`,
`node_modules`, `dist` ...); test runs with the office variables unset (`env -u BOT_OFFICE_* ... python -m
pytest`); bot-to-bot messages (`bot-msg`, `desk message`, `desk grok send --kind result|fyi`); connector reads.

## Bot-Office safeguards (the owner trusts Bot-Office less than Grok Bot)

- **Audit**: every allowed action that the old standard rules (E8 per-bot set, outbound preset, regex
  classes) would have carded is written to the append-only table `policy_audit` (SQLite triggers refuse
  UPDATE and DELETE): bot, job, source, command summary, the policy's reason, and why it used to ask.
  `GET /v1/policy/audit?limit=&bot_id=&since=` (master token). `GET /v1/policy` describes the policy.
- **Scope**: a bot writes only in its workspace, its own earlier workspaces and the work roots; other
  bots' dirs, the live checkout and system files stay guarded (`scope`, `live-checkout`, the git guard).
- Secrets stay `never`; the git guard stays above every rule and class.

## Cards

- A card waits **24 h** (attended: Settings → approval timeout, default 1440 min; unattended:
  `BOT_OFFICE_APPROVAL_TTL_MIN`, default 1440). Grants after approval: 24 h, one use, as before.
- The same action asked again by the same bot (any turn of the same task: the same assignment, or chat
  turns) **reuses** the waiting card; one that timed out unanswered in the last 72 h is raised again
  instead of a duplicate. 0.12.47: a card never moves to another assignment.
- **Daily digest**: Needs-you shows one "Cards still waiting" line for cards older than 6 h (clear it to
  hide it until the next day).

## Rules data

`scripts/migrate_rules_0_12_45.py` (through the API, idempotent, JSON backup in `~/backups/`) removes
the per-bot "E8 safety gate" and "Nov 26 interim" ask rules: the policy replaces them. MPG DevOps (BO)
is skipped on purpose (its rules are left as they were). General Worker is the template new bots copy
rules from, so new bots start with the policy alone.

A tripwire, not a sandbox: a bot with full CLI access, running as the same Unix user, can get around it.
