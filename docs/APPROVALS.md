# Approvals

Bots rarely ask. They work on their own, and the owner picks the kinds of action that should wait for a yes. These are the **approval classes** in Settings → Approvals.

**A checkpoint, not enforcement.** A class is a tripwire on what a bot is about to do: it stops the tool calls it recognises on engines that can pause, so the owner gets a say before the usual risky steps. It is not a sandbox and does not make anything impossible:

- matching is on a tool's name and command line, so a command built at run time (`s=sudo; $s ...`) or hidden in a script the bot wrote is not caught;
- on engines that can't pause (opencode, Codex with restricted access, and an engine whose hook is not proven yet) a class is only advice in the bot's SELF.md; Settings → Approvals names those bots next to every class that is on;
- the gate fails closed: an event it cannot read, a crash, running out of time or a controller it cannot reach refuses the tool call, in every run kind.

Rely on isolation, access settings and the owner's `never` action rules for what must not happen; use classes to be asked first.

## How a step gets approved

1. A bot tries an action, for example sending an email through a connector.
2. The engine's gate hook (`bin/bot-office-gate`) asks Bot-Office before the tool runs. If the action is in a class that is switched on, Bot-Office refuses that one tool call, records an approval and tells the bot to end its turn with `BOT-OFFICE-STATUS: waiting_on_user — approval #N`.
3. The bot's chat shows an Approve / Deny card with the class and the whole action. Needs-you lists it too. Automatic continuation does not run while it waits.
4. **Approve** gives a one-shot grant for exactly that action (the command or connector tool with its arguments, in the same working directory; a description or timeout field does not count) and queues a resume turn; only that turn can use the grant, once, within 24 hours. The resume prompt is kept out of the chat; the card reads "Approved".
5. **Deny**, or no answer within the timeout (60 minutes by default, 0 = wait until answered), leaves the task stopped with a one-line note. An expired card can still be approved later ("Approve and run").

Order of decisions: protected secrets are always refused; then the git guard and the Sending class, which no rule or setting opens (below); then the owner's action rules (`never` > `ask` > `allow`); then the approval classes; then, for unattended runs under the strict profile (watcher runs, and scheduled runs of bots set to "gated"), every outbound or destructive action asks as before. An owner `allow` rule for an action therefore beats a class.

Which rules apply (0.12.18): a chat or API run uses the `attended` and `all` rules. A run nobody is watching (routine, wake-up, assignment, bot-to-bot inbox, watcher trigger) uses its own profile's rules (`unattended` under the strict profile, else `attended`) plus `all`, **and also the other scope's `never` and `ask` rules**: the strictest wins, and only its own scope's `allow` rules open anything. So an `unattended` rule set to ask applies to a default bot's wake-ups and routines too.

## Scheduled runs start with no approval (0.12.18)

A wake-up, routine or other scheduled run is judged by the bot's rules when it fires, never by what was approved when it was scheduled:

- an approval's grant is bound to the resume turn of the job that asked (only that turn, once, within 24 hours); a push pass (10 minutes) and a GitHub ticket (2 minutes) are bound to their job. A woken job is a new job with no origin job, so none of these reach it, whatever its note or prompt says ("send the approved update");
- an outbound or destructive action in it goes through the rules as above: with `ask` it raises a fresh card at fire time;
- its rules say so: "This run was started by a scheduled wake. Earlier approvals do not carry over; any send or destructive action needs a fresh approval." (a routine run gets the same line naming a routine; the resume turn after an approval, which does hold that one grant, does not);
- creating a wake-up is never blocked.

## Nested agent CLIs (0.12.18)

Bots keep full CLI access, so a bot can start another agent CLI from its shell. Started with its permission checks off, that child runs any tool without asking, out of sight of the bot's own gate hook. The gate therefore treats such a launch as an action of its own, the rule preset **`nested-agent`** ("Nested agent with permissions bypassed" on the card):

| CLI | Flags that count |
| --- | --- |
| `claude` (also `npx @anthropic-ai/claude-code`) | `--permission-mode bypassPermissions` (not `acceptEdits` or `plan`), `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions` |
| `codex` (also `npx @openai/codex`) | `--dangerously-bypass-approvals-and-sandbox`, `--yolo`, `--full-auto`, `-s`/`--sandbox danger-full-access`, `-c`/`--config approval_policy=never` or `sandbox_mode=danger-full-access`, `-a`/`--ask-for-approval never` |
| `gemini`, `opencode` | `--yolo`, `-y`, `--dangerously-skip-permissions`, gemini `--approval-mode yolo` |
| `cursor-agent`, `agent` | `--force`, `-f`, `--yolo` |
| `aider` | `--yes-always`, `--yes` |

The launch is found by path (`/usr/local/bin/claude`), behind wrappers (`env`, `nohup`, `setsid`, `timeout`, `nice`, `ionice`, `chrt`, `taskset`, `stdbuf`, `xargs`, `sudo`, `doas`, `exec`, `command`, `flock`, `watch`, `parallel`, `script -c`, `busybox sh`, `npx`, `bunx`, `pnpm dlx`, `uvx`, `pipx run`), in `bash -c` / `sh -c` / `eval`, `$( )` and backticks, a here-document fed to a shell, and string literals of inline code (`python -c "subprocess.run(['claude', '--dangerously-skip-permissions'])"`). A mention is not a launch: `grep bypassPermissions file`, `echo claude --yolo`, `man claude`, a here-document written to a file, a commit message.

More than the command text is read (0.12.18, round 2):

- **Script files.** When a command runs a script (`bash f`, `sh f`, `zsh f`, `dash f`, `busybox sh f`, `. f`, `source f`, `./f` or another path to a file, `python3 f.py`, `node f.js`), the file is read at check time if it exists and is a text file of at most 256 KiB, and the same detection runs on its contents. A script that runs a script is followed two levels deep; binaries, larger files and missing files are not followed. A script written and run in the same command (a here-document or `echo ... > f`, then `bash f`) is read from the command.
- **Unknown wrappers.** A program the detector does not know that is followed, anywhere in its arguments, by an agent CLI name and one of its bypass flags is a hit too (words of `grep`, `echo`, `git`, `man`... are data and are skipped).
- **Aliases and functions** defined in the command whose body launches an agent CLI with a bypass flag are a hit.
- **Long commands fail closed.** A command over 32 KiB is not parsed: if it contains any agent CLI name and any bypass flag anywhere (padding and comments included), it is a nested-agent hit. A command that long is also over the approval-card limit, so it is refused with the usual "too long to review" message rather than shown on a card; split it, or write a script file and run that.
- **A stripped environment.** An agent CLI started under `env -i`, `env -`, `env -u PATH` / `BOT_OFFICE_*` / `GIT_CONFIG_*`, `env PATH=... / BOT_OFFICE_X= / GIT_CONFIG_X=`, `exec -c`, or from inline interpreter code that clears or rewrites its environment and starts one (`os.environ.clear()` then `os.execvp('claude', ...)`, `env={}`, `%ENV = ()`) is a hit even without a bypass flag, because the child then lacks the guard variables, the pre-push hook and the shim PATH. This is text matching: a compiled program, or a script that is written and then run in a later command and strips the environment itself before it execs an agent CLI, is beyond it.
- **Fail-safe false positives.** Inline interpreter code is read for string literals that look like a launch without proving they run: `python3 -c 'print("claude --dangerously-skip-permissions")'` raises a card. That is deliberate (a card is cheap; a missed launch is not).

The owner's rules decide, and the bot's `outbound` and `destructive` preset rules also govern it (a bypassed agent can do both): `ask` raises the card and the launch is refused until approved (then it runs once, in the resume turn), `allow` runs it, `never` refuses it. A `preset nested-agent` rule targets only these launches. With no rule it runs in attended and full-profile runs and asks in strict-profile runs, like the other presets; it is not part of the always-on git guard, so an owner `allow` rule can open it.

A nested CLI started **without** a bypass flag is not stopped. It keeps its own permission prompts (headless, it cannot run a gated tool without them) and inherits the run's environment: PATH with Bot-Office's `bin/` first (the git, gh, curl and mail shims), the job's `BOT_OFFICE_*` settings and the `GIT_CONFIG_*` pairs that set `core.hooksPath` (git's pre-push check still sees its pushes). Changing those variables, `env -i` or `env -u` already asks (the git guard). What a child does **not** inherit is its parent's gate hook: Claude Code's hook is a per-invocation `--settings` flag and Codex's a per-invocation `-c hooks...`, and Bot-Office does not patch, wrap or proxy the CLIs, so a nested `claude` or `codex` does not call `bin/bot-office-gate` for its own tool calls (a nested `cursor-agent` started in the workspace may pick up its `.cursor/hooks.json`; this is not relied on). No PATH shim is added for agent CLIs: the card on the bypass launch is the check.

Defence in depth, not a sandbox: a compiled program, a script that builds its launch at run time or is changed between the check and the run, a script over 256 KiB or nested more than two deep, a renamed or copied CLI binary, an alias defined in an earlier command or a shell rc file, flags assembled at run time, or a bypass set in the child CLI's own config file (`~/.claude/settings.json` `defaultMode`, `~/.codex/config.toml`) are not seen. The airtight design (each run in its own sandbox or Unix user) is separate work. Code: `src/botoffice/nested_agents.py`, used by `rules.presets_hit`.

## Classes

| Class | Default | Matches |
| --- | --- | --- |
| Sending email or messages | always on (locked) | `sendmail`, `mutt`, `mail`, `msmtp`, `swaks`...; SMTP from inline code; a request to a known sending endpoint (chat and webhook hosts, mail, SMS and social APIs; below); connector tools whose name sends (send / post / reply / message / email / publish / tweet / comment / create_issue...). Messages to other bots (bot-msg, desk grok send) never ask |
| Purchases and payments | on | connector tools named pay / purchase / checkout / buy / charge / refund / create_payment / create_order...; `stripe ... create` |
| Merging, releasing, force-pushing | on | `gh pr merge`, `gh release create`, `npm publish`, `docker push`, `git push` to main or master or with `--force` |
| Admin and system changes | on | `sudo`, `systemctl`, `crontab`, `shutdown`, `reboot`, user accounts, `podman system prune` |
| Deleting outside the bot's workspace | on | `rm -r` of paths outside the workspace, writes to `~/.ssh` and other protected places, `dd of=`; connector tools named delete / trash / destroy / purge |
| Pushing code and posting on GitHub | off | any `git push`, `gh pr create/comment`, `gh issue ...`; GitHub connector writes |
| Logging in to other computers | off | `ssh`, `scp`, `sftp`, `rsync host:` |
| Controlling printers and the screen | off | PrusaLink and OctoPrint commands, `desk click/type/key` |
| Any connector action | off | every connector (MCP) tool call, including reads |

Sending email or messages cannot be switched off (the box is greyed and the API refuses it with 422) and no `allow` rule opens it: like the git guard, it asks in every run kind.

What counts as sending:

- a request to a known sending endpoint, made with `curl`, `wget`, `http`/`https` (httpie), `xh`, or from inline code (`python -c`, `node -e`... with the URL in the code). The list (`SEND_ENDPOINTS` in `git_guard.py`): `hooks.slack.com`, `slack.com/api/chat.*` (and file uploads, invites), Discord webhooks and channel messages, `api.telegram.org` send methods, Microsoft Graph and Outlook `sendMail` / send / reply / forward and Teams chat or channel messages, `*.webhook.office.com`, Gmail `messages/send` and `drafts/send`, Google Chat space messages, `api.sendgrid.com`, `api.mailgun.net`, `api.postmarkapp.com`, `api.resend.com`, `api.twilio.com`, Brevo, Mailjet, SparkPost, Mandrill, SMTP2GO, Amazon SES (`email.*.amazonaws.com`), Pushover, ntfy.sh, `api.x.com` / `api.twitter.com` tweets and DMs, `graph.facebook.com`, `api.linkedin.com` posts and messages, Bluesky posts. A GET, HEAD or OPTIONS does not count, except for the Slack and Telegram methods, which also send on GET;
- a POST, PUT, PATCH or DELETE to a URL in a variable named like one of these (`$SLACK_WEBHOOK_URL`, `$DISCORD_WEBHOOK`...);
- mail programs (`sendmail`, `mail`, `mailx`, `msmtp`, `swaks`, `mutt`), and SMTP from inline code (`smtplib.SMTP(...)`, `.sendmail(`, `nodemailer.createTransport(`...; importing `smtplib` alone does not count);
- connector tools, by name: a send verb (send, post, reply, publish, tweet, comment, message, email...), or create/add with a message-like object (issue, comment, message, post, review...); a tool whose name starts with get / list / read / search does not.

Every other request does not ask, whatever its method: a read-only GraphQL POST, an embeddings or LLM API call, a POST to your own service. `BOT_OFFICE_SEND_HOSTS` (comma or space separated, in the controller's environment) adds endpoints: `host` (any path) or `host/path-prefix`; `*.example.com` also matches subdomains. A non-GET request to one of them sends.

Your own classes match a command pattern or a tool-name pattern (regular expressions). A pattern sees at most the first 4096 characters; a longer command counts as in your class, so it asks. A pattern that is too slow on long, repetitive input (nested repeats such as `(a+)+`) is refused when you save it. Matching is a tripwire on the tool's name and its command line, not a sandbox: a bot that hides a command inside a script it wrote first is not caught. The bot's own instructions tell it not to work around a refused step.

## The git guard (always asks)

Some actions are not a class and have no switch. In every run kind (chat, routine, wake-up and auto-continue, assignment, bot-to-bot inbox, watcher trigger; strict profile or not), for every bot whatever its CLI access, they stop on an Approve / Deny card first when the guard sees them. It is defence in depth, not an inescapable boundary for bots with full CLI access running as the same Unix user (see [What is and isn't covered](#what-is-and-isnt-covered)):

- `git push` to the default branch (always `main`, `master`, `trunk`, and the remote's real default: GitHub's answer, or the remote's own from `git ls-remote --symref`, see the pre-push check below), including `HEAD:main`, `+x:main`, `:main`, `--all`, `--mirror`, and a plain `git push` made while on that branch (or with `push.default=matching`, an upstream on it, or a remote refspec); a push whose branch cannot be known asks too. While the remote's default is not known (no token, `ls-remote` failed), `production` and the branch the clone's own `refs/remotes/<remote>/HEAD` names are guarded too, and other branches pass: a feature-branch push never asks just because the default is unknown
- any force push (`-f`, `--force`, `--force-with-lease`, `+refspec`), `git push --delete` / `:ref`, `git push --no-verify`, `git branch -d/-D`, `git tag -d`, deleting the default branch
- changing the run's git hooks or credentials: `git -c core.hooksPath=...` / `credential.*` / `include.*`, `git config` of those keys, `GIT_CONFIG_*`, `GIT_DIR`, `GH_TOKEN` and similar in the environment, `env -i` / `env -u`, `gh auth token|login|setup-git`, `git credential`
- `gh pr merge`, `gh repo delete|edit|rename|archive|transfer|sync`, `gh release delete`, `gh secret|variable set|delete`, `gh api` with DELETE, PATCH or PUT (except plain issue edits), any POST/PUT/PATCH/DELETE under `repos/`, `orgs/` and similar that is not on a short list of safe writes (issues, comments, labels, opening a PR, reviews, statuses, check runs), GraphQL mutations not on the safe list (comments, issues, reviews, labels...), and GraphQL whose body cannot be read (`--input -`, a variable, a missing file); request bodies in files (`--input file`, `-F query=@file`, `curl -d @file`, `--data-binary @file`, `--post-file`) are read and judged; the same through `curl`/`wget`/`http`
- connector (MCP) tools that merge, delete a branch, tag, repo or release, change repo settings, or commit or push to the target repo's default branch (read from GitHub, below; while that is not known, `main`, `master`, `trunk` and `production`)
- deleting or moving files outside the bot's workspace (`rm`, `mv`, `find -delete`, `rsync --delete`, `shred`, trash, patch deletes); paths are resolved through symlinks, and a path with an unknown variable (`$DIR/x`) or a hidden-file glob (`.*`) counts as outside

It is read from the command itself (env prefixes, `git -C`, `-c`, `sh -c`, `eval`, `$( )`, quoting, chains, aliases, and inline code: `python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`, an awk program, and a here-document fed to a shell or an interpreter). A script file the command runs (`python test_x.py`, `bash build.sh`, `source env.sh`) and the modules code imports are not read: a test file or a library that mentions a push or SMTP is not a push or a send. What a script really does still reaches git's pre-push check, and the shims where they are on. Every other command a bot runs, git included, is untouched. Only a `never` rule is stricter; no `allow` rule, switched-off class, timeout, bot setting or access level opens it. Code: `src/botoffice/git_guard.py`, applied first in `Rules.evaluate`.

The command check is a tripwire, not a sandbox: a command assembled at run time from a file the guard cannot read is not seen by it. Where the hook cannot see a command (opencode, restricted Codex), `bin/git`, `bin/gh`, `bin/curl`, `bin/wget`, `bin/http`, `bin/https`, `bin/xh`, `bin/xhs` and the mail programs (`sendmail`, `mail`, `mailx`, `msmtp`, `swaks`, `mutt`), first on the bot's PATH, ask the controller instead, with `BOT_OFFICE_GUARD_SHIM=1` (kept for child processes); a call by absolute path (`/usr/bin/git`) is out of their reach. If the controller cannot be reached, everything is refused. `scripts/drill_git_guard.md` is the live drill.

### Known limitation: jobs without a bot cannot be approved and resumed

A job started with no bot (`POST /v1/jobs` without `bot_id`) has no chat for a card and no session to resume, and running it again would repeat its earlier steps. So it cannot wait for an approval: its hook, shims and pre-push check run in local mode and refuse a guarded or sending action, and the job goes on without it. When the job ends, Needs-you shows a notice ("Job without a bot", Dismiss) naming the action and the reason. There is no Approve button on that notice and no way to approve the action and continue that job. To get a card, run the job again from a bot (assign it to a bot, or ask a bot in its chat): a bot's run pauses on an Approve / Deny card and resumes when you approve.

### At the push itself: git's pre-push check

Every run gets `core.hooksPath = bin/git-hooks` through its environment (`GIT_CONFIG_COUNT/KEY_n/VALUE_n`, which outranks the repo's and the user's config; the root-owned launcher passes exactly these variables). `bin/git-hooks/pre-push` runs `bin/bot-office-git-hook`, which sends the controller the refs the push really updates (old and new commit, fast-forward or not) and the remote's default branch. When the controller holds a GitHub token, it asks the GitHub API for github.com (`GET /repos/{owner}/{repo}`, cached an hour; unknown for 5 minutes). Otherwise, and for every other remote, the hook asks the remote itself with `git ls-remote --symref <remote> HEAD`, using the bot's own git credentials, with a 6 s timeout; the answer is cached in the repo's git dir for an hour (a failure for 5 minutes) and the controller remembers it for the command check. The clone's own `refs/remotes/<remote>/HEAD` is sent too, but only as a hint: it goes stale when the remote's default changes. A push that deletes a ref, is not a fast-forward, overwrites a tag, or goes to the default branch stops on a "GitPush" card, however the command was written. While the default is not known (no token and `ls-remote` failed), a push to `main`, `master`, `trunk`, `production` or the clone's own default stops; other branches pass.

An approval is bound to the exact update. The card names it (repository, full ref name, operation and old and new commit: `github.com/o/r refs/heads/main: 1a2b3c4d5e -> 6f7a8b9c0d (update)`), computed when it is asked from the repo's files; the pre-push check of the resume turn passes only a push whose every update matches it (same normalised remote URL, ref, old and new commit, and an operation the card allows), once, within 10 minutes. Anything else (another commit made since, another branch, another repo, a force push where a fast-forward was approved) asks again with a GitPush card for the update git really reports; approving that one lets the same command run and passes that exact update. A push whose update cannot be worked out beforehand (`--all`, `--mirror`, a variable, a glob, an unknown remote) gets a command card that says so; its push then asks at the pre-push check with the real update. No answer, an unreadable answer, a crash or a 60 s deadline stop the push. Then the repo's own pre-push hook runs: `core.hooksPath` from the repo's, the user's (`~/.gitconfig`) or the system config (the last one wins, as in git; never the one the run set), else `.git/hooks`. The other hooks in `bin/git-hooks` (commit, merge, rebase, checkout, rewrite, gc, applypatch, sendemail) only run the repo's own hook; `reference-transaction`, `post-update`, `push-to-checkout`, `fsmonitor-watchman`, `p4-*` and `post-index-change` are not passed on (they would run on every ref change and slow every git command), so a repo that relies on those hooks does not get them inside a Bot-Office run.

### GitHub credentials

The bots user should hold no GitHub credential of its own (no `gh auth login`, no `~/.git-credentials`, no token in an env file). The controller keeps one fine-grained token in `<private dir>/github-token` (mode 0600, owner botoffice; a symlink or a file readable by others is refused; `BOT_OFFICE_GITHUB_TOKEN_FILE` overrides the path). When it is there, each run's git gets `bin/bot-office-credential` as its only helper for `https://github.com` (an empty helper first clears any other; `useHttpPath`, so git names the repository), and `gh` called through the shim gets `GH_TOKEN` for that one call.

The token goes out only for a ticket. When the shim runs a git command that reaches a remote (push, fetch, pull, clone, ls-remote, remote update...) or a `gh` command, it first asks `POST /v1/agent/github-ticket` with the command's words and directory. The controller works out the operation and the GitHub repositories itself and issues a one-time ticket per repository, valid 2 minutes, bound to the job, the repository and the operation, only when it allows the call: a guarded push needs an approved card for that push, any other guarded call an approved card for that command; at most 300 tickets per job. The helper (git) or the shim (gh) exchanges the ticket at `POST /v1/agent/github-token`, once; git's request must name the ticket's repository. A request with only the agent token, a used, expired or another job's ticket, or a ticket for another repository gets 403. Each hand-out is logged in the gate log as "GitHub credential for {operation} {repository}", from the ticket, never the token and never text the caller sent. Tickets are forgotten when the job ends. Nothing is written to disk on the bots side and the token is not in the bot's environment. Jobs without a bot get no ticket.

Why the ticket comes from the command, not the pre-push check: git asks for the credential while it reads the remote's refs, before the pre-push hook runs, so the hook cannot be the thing that unlocks the token. The pre-push check stays the gate for which refs a push may change. CLI subscription logins (Claude, Codex, Cursor...) are not touched: this is only for GitHub.

### What is and isn't covered

Covered, client side: the command check (hook or shims) for every command it can read, including commands in string literals of inline code (`python -c`, `node -e`, `perl -e`..., a here-document fed to an interpreter), not script files; git's pre-push check for every push made with the system git inside a run, whatever the command looked like, bound to the exact approved update; the token only for a ticket the controller issued for a call it allows; fail-closed hooks.

Not covered, client side: a bot that is determined to get around Bot-Office can. Bots keep full CLI access (the owner's default) and run as the same Unix user as each other, and in the single-user layout as the controller too. A deliberate bot can:

- run a git binary or library that ignores `core.hooksPath`, or speak the git or HTTP protocol itself, by absolute path or from code the guard cannot read (a command built at run time, a file written and run later);
- ask `/v1/agent/github-ticket` itself with words for an operation the guard allows (a feature-branch push, a fetch) and spend the ticket on something else within 2 minutes, by hand-written HTTP to GitHub; the ticket narrows what the token is handed out for and logs it, it does not limit what the token can do once out;
- in the single-user layout, read the token file (`<private dir>` is the same user's), the agent token of its own job, and the master token;
- push or send from a script file, which is not read (git's pre-push check still sees the push; a send from a script is seen only through the shims where they are on);
- send to an endpoint that is not on the sending list, or through a connector tool whose name does not look like sending;
- push to a branch that is the remote's default when that default is not known (no token and `ls-remote` failed) and is not `main`, `master`, `trunk`, `production` or the clone's own default.

Submodule fetches git starts internally get no ticket (they fail on private GitHub repos; the shim covers the command the bot typed). Deletes outside the workspace and connector (MCP) calls are judged only from the command or tool call. These checks keep a well-meaning bot from making a mistake; they do not stop an adversarial one.

#### The owner's options, and what each costs the bots

| Option | What it closes | Cost to bot capability |
|---|---|---|
| GitHub rulesets on every repo the token reaches (below) | pushes to the default branch, force pushes, deletes, merges without a review, whatever runs on this computer | none for feature-branch work; merges need a human review on GitHub |
| A fine-grained token: only the repos the bots work on; Contents and Pull requests write, no Administration, Secrets, Workflows or Actions write | what a leaked token can do | bots cannot change repo settings, secrets or workflows (they already ask for those) |
| The isolation layout (`deploy/install-isolation.sh`): the controller as `botoffice`, bots as `ubuntu` | the token file, the database and the master token become unreadable to bots | none to CLI access; sudo for bots is per-bot opt-in |
| A separate Unix user for the bots with no repo write credential at all, and pushes only through the controller | a bot can no longer use any GitHub credential directly | every push and gh write goes through the controller (needs a controller-side git proxy or push endpoint; not built) |
| Bubblewrap (or a container) around each run: read-only system paths, the workspace writable, network only through a proxy | absolute-path binaries, hand-written requests, reading other bots' files | the biggest: no arbitrary system changes, no direct network, tools have to be installed into the sandbox; this is the "full CLI access" the owner chose to keep |
| A controller-side git smart-HTTP proxy holding the token | the token never reaches the bots' side at all | git remotes point at the proxy; gh still needs the token or an API proxy |

The backstop is on GitHub: rulesets on every repo the token can reach (block pushes to the default branch except through a pull request, block force pushes and deletion of the default branch and release tags, require a review the bot cannot give) and a fine-grained token without Administration, Secrets or Workflows write. Those hold whatever happens on this computer.

## Which engines can pause

Bot-Office runs every engine headless, with its own permission prompts turned off (nobody is at the terminal to answer them). The engines' own ideas of what is risky (Claude Code's permission rules, Codex's sandbox escalations, Cursor's allowlist, Antigravity's review step) only work with a person at the terminal, and Bot-Office cannot read them from a hook. Bot-Office's classes take their place.

| Engine | Can pause? | How |
| --- | --- | --- |
| Claude Code | yes | `PreToolUse` hook in the run's `--settings` |
| Codex, full access | yes | `PreToolUse` hook passed with `-c hooks...` (attended runs too: bypass flag kept, hook added) |
| Codex, restricted access | no | no hook; Codex's own sandbox and approval limits apply instead; the git guard uses PATH shims |
| Cursor | yes | `.cursor/hooks.json`: shell commands and connector (MCP) calls |
| Antigravity | yes | `.agents/hooks.json` `PreToolUse` hook |
| opencode | no | no hook mechanism; classes do not apply; the git guard uses PATH shims |

For a bot that can't pause, Bot-Office does what it can without a hook. Its SELF.md lists the switched-on classes and tells it never to do them itself: it prepares a draft or the exact command and ends with `waiting_on_user — needs your approval`. Settings → Approvals names every such bot next to each class that is on and in a warning, because this is an instruction, not something Bot-Office can enforce. Restricted Codex and restricted opencode also keep their own sandbox and permission limits; opencode with full access runs with `--auto` and has neither.

Settings → Approvals shows each engine's state on this computer: "can pause" once `scripts/probe_gate.sh <engine>` has proven the hook, "not yet proven" when it is wired but untested, "can't pause" when there is no hook. The gate fails closed in every run kind: if the engine cannot reach Bot-Office, the tool call is refused.
