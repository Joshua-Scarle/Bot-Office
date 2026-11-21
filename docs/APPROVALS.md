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

## Classes

| Class | Default | Matches |
| --- | --- | --- |
| Sending email or messages | always on (locked) | `sendmail`, `mutt`, `mail`, `msmtp`, `swaks`...; any non-GET HTTP(S) request to a host off the allowlist (below); connector tools whose name sends (send / post / reply / message / email / publish / tweet / comment / create_issue...). Messages to other bots (bot-msg, desk grok send) never ask |
| Purchases and payments | on | connector tools named pay / purchase / checkout / buy / charge / refund / create_payment / create_order...; `stripe ... create` |
| Merging, releasing, force-pushing | on | `gh pr merge`, `gh release create`, `npm publish`, `docker push`, `git push` to main or master or with `--force` |
| Admin and system changes | on | `sudo`, `systemctl`, `crontab`, `shutdown`, `reboot`, user accounts, `podman system prune` |
| Deleting outside the bot's workspace | on | `rm -r` of paths outside the workspace, writes to `~/.ssh` and other protected places, `dd of=`; connector tools named delete / trash / destroy / purge |
| Pushing code and posting on GitHub | off | any `git push`, `gh pr create/comment`, `gh issue ...`; GitHub connector writes |
| Logging in to other computers | off | `ssh`, `scp`, `sftp`, `rsync host:` |
| Controlling printers and the screen | off | PrusaLink and OctoPrint commands, `desk click/type/key` |
| Any connector action | off | every connector (MCP) tool call, including reads |

Sending email or messages cannot be switched off (the box is greyed and the API refuses it with 422) and no `allow` rule opens it: like the git guard, it asks in every run kind.

What counts as sending: a POST, PUT, PATCH or DELETE (or a request with a body) made with `curl`, `wget`, `http`/`https` (httpie), `xh`, or from code in a `python -c` / `node -e` one-liner or a script the command runs (`requests`/`httpx`/`aiohttp`/`fetch`/`axios`... `.post(`, `method: 'POST'`, `urlopen` with data), to any host except: `localhost`, `127.0.0.1`, `::1`, the controller (`BOT_OFFICE_API_URL`), and the package registries used for installs (pypi.org, files.pythonhosted.org, registry.npmjs.org, registry.yarnpkg.com, crates.io, proxy.golang.org, sum.golang.org, rubygems.org, Maven Central). `BOT_OFFICE_SEND_ALLOW_HOSTS` (comma or space separated host names, in the controller's environment) adds hosts. A GET never asks. A request whose host or method cannot be read asks. Mail programs and SMTP from code always ask. Connector tools ask by name: a send verb (send, post, reply, publish, tweet, comment, message, email...), or create/add with a message-like object (issue, comment, message, post, review...); a tool whose name starts with get / list / read / search does not.

Your own classes match a command pattern or a tool-name pattern (regular expressions). A pattern sees at most the first 4096 characters; a longer command counts as in your class, so it asks. A pattern that is too slow on long, repetitive input (nested repeats such as `(a+)+`) is refused when you save it. Matching is a tripwire on the tool's name and its command line, not a sandbox: a bot that hides a command inside a script it wrote first is not caught. The bot's own instructions tell it not to work around a refused step.

## The git guard (always asks)

Some actions are not a class and have no switch. In every run kind (chat, routine, wake-up and auto-continue, assignment, bot-to-bot inbox, watcher trigger; strict profile or not), for every bot whatever its CLI access, they stop on an Approve / Deny card first when the guard sees them. It is defence in depth, not an inescapable boundary for bots with full CLI access running as the same Unix user (see [What is and isn't covered](#what-is-and-isnt-covered)):

- `git push` to the default branch (the branch the clone's `refs/remotes/<remote>/HEAD` names, and always `main`, `master`, `trunk`), including `HEAD:main`, `+x:main`, `:main`, `--all`, `--mirror`, and a plain `git push` made while on that branch (or with `push.default=matching`, an upstream on it, or a remote refspec); a push whose branch cannot be known, or to a remote whose default branch is not known, asks too
- any force push (`-f`, `--force`, `--force-with-lease`, `+refspec`), `git push --delete` / `:ref`, `git push --no-verify`, `git branch -d/-D`, `git tag -d`, deleting the default branch
- changing the run's git hooks or credentials: `git -c core.hooksPath=...` / `credential.*` / `include.*`, `git config` of those keys, `GIT_CONFIG_*`, `GIT_DIR`, `GH_TOKEN` and similar in the environment, `env -i` / `env -u`, `gh auth token|login|setup-git`, `git credential`
- `gh pr merge`, `gh repo delete|edit|rename|archive|transfer|sync`, `gh release delete`, `gh secret|variable set|delete`, `gh api` with DELETE, PATCH or PUT (except plain issue edits), any POST/PUT/PATCH/DELETE under `repos/`, `orgs/` and similar that is not on a short list of safe writes (issues, comments, labels, opening a PR, reviews, statuses, check runs), GraphQL mutations not on the safe list (comments, issues, reviews, labels...), and GraphQL whose body cannot be read (`--input -`, a variable, a missing file); request bodies in files (`--input file`, `-F query=@file`, `curl -d @file`, `--data-binary @file`, `--post-file`) are read and judged; the same through `curl`/`wget`/`http`
- connector (MCP) tools that merge, delete a branch, tag, repo or release, change repo settings, or commit or push to the target repo's default branch (read from GitHub, below), or to a repo whose default branch is not known or not named
- deleting or moving files outside the bot's workspace (`rm`, `mv`, `find -delete`, `rsync --delete`, `shred`, trash, patch deletes); paths are resolved through symlinks, and a path with an unknown variable (`$DIR/x`) or a hidden-file glob (`.*`) counts as outside

It is read from the command itself (env prefixes, `git -C`, `-c`, `sh -c`, `eval`, `$( )`, quoting, chains, aliases, and scripts and interpreter code the command runs), and leans to asking. Every other command a bot runs, git included, is untouched. Only a `never` rule is stricter; no `allow` rule, switched-off class, timeout, bot setting or access level opens it. Code: `src/botoffice/git_guard.py`, applied first in `Rules.evaluate`.

The command check is a tripwire, not a sandbox: a command assembled at run time from a file the guard cannot read is not seen by it. Where the hook cannot see a command (opencode, restricted Codex), `bin/git`, `bin/gh`, `bin/curl`, `bin/wget`, `bin/http`, `bin/https`, `bin/xh`, `bin/xhs` and the mail programs (`sendmail`, `mail`, `mailx`, `msmtp`, `swaks`, `mutt`), first on the bot's PATH, ask the controller instead, with `BOT_OFFICE_GUARD_SHIM=1` (kept for child processes); a call by absolute path (`/usr/bin/git`) is out of their reach. A job started with no bot (`POST /v1/jobs` without `bot_id`) has no chat for a card and no session to resume (a rerun would repeat its earlier steps), so it cannot wait for an approval: its hook, shims and pre-push check run in local mode and refuse a guarded or sending action, and the owner gets a Needs-you notice ("Job without a bot", Dismiss) naming the action and the reason when the job ends. If the controller cannot be reached, everything is refused. `scripts/drill_git_guard.md` is the live drill.

### At the push itself: git's pre-push check

Every run gets `core.hooksPath = bin/git-hooks` through its environment (`GIT_CONFIG_COUNT/KEY_n/VALUE_n`, which outranks the repo's and the user's config; the root-owned launcher passes exactly these variables). `bin/git-hooks/pre-push` runs `bin/bot-office-git-hook`, which sends the controller the refs the push really updates (old and new commit, fast-forward or not) and the remote's default branch. For github.com the controller asks the GitHub API (`GET /repos/{owner}/{repo}`, with its token, cached an hour; unknown for 5 minutes), for other remotes `git ls-remote --symref`. A push that deletes a ref, is not a fast-forward, overwrites a tag, goes to the default branch, or goes anywhere while the default is unknown stops on a "GitPush" card, however the command was written.

An approval is bound to the exact update. The card names it (repository, full ref name, operation and old and new commit: `github.com/o/r refs/heads/main: 1a2b3c4d5e -> 6f7a8b9c0d (update)`), computed when it is asked from the repo's files; the pre-push check of the resume turn passes only a push whose every update matches it (same normalised remote URL, ref, old and new commit, and an operation the card allows), once, within 10 minutes. Anything else (another commit made since, another branch, another repo, a force push where a fast-forward was approved) asks again with a GitPush card for the update git really reports; approving that one lets the same command run and passes that exact update. A push whose update cannot be worked out beforehand (`--all`, `--mirror`, a variable, a glob, an unknown remote) gets a command card that says so; its push then asks at the pre-push check with the real update. No answer, an unreadable answer, a crash or a 60 s deadline stop the push. Then the repo's own pre-push hook runs: `core.hooksPath` from the repo's, the user's (`~/.gitconfig`) or the system config (the last one wins, as in git; never the one the run set), else `.git/hooks`. The other hooks in `bin/git-hooks` (commit, merge, rebase, checkout, rewrite, gc, applypatch, sendemail) only run the repo's own hook; `reference-transaction`, `post-update`, `push-to-checkout`, `fsmonitor-watchman`, `p4-*` and `post-index-change` are not passed on (they would run on every ref change and slow every git command), so a repo that relies on those hooks does not get them inside a Bot-Office run.

### GitHub credentials

The bots user should hold no GitHub credential of its own (no `gh auth login`, no `~/.git-credentials`, no token in an env file). The controller keeps one fine-grained token in `<private dir>/github-token` (mode 0600, owner botoffice; a symlink or a file readable by others is refused; `BOT_OFFICE_GITHUB_TOKEN_FILE` overrides the path). When it is there, each run's git gets `bin/bot-office-credential` as its only helper for `https://github.com` (an empty helper first clears any other; `useHttpPath`, so git names the repository), and `gh` called through the shim gets `GH_TOKEN` for that one call.

The token goes out only for a ticket. When the shim runs a git command that reaches a remote (push, fetch, pull, clone, ls-remote, remote update...) or a `gh` command, it first asks `POST /v1/agent/github-ticket` with the command's words and directory. The controller works out the operation and the GitHub repositories itself and issues a one-time ticket per repository, valid 2 minutes, bound to the job, the repository and the operation, only when it allows the call: a guarded push needs an approved card for that push, any other guarded call an approved card for that command; at most 300 tickets per job. The helper (git) or the shim (gh) exchanges the ticket at `POST /v1/agent/github-token`, once; git's request must name the ticket's repository. A request with only the agent token, a used, expired or another job's ticket, or a ticket for another repository gets 403. Each hand-out is logged in the gate log as "GitHub credential for {operation} {repository}", from the ticket, never the token and never text the caller sent. Tickets are forgotten when the job ends. Nothing is written to disk on the bots side and the token is not in the bot's environment. Jobs without a bot get no ticket.

Why the ticket comes from the command, not the pre-push check: git asks for the credential while it reads the remote's refs, before the pre-push hook runs, so the hook cannot be the thing that unlocks the token. The pre-push check stays the gate for which refs a push may change. CLI subscription logins (Claude, Codex, Cursor...) are not touched: this is only for GitHub.

### What is and isn't covered

Covered, client side: the command check (hook or shims) for every command it can read, including commands in string literals of `python -c`, `node -e`, `perl -e`... one-liners and scripts the command runs; git's pre-push check for every push made with the system git inside a run, whatever the command looked like, bound to the exact approved update; the token only for a ticket the controller issued for a call it allows; fail-closed hooks.

Not covered, client side: a bot that is determined to get around Bot-Office can. Bots keep full CLI access (the owner's default) and run as the same Unix user as each other, and in the single-user layout as the controller too. A deliberate bot can:

- run a git binary or library that ignores `core.hooksPath`, or speak the git or HTTP protocol itself, by absolute path or from code the guard cannot read (a command built at run time, a file written and run later);
- ask `/v1/agent/github-ticket` itself with words for an operation the guard allows (a feature-branch push, a fetch) and spend the ticket on something else within 2 minutes, by hand-written HTTP to GitHub; the ticket narrows what the token is handed out for and logs it, it does not limit what the token can do once out;
- in the single-user layout, read the token file (`<private dir>` is the same user's), the agent token of its own job, and the master token;
- send from code the guard cannot read, or to an allowlisted host; call a connector tool whose name does not look like sending.

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
