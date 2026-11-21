"""Install the repo's policy pack (policy-pack/RULES.md) into a data dir's shared standing rules.

    python -m botoffice.policy_pack install --data-dir data            # dry run
    python -m botoffice.policy_pack install --data-dir data --apply    # write, after a backup

Writes data/shared/RULES.md (the canonical copy) and a marked block in data/shared/memory.md (what every
job reads as shared-memory.md), under the shared-memory lock. Per-bot files are never touched. Nothing
runs at import or on its own: a deploy or the owner calls it.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import memory as memory_mod

PACK_DIR = Path(__file__).resolve().parents[2] / "policy-pack"
BEGIN = "<!-- bot-office policy-pack:begin sha256={sha} -->"
END = "<!-- bot-office policy-pack:end -->"
_BEGIN_PREFIX = "<!-- bot-office policy-pack:begin"


class PolicyPackError(Exception):
    pass


def load_pack(pack_dir: Path | None = None) -> str:
    path = (pack_dir or PACK_DIR) / "RULES.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyPackError(f"cannot read the policy pack at {path}: {exc.strerror or exc}") from exc
    if not text.strip():
        raise PolicyPackError(f"the policy pack at {path} is empty")
    return text


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def block(pack: str) -> str:
    return f"{BEGIN.format(sha=digest(pack))}\n{pack.strip()}\n{END}\n"


def _replace_block(memory_text: str, new_block: str) -> str | None:
    """*memory_text* with its marked block replaced, or None when it has none."""
    start = memory_text.find(_BEGIN_PREFIX)
    if start < 0:
        return None
    end = memory_text.find(END, start)
    if end < 0:
        return None
    return memory_text[:start] + new_block + memory_text[end + len(END):].lstrip("\n")


def merged_memory(memory_text: str, old_rules: str, pack: str) -> tuple[str, str]:
    """(new shared memory text, how): the marked block replaces an earlier block, else the old installed
    RULES text found inside the file, else goes on top (the old copy may then remain: *how* says so)."""
    new_block = block(pack)
    replaced = _replace_block(memory_text, new_block)
    if replaced is not None:
        return replaced, "replaced the managed block"
    old = old_rules.strip()
    if old and old in memory_text:
        return memory_text.replace(old, new_block.strip("\n"), 1).rstrip("\n") + "\n", "replaced the old installed rules"
    body = memory_text.lstrip("\n")
    how = "added the block on top of an empty shared memory" if not body.strip() else \
        "added the block on top; the old ported rules were not found verbatim, so review the rest of the file"
    return new_block + ("\n" + body if body else ""), how


def install(data_dir: Path, *, apply: bool = False, pack_dir: Path | None = None,
            now: datetime | None = None) -> dict:
    """The report of installing the pack into *data_dir* (nothing is written unless *apply*)."""
    pack = load_pack(pack_dir)
    shared = Path(data_dir) / "shared"
    rules_path, memory_path = shared / "RULES.md", shared / "memory.md"
    old_rules = _read(rules_path)
    old_memory = _read(memory_path)
    new_memory, how = merged_memory(old_memory, old_rules, pack)
    new_rules = pack if pack.endswith("\n") else pack + "\n"
    report = {"applied": False, "sha256": digest(pack), "rules": "unchanged" if old_rules == new_rules else "changed",
              "memory": "unchanged" if old_memory == new_memory else "changed", "memory_how": how, "backup": None}
    if report["rules"] == "unchanged" and report["memory"] == "unchanged":
        report["memory_how"] = "already installed"
        return report
    if not apply:
        return report
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    backup = Path(data_dir) / "backups" / f"policy-pack-{stamp}"
    mem = memory_mod.Memory(Path(data_dir))
    with mem._lock(shared):
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path, text in ((rules_path, old_rules), (memory_path, old_memory)):
            if text:
                memory_mod._atomic_write(backup / path.name, text)
        if report["rules"] == "changed":
            memory_mod._atomic_write(rules_path, new_rules)
        if report["memory"] == "changed":
            memory_mod._atomic_write(memory_path, new_memory)
    report.update(applied=True, backup=str(backup))
    return report


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise PolicyPackError(f"cannot read {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="botoffice.policy_pack", description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["install"])
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--pack-dir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="write the files (default: dry run)")
    args = parser.parse_args(argv)
    try:
        report = install(args.data_dir, apply=args.apply, pack_dir=args.pack_dir)
    except PolicyPackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(("applied" if report["applied"] else "dry run (nothing written; add --apply)")
          + f": RULES.md {report['rules']}, shared memory {report['memory']} ({report['memory_how']})"
          + (f"; backup in {report['backup']}" if report["backup"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
