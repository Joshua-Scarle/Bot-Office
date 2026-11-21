"""The command half of this computer's deny-list: checked on every ``run`` (argv or sh string)
before Bot Office asks anyone and again right before the command starts. It wins over any grant.

Refused:
- catastrophic commands: a recursive ``rm`` of ``/``, your home folder or a folder above it
  (``rm -rf /``, ``rm -fr ~/``, ``rm -rf --no-preserve-root /``, ``rm -r "$HOME"``, ``rm -rf /*``);
  ``mkfs*``, ``mke2fs``, ``wipefs``; ``dd of=`` a disk; any write or redirect to a disk device
  (``/dev/sd*``, ``/dev/nvme*``, ...); ``shutdown``, ``reboot``, ``poweroff``, ``halt``,
  ``init 0|6``, ``systemctl poweroff|reboot|halt``; a fork bomb; a recursive ``chmod``/``chown``/
  ``chgrp`` of ``/`` or your home folder; decoded text piped into a shell;
- any word of the command that names a deny-listed path (``~/.ssh``, browser profiles, ...);
- the ``commands`` regular expressions in deny.toml.

It is conservative: wrappers (``sudo``, ``env``, ``timeout``, ``xargs``...) are looked through,
``sh -c``/``bash -c``/``eval`` strings are checked too, a recursive ``rm``/``chmod`` whose target is
a variable it cannot resolve is refused, and a string it cannot parse with confidence (unbalanced
quotes, ``$(...)``, backticks, ``eval``, ``${...}``, a variable as the command) is refused when it
contains a catastrophic word anywhere. It is still a tripwire, not a sandbox: a command can write a
script and run it, or use an interpreter. The sandbox switch (bwrap: read-only view, deny-listed
paths hidden, no network) is the real boundary for ``run``.
"""
from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import shlex

from .paths import LocalDenied, under

SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "fish", "csh", "tcsh", "ash", "busybox"})
# words that run the rest of the line as a command (their options are skipped)
WRAPPERS = frozenset({"sudo", "doas", "pkexec", "env", "nice", "nohup", "timeout", "exec", "command", "builtin",
                      "time", "stdbuf", "ionice", "chrt", "setsid", "xargs", "strace", "ltrace", "unbuffer",
                      "taskset", "flock", "watch", "busybox", "run0", "systemd-run", "then", "do", "else",
                      "elif", "if", "while", "until", "!", "{", "}", "(", ")"})
# options of wrappers that take a value (skipped with it)
WRAPPER_VALUE_OPTIONS = frozenset({"-u", "-g", "-n", "-s", "-k", "-c", "-p", "-C", "-o", "-e", "-I", "-L", "-P",
                                   "-d", "-a", "--user", "--group", "--signal", "--kill-after", "--chdir"})
SEPARATORS = frozenset({";", "&", "&&", "|", "||", "|&", "(", ")", ";;", "{", "}", "!"})
POWER = frozenset({"shutdown", "reboot", "poweroff", "halt"})
POWER_VERBS = frozenset({"poweroff", "reboot", "halt", "kexec", "soft-reboot", "suspend-then-hibernate"})
FORMAT = re.compile(r"^(mkfs(\..*)?|mke2fs|mkfs\w*|wipefs|blkdiscard)$")
DISK = re.compile(r"^/dev/(sd[a-z]|nvme\d|hd[a-z]|vd[a-z]|xvd[a-z]|mmcblk\d|dm-\d|md\d|mapper/|disk/|loop\d|sr\d)")
DISK_WRITERS = frozenset({"tee", "cp", "mv", "install", "shred", "truncate", "sfdisk", "fdisk", "parted", "sgdisk",
                          "cat", "pv", "ddrescue", "badblocks"})
RECURSIVE_OWNERS = frozenset({"chmod", "chown", "chgrp", "setfacl"})
DECODERS = re.compile(r"\b(base64\s+(-d|--decode)|xxd\s+-r|openssl\s+(enc|base64)\b[^|;]*-d|rev\b|uudecode)")
DYNAMIC = re.compile(r"\$\(|`|\$\{|\$'|\beval\b|<\(|>\(")
# catastrophic words, for strings that cannot be parsed with confidence
RAW_CATASTROPHIC = re.compile(
    r"\brm\b[^;|&\n]*\s-(\w*[rR]\w*|-r[a-z]*)\b|\brm\b[^;|&\n]*\s--no?\b|--no-p|\bmkfs|\bmke2fs\b|\bwipefs\b"
    r"|\bdd\b[^;|&\n]*\bof=|\b(shutdown|reboot|poweroff|halt)\b|\bsystemctl\b[^;|&\n]*\b(poweroff|reboot|halt|kexec)\b"
    r"|\b(chmod|chown|chgrp)\b[^;|&\n]*\s-(\w*R\w*|-rec[a-z]*)\b|/dev/(sd|nvme|hd|vd|xvd|mmcblk|dm-|md|mapper/|disk/)"
    r"|:\s*\(\s*\)\s*\{")
REDIRECT_TO_DISK = re.compile(r">\s*&?\s*/dev/+(sd|nvme|hd|vd|xvd|mmcblk|dm-|md|mapper/|disk/)")
FORK_BOMB = re.compile(r"([^\s(){};|&]+)\(\)\{[^}]*\1\|\1|function([^\s(){};|&]+)\{[^}]*\2\|\2")
GLOB = re.compile(r"[*?\[]")
NESTING_MAX = 4
# what the shell itself would turn into other words: $IFS (a space), other variables (maybe empty)
# and brace lists ({rm,-rf,/} is "rm -rf /"); the string is checked again with them spelled out
IFS_WORD = re.compile(r"\$\{IFS\}|\$IFS(?![A-Za-z0-9_])")
VARIABLE = re.compile(r"\$(\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])")
BRACE_LIST = re.compile(r"\{([^{}\s]*,[^{}\s]*)\}")


def _option(arg: str, *names: str) -> bool:
    """*arg* is one of the long options *names*, spelled out or abbreviated (GNU accepts any
    unambiguous prefix, so "--rec" is "--recursive")."""
    word = arg.split("=", 1)[0]
    return len(word) > 2 and word.startswith("--") and any(name.startswith(word) for name in names)


class Checker:
    """One computer's command deny-list (home folder, deny-listed path roots, extra patterns)."""

    def __init__(self, home: str, roots: list[str], patterns: list[str] | tuple[str, ...] = ()):
        self.home = os.path.normpath(home)
        self.roots = roots
        self.patterns = [re.compile(item) for item in patterns]
        # the spellings a command may use for a deny-listed path under the home folder: ".ssh", ...
        self.relative = sorted({os.path.relpath(root, self.home) for root in roots
                                if under(root, self.home) and root != self.home}, key=len, reverse=True)
        self.absolute = sorted({root for root in roots if not under(root, self.home)}, key=len, reverse=True)

    # --- entry point ---

    def check(self, argv: list[str] | None, sh: str | None, cwd: str) -> None:
        """LocalDenied if the command is on the deny-list."""
        if argv is not None:
            text = shlex.join(argv)
            self._patterns(text)
            self._raw_paths(" ".join(argv))
            self._simple(list(argv), (cwd,), depth=0, literal=True)
        elif isinstance(sh, str):
            self._patterns(sh)
            # a string that changes folder may mean its relative paths from anywhere: home and / too
            moves = re.search(r"(^|[\s;&|(`])(cd|pushd|popd)(\s|$|[;&|)])", sh) is not None
            self._script(sh, (cwd, self.home, "/") if moves else (cwd,), depth=0)
        else:
            raise LocalDenied("no command")

    # --- helpers ---

    def _deny(self, why: str) -> None:
        raise LocalDenied(f"refused by this computer's command deny-list: {why}")

    def _patterns(self, text: str) -> None:
        for pattern in self.patterns:
            if pattern.search(text):
                self._deny(f"matches deny.toml pattern {pattern.pattern!r}")

    def _raw_paths(self, text: str) -> None:
        """A deny-listed path spelled anywhere in the text, quotes and backslashes removed."""
        flat = re.sub(r"[\"'\\]", "", text)
        for rel in self.relative:
            if re.search(r"(^|[\s/=:~\"'(<>|;&])" + re.escape(rel) + r"($|[\s/\"');|&<>])", flat):
                self._deny(f"names a deny-listed path (~/{rel})")
        for root in self.absolute:
            if re.search(re.escape(root) + r"($|[\s/\"');|&<>])", flat):
                self._deny(f"names a deny-listed path ({root})")

    def _expand(self, word: str, literal: bool) -> tuple[str, bool]:
        """(*word* with ~ and $HOME expanded, whether something else stays unresolved)."""
        value = word
        if value == "~" or value.startswith("~/"):
            value = self.home + value[1:]
        elif value.startswith("~") and not literal:
            user = value[1:].split("/", 1)[0]
            expanded = os.path.expanduser("~" + user)
            value = expanded + value[1 + len(user):] if not expanded.startswith("~") else value
        value = re.sub(r"\$\{HOME\}|\$HOME\b", self.home, value)
        return value, ("$" in value or "`" in value) and not literal

    def _absolute(self, path: str, bases: tuple[str, ...]) -> list[str]:
        """The places *path* may mean: from the working folder (or every folder a cd may reach)."""
        out = []
        for base in ([""] if path.startswith("/") else bases):
            full = posixpath.normpath(posixpath.join(base, path)) if base else posixpath.normpath(path)
            if full.startswith("//"):
                full = "/" + full.lstrip("/")
            out.append(full)
            try:
                out.append(os.path.realpath(full))
            except (OSError, ValueError):
                pass
        return out

    def _glob_hits(self, pattern: str, target: str) -> bool:
        """Could the shell glob *pattern* (absolute) name *target* or a path inside it?"""
        want = [part for part in pattern.split("/") if part]
        have = [part for part in target.split("/") if part]
        for index, part in enumerate(have):
            if index >= len(want):
                return True  # the glob names a folder above the target (and maybe everything in it)
            if want[index] == "**":
                return True
            if part.startswith(".") and not want[index].startswith(".") and GLOB.search(want[index]):
                return False  # * does not match a leading dot
            if not fnmatch.fnmatchcase(part, want[index]):
                return False
        return True

    def _word_paths(self, word: str, bases: tuple[str, ...], literal: bool) -> None:
        """Every path-like piece of one word (also after = and :) checked against the deny-list."""
        pieces = {word, *re.split(r"[=:,]", word)}
        for piece in pieces:
            if not piece or not ("/" in piece or piece.startswith(("~", ".")) or "$HOME" in piece
                                 or "${HOME}" in piece):
                continue
            value, _ = self._expand(piece, literal)
            for full in self._absolute(value, bases):
                if GLOB.search(full) and not literal:
                    for root in self.roots:
                        if self._glob_hits(full, root):
                            self._deny(f"{piece!r} can match the deny-listed path {root}")
                    continue
                for root in self.roots:
                    if under(full, root):
                        self._deny(f"{piece!r} is the deny-listed path {root}")

    def _danger_target(self, word: str, bases: tuple[str, ...], literal: bool) -> str | None:
        """Why removing or re-owning *word* recursively is catastrophic, or None."""
        value, unresolved = self._expand(word, literal)
        if unresolved:
            return f"{word!r} cannot be resolved here"
        tops = {"/", self.home}
        parent = self.home
        while parent != "/":
            parent = posixpath.dirname(parent)
            tops.add(parent)
        if GLOB.search(value) and not literal:
            fixed = value.split("/")
            prefix = []
            for part in fixed:
                if GLOB.search(part):
                    break
                prefix.append(part)
            base = "/".join(prefix) or ("/" if value.startswith("/") else ".")
            for full in self._absolute(base, bases):
                if full in tops:
                    return f"{word!r} covers {full}"
            return None
        for full in self._absolute(value, bases):
            if full in tops:
                return f"{word!r} is {full}"
        return None

    # --- shell strings ---

    def _script(self, text: str, bases: tuple[str, ...], depth: int) -> None:
        if depth > NESTING_MAX:
            self._deny("too deeply nested shell strings")
        text = text.replace("\\\n", "")  # a line continuation joins the two lines
        spelled = re.sub(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])", self.home, text)
        spelled = VARIABLE.sub("", IFS_WORD.sub(" ", spelled))
        spelled = BRACE_LIST.sub(lambda match: " ".join(match.group(1).split(",")), spelled)
        if spelled != text:
            self._script(spelled, bases, depth + 1)
        self._raw_paths(text)
        compact = re.sub(r"\s+", "", text)
        if FORK_BOMB.search(compact) or ":(){" in compact:
            self._deny("a fork bomb")
        flat = re.sub(r"[\"'\\]", "", text)
        if REDIRECT_TO_DISK.search(flat):
            self._deny("writes to a disk device")
        dynamic = bool(DYNAMIC.search(text))
        if DECODERS.search(text) and re.search(r"\|\s*(sudo\s+)?(\S*/)?(ba|da|z|k|c|tc|fi|mk)?sh\b|\beval\b|\bsource\b",
                                               text):
            self._deny("decoded text is run as a command")
        # newlines and command substitutions are command boundaries for this check
        prepared = re.sub(r"\$\(|`|<\(|>\(", " ; ", text.replace("\n", " ; "))
        try:
            lexer = shlex.shlex(prepared, posix=True, punctuation_chars=";&|()<>")
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:
            if RAW_CATASTROPHIC.search(flat):
                self._deny("a command it cannot parse contains a catastrophic word")
            return
        commands: list[list[str]] = [[]]
        redirect_next = False
        for token in tokens:
            if redirect_next:
                redirect_next = False
                target, _ = self._expand(token, False)
                if DISK.match(posixpath.normpath(target)):
                    self._deny("writes to a disk device")
                commands[-1].append(token)
                continue
            if token in SEPARATORS or set(token) <= set(";&|()"):
                commands.append([])
                continue
            if set(token) <= set("<>&|0123456789") and ">" in token:
                redirect_next = True
                continue
            if token in ("<", "<<", "<<<", "<&", "<>"):
                continue
            commands[-1].append(token)
        for words in commands:
            if words:
                if words[0].startswith("$") and RAW_CATASTROPHIC.search(flat):
                    self._deny("a variable is run as a command next to a catastrophic word")
                self._simple(words, bases, depth, literal=False)
        if dynamic and RAW_CATASTROPHIC.search(flat):
            self._deny("a command with $(...), backticks, ${...} or eval contains a catastrophic word")

    # --- one simple command ---

    def _command_positions(self, words: list[str]) -> list[int]:
        """Indexes of the words that run as a command: the first, and after each wrapper."""
        positions, index = [], 0
        while index < len(words):
            word = words[index]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
                index += 1
                continue
            positions.append(index)
            if posixpath.basename(word) not in WRAPPERS:
                break
            index += 1
            while index < len(words):
                option = words[index]
                if option in WRAPPER_VALUE_OPTIONS:
                    index += 2
                elif option.startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", option) \
                        or re.fullmatch(r"[0-9][0-9.]*[smhd]?|0x[0-9a-fA-F]+", option):
                    index += 1
                else:
                    break
        return positions

    def _simple(self, words: list[str], bases: tuple[str, ...], depth: int, literal: bool) -> None:
        for word in words:
            self._word_paths(word, bases, literal)
            if word.startswith("of=") and DISK.match(posixpath.normpath(self._expand(word[3:], literal)[0])):
                self._deny("dd writes to a disk device")
        names = [posixpath.basename(word) for word in words]
        for position in self._command_positions(words):
            name, rest = names[position], words[position + 1:]
            if words[position].startswith("$") and not literal:  # could be any program: judge it as rm and chmod
                self._rm(rest, bases, literal)
                for owner in ("chmod", "chown"):
                    self._recursive_owner(owner, rest, bases, literal)
            if name in POWER:
                self._deny(f"{name} stops this computer")
            if name in ("init", "telinit") and rest[:1] and rest[0] in ("0", "6"):
                self._deny(f"{name} {rest[0]} stops this computer")
            if FORMAT.match(name):
                self._deny(f"{name} formats a disk")
            if name in DISK_WRITERS and any(DISK.match(posixpath.normpath(self._expand(arg, literal)[0]))
                                            for arg in rest if not arg.startswith("-")):
                self._deny(f"{name} writes to a disk device")
            if name in SHELLS or name in ("eval",):
                self._nested(name, rest, bases, depth)
            if name == "alias":  # alias x='rm -rf /'; x
                for arg in rest:
                    if "=" in arg:
                        self._script(arg.split("=", 1)[1], bases, depth + 1)
            if name in ("find",):
                self._find(rest, bases, literal)
        # anywhere in the words (also as an argument of something we do not know): these are
        # specific enough not to be ordinary arguments
        for index, name in enumerate(names):
            rest = words[index + 1:]
            if name == "rm":
                self._rm(rest, bases, literal)
            elif name in RECURSIVE_OWNERS:
                self._recursive_owner(name, rest, bases, literal)
            elif name in ("systemctl", "loginctl") and any(arg in POWER_VERBS for arg in rest):
                self._deny(f"{name} stops this computer")

    def _nested(self, name: str, rest: list[str], bases: tuple[str, ...], depth: int) -> None:
        if name == "eval":
            self._script(" ".join(rest), bases, depth + 1)
            return
        for index, arg in enumerate(rest):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", arg) and index + 1 < len(rest):
                self._script(rest[index + 1], bases, depth + 1)
                return
            if name == "busybox" and not arg.startswith("-"):
                return

    def _rm(self, rest: list[str], bases: tuple[str, ...], literal: bool) -> None:
        options = [arg for arg in rest if arg.startswith("-") and arg != "--"]
        if any(_option(arg, "--no-preserve-root") for arg in options):
            self._deny("rm --no-preserve-root")
        recursive = any(_option(arg, "--recursive") or re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*", arg)
                        for arg in options)
        if not recursive:
            return
        after_dashes = False
        for arg in rest:
            if arg == "--" and not after_dashes:
                after_dashes = True
                continue
            if arg.startswith("-") and not after_dashes:
                continue
            why = self._danger_target(arg, bases, literal)
            if why:
                self._deny(f"recursive rm of {why}")

    def _recursive_owner(self, name: str, rest: list[str], bases: tuple[str, ...], literal: bool) -> None:
        if not any(_option(arg, "--recursive") or re.fullmatch(r"-[A-Za-z]*R[A-Za-z]*", arg) for arg in rest):
            return
        operands = [arg for arg in rest if not arg.startswith("-")]
        for arg in operands[1:] if name != "setfacl" else operands:
            why = self._danger_target(arg, bases, literal)
            if why:
                self._deny(f"recursive {name} of {why}")

    def _find(self, rest: list[str], bases: tuple[str, ...], literal: bool) -> None:
        destructive = "-delete" in rest or any(
            arg in ("-exec", "-execdir", "-ok", "-okdir") and index + 1 < len(rest)
            and posixpath.basename(rest[index + 1]) in ("rm", "shred", "chmod", "chown", "unlink")
            for index, arg in enumerate(rest))
        if not destructive:
            return
        for arg in rest:
            if arg.startswith("-") or arg in ("(", ")", "!"):
                break
            why = self._danger_target(arg, bases, literal)
            if why:
                self._deny(f"find deleting under {why}")
