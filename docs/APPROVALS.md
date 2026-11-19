# Approvals

Bots rarely ask. They work on their own, and the owner picks the kinds of action that should wait for a yes. These are the **approval classes** in Settings → Approvals.

**A checkpoint, not enforcement.** A class is a tripwire on what a bot is about to do: it stops the tool calls it recognises on engines that can pause, so the owner gets a say before the usual risky steps. It is not a sandbox and does not make anything impossible:

- matching is on a tool's name and command line, so a command built at run time (`s=sudo; $s ...`) or hidden in a script the bot wrote is not caught;
- on engines that can't pause (opencode, Codex with restricted access, and an engine whose hook is not proven yet) a class is only advice in the bot's SELF.md; Settings → Approvals names those bots next to every class that is on;
- in the owner's own chat the gate fails open if the engine cannot reach Bot-Office.

Rely on isolation, access settings and the owner's `never` action rules for what must not happen; use classes to be asked first.

## How a step gets approved

1. A bot tries an action, for example sending an email through a connector.
2. The engine's gate hook (`bin/bot-office-gate`) asks Bot-Office before the tool runs. If the action is in a class that is switched on, Bot-Office refuses that one tool call, records an approval and tells the bot to end its turn with `BOT-OFFICE-STATUS: waiting_on_user — approval #N`.
3. The bot's chat shows an Approve / Deny card with the class and the whole action. Needs-you lists it too. Automatic continuation does not run while it waits.
4. **Approve** gives a one-shot grant for exactly that action and queues a resume turn; only that turn can use the grant, once, within 24 hours. The resume prompt is kept out of the chat; the card reads "Approved".
5. **Deny**, or no answer within the timeout (60 minutes by default, 0 = wait until answered), leaves the task stopped with a one-line note. An expired card can still be approved later ("Approve and run").

Order of decisions: protected secrets are always refused; then the owner's action rules (`never` > `ask` > `allow`); then the approval classes; then, for unattended runs under the strict profile (watcher runs, and scheduled runs of bots set to "gated"), every outbound or destructive action asks as before. An owner `allow` rule for an action therefore beats a class.

## Classes

| Class | Default | Matches |
| --- | --- | --- |
| Sending email or messages | on | `sendmail`, `mutt`, `mail`...; connector tools named send / reply / forward / post_message / sms / dm. Messages to other bots (bot-msg, desk grok send) never ask |
| Purchases and payments | on | connector tools named pay / purchase / checkout / buy / charge / refund / create_payment / create_order...; `stripe ... create` |
| Merging, releasing, force-pushing | on | `gh pr merge`, `gh release create`, `npm publish`, `docker push`, `git push` to main or master or with `--force` |
| Admin and system changes | on | `sudo`, `systemctl`, `crontab`, `shutdown`, `reboot`, user accounts, `podman system prune` |
| Deleting outside the bot's workspace | on | `rm -r` of paths outside the workspace, writes to `~/.ssh` and other protected places, `dd of=`; connector tools named delete / trash / destroy / purge |
| Pushing code and posting on GitHub | off | any `git push`, `gh pr create/comment`, `gh issue ...`; GitHub connector writes |
| Logging in to other computers | off | `ssh`, `scp`, `sftp`, `rsync host:` |
| Controlling printers and the screen | off | PrusaLink and OctoPrint commands, `desk click/type/key` |
| Any connector action | off | every connector (MCP) tool call, including reads |

Your own classes match a command pattern or a tool-name pattern (regular expressions). Matching is a tripwire on the tool's name and its command line, not a sandbox: a bot that hides a command inside a script it wrote first is not caught. The bot's own instructions tell it not to work around a refused step.

## Which engines can pause

Bot-Office runs every engine headless, with its own permission prompts turned off (nobody is at the terminal to answer them). The engines' own ideas of what is risky (Claude Code's permission rules, Codex's sandbox escalations, Cursor's allowlist, Antigravity's review step) only work with a person at the terminal, and Bot-Office cannot read them from a hook. Bot-Office's classes take their place.

| Engine | Can pause? | How |
| --- | --- | --- |
| Claude Code | yes | `PreToolUse` hook in the run's `--settings` |
| Codex, full access | yes | `PreToolUse` hook passed with `-c hooks...` |
| Codex, restricted access | no | no hook; Codex's own sandbox and approval limits apply instead |
| Cursor | yes | `.cursor/hooks.json`: shell commands and connector (MCP) calls |
| Antigravity | yes | `.agents/hooks.json` `PreToolUse` hook |
| opencode | no | no hook mechanism; classes do not apply |

For a bot that can't pause, Bot-Office does what it can without a hook. Its SELF.md lists the switched-on classes and tells it never to do them itself: it prepares a draft or the exact command and ends with `waiting_on_user — needs your approval`. Settings → Approvals names every such bot next to each class that is on and in a warning, because this is an instruction, not something Bot-Office can enforce. Restricted Codex and restricted opencode also keep their own sandbox and permission limits; opencode with full access runs with `--auto` and has neither.

Settings → Approvals shows each engine's state on this computer: "can pause" once `scripts/probe_gate.sh <engine>` has proven the hook, "not yet proven" when it is wired but untested, "can't pause" when there is no hook. In your own chat the gate fails open if the engine cannot reach Bot-Office (protected paths are still refused); unattended runs fail closed.
