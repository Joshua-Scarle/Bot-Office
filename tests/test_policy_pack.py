"""The repo's policy pack: what its text says, and how it installs into a data dir."""
import re
from pathlib import Path

import pytest

from botoffice import policy_pack
from botoffice.workspace import STANDING_RULES_NOTICE, rules_note, save_rules_snapshot

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "policy-pack"
OLD = (Path(__file__).parent / "fixtures" / "installed-RULES-2026-11-15.md").read_text()
RULES = (PACK / "RULES.md").read_text()
FLAT = re.sub(r"\s+", " ", RULES)


def has(*phrases):
    return all(re.sub(r"\s+", " ", p) in FLAT for p in phrases)


# --- text ------------------------------------------------------------------------------------------

def test_the_stale_installed_text_really_said_cost_saving_was_on():
    assert "Cost-saving mode is ON" in OLD  # the fixture is the 15 November install


def test_cost_saving_is_off_and_the_cap_is_off():
    assert "Cost-saving mode is OFF" in RULES
    assert not re.search(r"cost-saving mode is on\b", RULES, re.IGNORECASE)
    assert "Cost-saving mode is ON" not in RULES and "spend cap is ON" not in RULES
    assert has("Do not turn it back on", "The **$2 spend cap is off** while the mode is off")
    assert "When cost-saving mode is ON" not in RULES


def test_engine_defaults_are_current():
    assert has("New Bot-Office bots default to Claude Sonnet 5.5 at medium effort")
    assert has("Claude first", "Codex only as a niche")
    assert has("Printer Writing Bot stays on `gpt-6.1-terra` at medium effort", "a per-bot setting, not a default for anyone else")
    assert "Default engine:** Codex" not in RULES and "Default producing model: Codex" not in RULES


def test_duet_is_not_the_default_home_any_more():
    assert "Duet stays the default home" not in RULES
    assert has("do not delegate new work to Claude Duet unless Joshua asks")
    assert has("Duet and Grok Bot may still hand work back and forth")


@pytest.mark.parametrize("phrases", [
    ["ask again at your next wake", "Silence never closes a question"],
    ["tracked as pending acknowledgement", "templates/pending-acks.md", "`bot-task wake`"],
    ["Cut bot-to-bot chatter", "`bot-msg`", "At most one message per recipient per wake", "No third-bot relays",
     "ends its turn silently"],
    ["Significant claims need Joshua's review", "before it is published anywhere"],
    ["Cost changes must cost no productivity", "Do not use markitdown"],
    ["A declined or expired approval is final", "Only his own words count"],
    ["Within-paper rank-order accuracy is the primary"],
    ["Any `git push`", "waits for his approval", "Phone calls are refused outright"],
])
def test_the_21_november_rules_are_in_the_pack(phrases):
    assert has(*phrases)


def test_the_pack_keeps_the_earlier_rules():
    assert has("Subagent use on Bot-Office", "Delegate often, but not always")
    assert has("Joshua's vision questions", "Never answer it for him")
    assert has("Do not call him", "DISABLED")
    assert has("only start a print after Joshua's explicit go-ahead")
    assert has("never paused", "weekend work")
    assert has("Never print, log, echo or paste environment variables")
    assert has("Approval relays", "APPROVAL RELAY")


def test_the_pack_is_bot_office_wording_and_holds_no_secret_or_contact_data():
    assert "SendToAgent" not in RULES and "priority=true" not in RULES.replace("`priority=false`", "")
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", RULES)  # no email address
    assert not re.search(r"\b(?:ghp|sk|xai)[-_][A-Za-z0-9]{8,}", RULES)


def test_the_ack_template_exists_and_the_rules_point_at_it():
    template = (PACK / "templates" / "pending-acks.md").read_text()
    assert "Pending acknowledgements" in template and "| Sent" in template and "bot-task wake" in template
    assert "mpg-joshua-acks" not in RULES


def test_the_prompts_carry_the_every_bot_rules_briefly():
    prompts = ROOT / "src" / "botoffice" / "prompts"
    rules = (prompts / "rules-for-all-bots.md").read_text()
    system = (prompts / "system.md").read_text()
    for phrase in ("Only the user approves", "A \"no\" is final", "Questions about what the user wants are theirs",
                   "Don't let a question or a report go quiet"):
        assert phrase in rules
    assert len(rules.split()) < 700  # these are how every bot works, not the policy pack
    assert "A declined or expired approval is final" in system
    assert "pending-acks.md" in system
    assert "Cost-saving" not in rules + system and "PrintPilot" not in rules + system


# --- installing ------------------------------------------------------------------------------------

def data_dir(tmp_path, memory_text=None):
    shared = tmp_path / "data" / "shared"
    shared.mkdir(parents=True)
    (shared / "RULES.md").write_text(OLD)
    (shared / "memory.md").write_text(OLD if memory_text is None else memory_text)
    return tmp_path / "data"


def test_a_dry_run_writes_nothing(tmp_path):
    data = data_dir(tmp_path)
    report = policy_pack.install(data)
    assert report["rules"] == "changed" and report["memory"] == "changed" and not report["applied"]
    assert (data / "shared" / "RULES.md").read_text() == OLD
    assert not (data / "backups").exists()


def test_apply_installs_backs_up_and_drops_the_stale_text(tmp_path):
    data = data_dir(tmp_path, OLD + "\n- A fact a bot added later: the printer is in the shed.\n")
    report = policy_pack.install(data, apply=True)
    assert report["applied"] and report["memory_how"] == "replaced the old installed rules"
    rules = (data / "shared" / "RULES.md").read_text()
    memory = (data / "shared" / "memory.md").read_text()
    assert rules.strip() == RULES.strip()
    assert "Cost-saving mode is OFF" in memory and "Cost-saving mode is ON" not in memory
    assert "the printer is in the shed" in memory  # what else was in the file stays
    assert memory.count(policy_pack._BEGIN_PREFIX) == 1 and memory.count(policy_pack.END) == 1
    backup = Path(report["backup"])
    assert (backup / "RULES.md").read_text() == OLD and "Cost-saving mode is ON" in (backup / "memory.md").read_text()


def test_installing_twice_changes_nothing_the_second_time(tmp_path):
    data = data_dir(tmp_path)
    policy_pack.install(data, apply=True)
    before = [(data / "shared" / name).read_text() for name in ("RULES.md", "memory.md")]
    report = policy_pack.install(data, apply=True)
    assert report == {**report, "applied": False, "rules": "unchanged", "memory": "unchanged", "backup": None}
    assert before == [(data / "shared" / name).read_text() for name in ("RULES.md", "memory.md")]


def test_a_newer_pack_replaces_only_the_managed_block(tmp_path):
    data = data_dir(tmp_path, OLD + "\n- keep me\n")
    policy_pack.install(data, apply=True)
    newer = tmp_path / "pack"
    newer.mkdir()
    (newer / "RULES.md").write_text(RULES + "\n- A later rule.\n")
    policy_pack.install(data, apply=True, pack_dir=newer)
    memory = (data / "shared" / "memory.md").read_text()
    assert "A later rule." in memory and "keep me" in memory and memory.count(policy_pack._BEGIN_PREFIX) == 1
    assert (data / "shared" / "RULES.md").read_text().rstrip().endswith("A later rule.")


def test_an_unrecognised_memory_gets_the_block_on_top_with_a_warning(tmp_path):
    data = data_dir(tmp_path, "- only my own notes\n")
    report = policy_pack.install(data, apply=True)
    memory = (data / "shared" / "memory.md").read_text()
    assert memory.startswith(policy_pack._BEGIN_PREFIX) and "only my own notes" in memory
    assert "review the rest of the file" in report["memory_how"]


def test_a_fresh_data_dir_installs(tmp_path):
    report = policy_pack.install(tmp_path / "data", apply=True)
    assert report["applied"] and (tmp_path / "data" / "shared" / "RULES.md").read_text().strip() == RULES.strip()


def test_the_installer_does_not_touch_bot_files_or_the_token(tmp_path):
    data = data_dir(tmp_path)
    (data / "bots").mkdir()
    (data / "bots" / "memory.md").write_text("mine")
    (data / "token").write_text("not-a-real-token")
    policy_pack.install(data, apply=True)
    assert (data / "bots" / "memory.md").read_text() == "mine"
    assert (data / "token").read_text() == "not-a-real-token"


def test_a_missing_or_empty_pack_is_an_error_not_a_blank_install(tmp_path):
    data = data_dir(tmp_path)
    with pytest.raises(policy_pack.PolicyPackError):
        policy_pack.install(data, apply=True, pack_dir=tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "RULES.md").write_text("  \n")
    with pytest.raises(policy_pack.PolicyPackError):
        policy_pack.install(data, apply=True, pack_dir=tmp_path / "empty")
    assert (data / "shared" / "RULES.md").read_text() == OLD


def test_the_cli_is_a_dry_run_unless_told_to_apply(tmp_path, capsys):
    data = data_dir(tmp_path)
    assert policy_pack.main(["install", "--data-dir", str(data)]) == 0
    assert "dry run" in capsys.readouterr().out and (data / "shared" / "RULES.md").read_text() == OLD
    assert policy_pack.main(["install", "--data-dir", str(data), "--apply"]) == 0
    assert "applied" in capsys.readouterr().out and "Cost-saving mode is OFF" in (data / "shared" / "RULES.md").read_text()


def test_an_install_makes_every_bots_next_turn_re_read_the_rules(tmp_path):
    """Fresh and resumed sessions are told to read shared-memory.md once its text changes (workspace.rules_note)."""
    data = data_dir(tmp_path)
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    before = (data / "shared" / "memory.md").read_text()
    save_rules_snapshot(before, bot_dir)
    assert rules_note(before, bot_dir) == ""
    policy_pack.install(data, apply=True)
    after = (data / "shared" / "memory.md").read_text()
    assert STANDING_RULES_NOTICE in rules_note(after, bot_dir)
