"""0.12.23: the gate-gap rule changes for a Bot-Office DB (new allow rules, the E8 deploy and send ask
patterns). usage: gate_gap_rules.py DB [--dry-run]. Idempotent; back the DB up first."""
import datetime
import re
import sqlite3
import sys

NOTE = "0.12.23 gate-gap (owner-approved draft allow rules, 2026-11-22): "
# 0.12.23: shell allow rules are matched against each command part the shell runs (words joined by
# one space; redirect operators dropped, their targets kept), anchored, so nothing can ride along.
PATHW = r"(?:~/(?:bot-office-reports|bo-work|bot-office-work)|/tmp|\$R)/[\w${}-]+(?:[./][\w${}-]+)*"
NEW = [
    ("shell", r"^(?:timeout \d+[smh]? )?codex exec (?:-m [\w.-]+ )?-c model_reasoning_effort=\w+ "
              r"--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -C ~/(?:bo-work|bot-office-work)/[\w-]+(?:/[\w-]+)* -"
              r"(?: (?:[12]|" + PATHW + r"))*$",
     "nested codex exec review run in ~/bo-work or ~/bot-office-work, prompt on stdin (draft item 3, card #19)"),
    ("shell", r"^(?=.*brief\.md)(?:timeout \d+[smh]? )?(?:env -u ANTHROPIC_API_KEY )?claude -p --model [\w.-]+ "
              r"--permission-mode bypassPermissions(?: (?:[12]|" + PATHW + r"))+$",
     "nested claude -p review run, brief.md on stdin from a report/work dir or /tmp (draft item 3, cards #17/#34)"),
    ("tool", r"mcp__(?:plugin_railway_railway|claude_ai_Railway)__(?:whoami|list_(?:workspaces|projects|services|deployments|domains|tcp_proxies)|"
             r"environment_status|domain_status|private_network_status|get_service_config|get_logs|service_metrics|http_(?:error_rate|requests|response_time)|docs_(?:search|fetch)|search_templates)",
     "read-only Railway MCP calls (cards #21/#22); list_variables and every write still ask"),
]
OLD_ALLOWS = []  # earlier drafts of this script, removed if present
OLD_SEND = r"(?i)\b(mail|mailx|sendmail|msmtp|mutt|swaks)\b|desk\s+grok\s+send|\b(slack|telegram|twilio)\b.*\b(send|post|message)\b"
NEW_SEND = r"(?i)\b(mail|mailx|sendmail|msmtp|mutt|swaks)\b|\b(slack|telegram|twilio)\b.*\b(send|post|message)\b"
OLD_DEPLOY = (r"(?i)\bdeploy|\brailway\s+up\b|\bkubectl\s+(apply|rollout|delete)\b|\bterraform\s+apply\b|\bvercel\b.*--prod|"
              r"\bnetlify\s+deploy|\bfly(ctl)?\s+deploy|\bsystemctl\s+(restart|reload|start)\b|\bansible-playbook\b")
NEW_DEPLOY = (r"(?i)(?:^|[\n;&|(]|\b(?:sudo|bash|sh|exec|timeout \d+\w?|npm run|pnpm(?: run)?|yarn(?: run)?|make|just|task))"
              r"[ \t]*(?:\w+=\S{0,99}[ \t]+){0,5}(?:[\w.~$-]{0,99}/){0,8}deploy[\w.-]{0,40}(?=[\s;&|)]|$)|"
              r"\b(?:railway|vercel|netlify|fly(?:ctl)?|heroku|firebase|serverless|wrangler|eb|pulumi|gcloud(?: [\w-]+){0,3})"
              r"(?: --?[\w-]+(?:=\S{0,99})?){0,6} (?:deploy|up)\b|\bkubectl\s+(apply|rollout|delete)\b|\bterraform\s+apply\b|"
              r"\bvercel\b.{0,300}--prod|\bsystemctl\s+(restart|reload|start)\b|\bansible-playbook\b")
def apply(db_path: str, dry: bool = False) -> list:
    """Apply (or with *dry* only count) the changes; returns the effect counts."""
    for _, p, _n in NEW: assert len(p) <= 500, (len(p), p); re.compile(p)
    assert len(NEW_DEPLOY) <= 500, len(NEW_DEPLOY); re.compile(NEW_DEPLOY)
    db = sqlite3.connect(db_path)
    with db:
        n_send = db.execute("SELECT count(*) FROM action_rules WHERE effect='ask' AND kind='shell' AND pattern=?", (OLD_SEND,)).fetchone()[0]
        deploys = db.execute("SELECT id, pattern FROM action_rules WHERE effect='ask' AND kind='shell' AND pattern=?", (OLD_DEPLOY,)).fetchall()
        print(f"send rows {n_send}, deploy rows {len(deploys)}, new deploy pattern {len(NEW_DEPLOY)} chars")
        if not dry:
            db.execute("UPDATE action_rules SET pattern=?, note=note || ' [0.12.23: desk grok send judged by the outbound preset]' WHERE effect='ask' AND kind='shell' AND pattern=?", (NEW_SEND, OLD_SEND))
            for rid, _p in deploys:
                db.execute("UPDATE action_rules SET pattern=?, note=note || ' [0.12.23: deploy commands, not the word]' WHERE id=?", (NEW_DEPLOY, rid))
            for pattern in OLD_ALLOWS:
                db.execute("DELETE FROM action_rules WHERE bot_id IS NULL AND effect='allow' AND pattern=?", (pattern,))
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for kind, pattern, note in NEW:
                if not db.execute("SELECT 1 FROM action_rules WHERE bot_id IS NULL AND effect='allow' AND kind=? AND pattern=?", (kind, pattern)).fetchone():
                    db.execute("INSERT INTO action_rules (bot_id,effect,kind,pattern,scope,note,created_at) VALUES (NULL,'allow',?,?,'all',?,?)",
                               (kind, pattern, NOTE + note, stamp))
        for kind, p, _n in NEW: print(kind, len(p))
        print(db.execute("SELECT effect, count(*) FROM action_rules GROUP BY effect").fetchall())
    return db.execute("SELECT effect, count(*) FROM action_rules GROUP BY effect").fetchall()


if __name__ == "__main__":
    apply(sys.argv[1], "--dry-run" in sys.argv[2:])
