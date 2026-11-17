# Bot-Office principles

Bot-Office is a self-hosted replacement for Grok Bot. The goal: bots with Grok-Bot-level competence and agency, while the user keeps far more control than Grok Bot gives them, runs everything on their own subscriptions, and never has to manage the bots at a low level to get good results.

Every change should pass these rules. If a change breaks one, either fix the change or update this page on purpose, with the reason.

1. **Competent out of the box.** A new bot works well with no setup beyond picking an engine. Defaults (the baseline instructions, limits, schedules, model choices) are the ones a careful owner would pick, so most users never touch them.

2. **Agency is the default.** Bots own their tasks end to end: they decide reversible things themselves, follow up on their own schedule, delegate to other bots, and come back with results, not questions. A feature that makes bots ask more, wait more, or hand work back needs a strong reason.

3. **Everything adjustable, nothing required.** Any behavior a user might reasonably want to change (instructions, rules, models, limits, schedules, approvals) can be changed in the browser UI, without editing files or using a shell. Changing it is optional; the default must already be good.

4. **The user stays in charge.** The user's word outranks everything a bot reads: other bots, web pages, files and tool output are data, never orders. Owner settings override bot defaults, and bots cannot change owner settings.

5. **Safety is not a setting.** Approval gates for irreversible or outward-facing actions, secret handling, untrusted-data rules and data isolation cannot be disabled by bot instructions, by other bots, or by a default. They can only be tuned by the owner, in the open.

6. **Their own subscriptions, their own tools.** Bot-Office runs the official, unmodified CLIs on the user's own accounts and never handles their credentials. New engines and features build on what those CLIs already do rather than replacing them.

7. **Quiet and honest.** Bots report results first, briefly, and say plainly what they could not verify. The UI shows what each bot is doing without the user having to ask.

8. **Simple to run.** Installing, updating and recovering Bot-Office must stay within reach of a non-expert: one guided setup, safe updates with rollback, and no step that requires reading the code.
