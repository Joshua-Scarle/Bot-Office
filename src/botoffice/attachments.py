"""Private, per-bot image attachment storage."""
from __future__ import annotations

import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from . import isolation
from .outgoing_images import SIDECAR

MAX_BYTES = 10 * 1024 * 1024
MAX_COUNT = 1000
MAX_OUTGOING = 6
MAX_SIDECAR_BYTES = 64 * 1024
MAX_CANDIDATES = 32
SNIFF_BYTES = 32
INDEX_FILE = "INDEX.md"
MAX_TOTAL = 500 * 1024 * 1024
GLOBAL_MAX_TOTAL = 2 * 1024 * 1024 * 1024
UNREFERENCED_TTL = 24 * 60 * 60
ID_RE = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{32}\.(?:png|jpg|gif|webp)\Z")
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

# 0.11.13: files of any type. They are stored apart from the images, in data/bots/<id>/uploads/, under a
# unique, sanitized name: <date>-<32 hex>-<name>. Never executable, never extracted, never served inline.
UPLOADS_DIR = "uploads"
FILE_ID_RE = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{32}-[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
DEFAULT_MAX_FILE_MB = 50
FILE_MAX_COUNT = 1000
FILE_MAX_TOTAL = 1024 * 1024 * 1024

# 0.12.4: how a stored file is served. Chosen from the stored extension only, never from what the bytes claim:
# a PDF and plain text may be previewed; everything else (html, svg, xml, scripts as pages, archives...) is a download.
TEXT_EXTENSIONS = frozenset(
    "txt md markdown csv tsv json jsonl log yaml yml toml ini cfg conf env diff patch py js ts sh sql rst tex".split())


def serve_type(name: str) -> tuple[str, str | None]:
    """(Content-Type, preview kind) for a stored file id: ``pdf`` or ``text`` may be shown inline, ``None`` never."""
    label = display_name(name)
    ext = label.rpartition(".")[2].lower() if "." in label else ""
    if ext == "pdf":
        return "application/pdf", "pdf"
    if ext in TEXT_EXTENSIONS:
        return "text/plain; charset=utf-8", "text"
    return "application/octet-stream", None


def max_file_bytes() -> int:
    """Per-file upload limit: BOT_OFFICE_MAX_UPLOAD_MB (default 50)."""
    try:
        mb = float(os.environ.get("BOT_OFFICE_MAX_UPLOAD_MB", DEFAULT_MAX_FILE_MB))
    except ValueError:
        mb = DEFAULT_MAX_FILE_MB
    return int(max(mb, 0.001) * 1024 * 1024)


def stored_limit(name: str) -> int:
    """The largest stored file that is still read. The upload limit applies when a file arrives; lowering it
    later must not make files already sent unreadable, so reads allow the larger of it and the default."""
    return max(max_file_bytes(), DEFAULT_MAX_FILE_MB * 1024 * 1024) if is_file_id(name) else MAX_BYTES


def safe_name(raw: str) -> str:
    """A file name that is safe on disk: no directory part, no path tricks, plain characters only."""
    name = re.split(r"[\\/]", raw.replace("\x00", ""))[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    if not name:
        return "file"
    if len(name) > 100:
        stem, dot, ext = name.rpartition(".")
        ext = ext[:12] if dot else ""
        name = (stem[: 99 - len(ext)] + "." + ext) if ext else name[:100]
        name = name[:100].strip("._-") or "file"
    return name


def display_name(name: str) -> str:
    """The file's own (sanitized) name inside a file id; an image id is returned as is."""
    return name[len("0000-00-00-") + 33:] if is_file_id(name) else name


def is_file_id(name: str) -> bool:
    return bool(FILE_ID_RE.fullmatch(name))


def is_valid_id(name: str) -> bool:
    return bool(ID_RE.fullmatch(name) or FILE_ID_RE.fullmatch(name))


def uploads_directory(data_dir: Path, bot_id, create: bool = False) -> Path:
    root = data_dir / "bots"
    workspace = root / str(bot_id)
    path = workspace / UPLOADS_DIR
    for part in (root, workspace, path):
        if create:
            part.mkdir(mode=isolation.dir_mode(), parents=True, exist_ok=True)
        try:
            info = part.lstat()
        except FileNotFoundError:
            if create:
                raise
            break
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("invalid upload directory")
    return path


def folder_for(data_dir: Path, bot_id, name: str, create: bool = False) -> Path:
    """The directory holding the stored image or file called *name*."""
    return uploads_directory(data_dir, bot_id, create) if is_file_id(name) else directory(data_dir, bot_id, create)


def save_file(data_dir: Path, bot_id, data: bytes, raw_name: str,
              referenced: dict[str, set[str]] | None = None) -> str:
    """Store an uploaded file of any type as uploads/<date>-<uuid>-<safe name>, mode 0400, never overwriting."""
    if len(data) > max_file_bytes():
        raise ValueError("file too large")
    prune(data_dir, referenced or {})
    path = uploads_directory(data_dir, bot_id, create=True)
    entries = [p for p in path.iterdir() if FILE_ID_RE.fullmatch(p.name)]
    if len(entries) >= FILE_MAX_COUNT or sum(p.lstat().st_size for p in entries) + len(data) > FILE_MAX_TOTAL:
        raise OverflowError(
            f"This bot's file storage is full ({FILE_MAX_COUNT} files or {FILE_MAX_TOTAL // (1024 * 1024)} MiB). "
            "Files sent in a message are kept; remove unsent uploads or ask for old files to be deleted."
        )
    name = f"{datetime.now(UTC):%Y-%m-%d}-{uuid4().hex}-{safe_name(raw_name)}"
    fd = os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o440 if isolation.active() else 0o400)  # readable, never executable
    except BaseException:
        (path / name).unlink(missing_ok=True)
        raise
    return name
IMAGE_SIGNATURES: tuple[tuple[str, bytes, int], ...] = (
    ("image/png", b"\x89PNG\r\n\x1a\n", 0),
    ("image/jpeg", b"\xff\xd8\xff", 0),
    ("image/gif", b"GIF87a", 0),
    ("image/gif", b"GIF89a", 0),
    ("image/webp", b"WEBP", 8),
)


def sniff_image(data: bytes) -> str | None:
    for mime, magic, offset in IMAGE_SIGNATURES:
        if data[offset : offset + len(magic)] == magic:
            if mime == "image/webp" and not data.startswith(b"RIFF"):
                continue
            return mime
    return None


# A destination may contain a single level of parentheses, or be enclosed in angle brackets.
IMAGE_MARKDOWN = re.compile(
    r'!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|(?:[^\s()\\]|\\.|\([^()\n]*\))+)'
    r'(?:\s+"[^"\n]*")?\s*\)'
)


def _read_local(path: str, roots: list[Path], limit: int = MAX_BYTES, *,
                sniff: bool = False, truncate: bool = False) -> tuple[Path, bytes]:
    """Read a regular file beneath a trusted root without following any symlinks.

    ``sniff`` rejects a non-image after its first bytes, before the rest is read;
    ``truncate`` reads at most ``limit`` bytes of a larger file instead of refusing it."""
    if not path or urlsplit(path).scheme or path.startswith("//") or "\x00" in path:
        raise ValueError("not a local path")
    candidate = Path(os.path.abspath(roots[0] / path))
    for root in roots:
        root = root.resolve()
        try:
            parts = candidate.relative_to(root).parts
        except ValueError:
            continue
        if not parts or parts[-1] == INDEX_FILE:
            raise ValueError("invalid image path")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            image_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(image_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size:
                    raise ValueError("invalid image file")
                if info.st_size > limit and not truncate:
                    raise ValueError("invalid image file")
                head = stream.read(SNIFF_BYTES)
                if sniff and not sniff_image(head):
                    raise ValueError("not an image")
                data = head + stream.read(max(limit - len(head), 0) + (0 if truncate else 1))
                if not data or (len(data) > limit and not truncate):
                    raise ValueError("invalid image size")
                return candidate, data
        finally:
            os.close(fd)
    raise ValueError("image outside allowed roots")


def _is_remote(path: str) -> bool:
    return bool(urlsplit(path).scheme) or path.startswith("//")


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(root == path or root in path.parents for root in roots)


def collect_outgoing(data_dir: Path, bot_id, workspace: Path, audit: str, text: str,
                     referenced: dict[str, set[str]], source_bot_id=None) -> tuple[str, list[str]]:
    """Copy valid final-reply and sidecar attachments and remove successfully attached image markup.

    ``![](path)`` in the reply is image-only. A sidecar entry (``bot-office-attach``) may be any file: an image
    goes to the image store, anything else to the file store (0.12.4). The copies go into *bot_id*'s store.
    *source_bot_id* is the bot that wrote the reply when that is another bot (the bot-to-bot copy): its
    workspace and files are the readable roots. A non-image file must lie in the job workspace or in the
    bot's attachments/ or uploads/ folder, never elsewhere in the bot directory."""
    directory(data_dir, bot_id)  # validate the bot directory before trusting it as a root
    directory(data_dir, source_bot_id or bot_id)
    bot_root = data_dir / "bots" / str(source_bot_id or bot_id)
    roots = [workspace.resolve(), bot_root.resolve()]
    file_roots = [roots[0], (bot_root / "attachments").resolve(), (bot_root / UPLOADS_DIR).resolve()]
    matches = list(IMAGE_MARKDOWN.finditer(audit))
    # (path, from the sidecar): only a sidecar entry may be a non-image file
    paths: list[tuple[str, bool]] = [(m.group(2).removeprefix("<").removesuffix(">"), False) for m in matches]
    try:
        _, sidecar = _read_local(SIDECAR, roots, MAX_SIDECAR_BYTES, truncate=True)
        paths.extend((line, True) for line in sidecar.decode("utf-8", errors="replace").splitlines() if line.strip())
    except (OSError, ValueError):
        pass
    saved: set[Path] = set()
    ids: list[str] = []
    quota = {False: False, True: False}  # by origin: reply markup (images) / sidecar (files)
    skipped = {False: 0, True: 0}
    # candidates past the cap are never opened; remote URLs stay as the bot wrote them
    for path, from_sidecar in paths[MAX_CANDIDATES:]:
        skipped[from_sidecar] += not _is_remote(path)
    paths = paths[:MAX_CANDIDATES]
    for position, (path, from_sidecar) in enumerate(paths):
        if len(ids) >= MAX_OUTGOING:
            for rest, rest_sidecar in paths[position:]:
                skipped[rest_sidecar] += not _is_remote(rest)
            break
        try:
            if from_sidecar:
                resolved, data = _read_local(path, roots, max_file_bytes())
            else:
                resolved, data = _read_local(path, roots, sniff=True)
            if resolved in saved:
                continue
            mime = sniff_image(data)
            if mime and len(data) <= MAX_BYTES:
                name = save(data_dir, bot_id, data, mime, referenced)
            elif from_sidecar and _inside(resolved, file_roots):
                name = save_file(data_dir, bot_id, data, resolved.name, referenced)
            else:
                raise ValueError("not an image")
            ids.append(name)
            referenced.setdefault(str(bot_id), set()).add(name)
            saved.add(resolved)
        except OverflowError:
            quota[from_sidecar] = True
        except (OSError, ValueError):
            if not _is_remote(path):
                skipped[from_sidecar] += 1

    def replace(match: re.Match) -> str:
        path = match.group(2).removeprefix("<").removesuffix(">")
        candidate = Path(os.path.abspath(roots[0] / path))
        return match.group(1) if candidate in saved else match.group(0)

    text = IMAGE_MARKDOWN.sub(replace, text)
    if quota[False]:
        text += "\n\nAn image could not be attached because the image storage quota is full."
    if quota[True]:
        text += "\n\nA file could not be attached because the storage quota is full."
    for from_sidecar, kind in ((False, "image"), (True, "file")):
        count = skipped[from_sidecar]
        if count:
            text += f"\n\n{count} {kind if count == 1 else kind + 's'} could not be attached."
    return text, ids


def directory(data_dir: Path, bot_id, create: bool = False) -> Path:
    root = data_dir / "bots"
    workspace = root / str(bot_id)
    path = workspace / "attachments"
    for part in (root, workspace, path):
        if create:
            part.mkdir(mode=isolation.dir_mode(), parents=True, exist_ok=True)
        try:
            info = part.lstat()
        except FileNotFoundError:
            if create:
                raise
            break
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("invalid attachment directory")
    return path


def _prune_folder(folder: Path, pattern: re.Pattern, kept: set[str], cutoff: float) -> None:
    if not folder.exists():
        return
    for entry in folder.iterdir():
        if pattern.fullmatch(entry.name) and entry.name not in kept:
            info = entry.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                entry.unlink(missing_ok=True)


def prune(data_dir: Path, referenced: dict[str, set[str]]) -> None:
    """Remove unsent uploads (images and files) older than a day; anything a message references stays."""
    root = data_dir / "bots"
    if not root.exists():
        return
    cutoff = time.time() - UNREFERENCED_TTL
    for bot in root.iterdir():
        kept = referenced.get(bot.name, set())
        for folder_of, pattern in ((directory, ID_RE), (uploads_directory, FILE_ID_RE)):
            try:  # a bot may have only one of the two folders
                _prune_folder(folder_of(data_dir, bot.name), pattern, kept, cutoff)
            except (OSError, ValueError):
                continue


def delete(data_dir: Path, bot_id, attachment_id: str) -> None:
    if not is_valid_id(attachment_id):
        raise ValueError("invalid attachment id")
    path = folder_for(data_dir, bot_id, attachment_id) / attachment_id
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("invalid attachment file")
    path.unlink()


def size(data_dir: Path, bot_id, attachment_id: str) -> int:
    """Size of a stored attachment; raises ValueError/OSError if it is invalid or missing."""
    if not is_valid_id(attachment_id):
        raise ValueError("invalid attachment id")
    info = (folder_for(data_dir, bot_id, attachment_id) / attachment_id).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > stored_limit(attachment_id):
        raise ValueError("invalid attachment file")
    return info.st_size


def read(data_dir: Path, bot_id, attachment_id: str) -> bytes:
    if not is_valid_id(attachment_id):
        raise ValueError("invalid attachment id")
    path = folder_for(data_dir, bot_id, attachment_id) / attachment_id
    limit = stored_limit(attachment_id)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid attachment file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return stream.read(limit + 1)
    finally:
        if fd != -1:
            os.close(fd)


def save(data_dir: Path, bot_id, data: bytes, mime: str, referenced: dict[str, set[str]] | None = None) -> str:
    prune(data_dir, referenced or {})
    path = directory(data_dir, bot_id, create=True)
    # only stored images count (INDEX.md does not); sent images are never deleted to make room
    entries = [p for p in path.iterdir() if ID_RE.fullmatch(p.name)]
    if len(entries) >= MAX_COUNT or sum(p.lstat().st_size for p in entries) + len(data) > MAX_TOTAL:
        raise OverflowError(
            f"This bot's image storage is full ({MAX_COUNT} images or {MAX_TOTAL // (1024 * 1024)} MiB). "
            "Sent images are kept permanently and are never deleted automatically, so this upload was refused. "
            "Remove unsent uploads, or delete images you no longer need, then try again."
        )
    total = 0
    for bot in (data_dir / "bots").iterdir():
        try:
            other = directory(data_dir, bot.name)
            if other.exists():
                total += sum(p.lstat().st_size for p in other.iterdir() if stat.S_ISREG(p.lstat().st_mode))
        except (OSError, ValueError):
            continue
    if total + len(data) > GLOBAL_MAX_TOTAL:
        raise OverflowError(
            f"Bot-Office image storage is full ({GLOBAL_MAX_TOTAL // (1024 ** 3)} GiB across all bots). "
            "Sent images are never deleted automatically, so this upload was refused."
        )
    name = f"{datetime.now(UTC):%Y-%m-%d}-{uuid4().hex}.{EXTENSIONS[mime]}"
    fd = os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o440 if isolation.active() else 0o400)  # bots read it
    except BaseException:
        (path / name).unlink(missing_ok=True)
        raise
    return name


def write_index(data_dir: Path, bot_id, rows: list[tuple]) -> Path:
    """Write ``attachments/INDEX.md`` (date, message id, file, message excerpt) for the bot."""
    path = directory(data_dir, bot_id, create=True)
    lines = [
        "# Images and files sent to and from this bot",
        "",
        f"Every image is kept permanently in {path}/ and every other uploaded file in {path.parent / UPLOADS_DIR}/ . Open files from there, never from an old job directory.",
        "Newest last. Columns: date (UTC) | message id | direction | file | message text.",
        "",
    ]
    for row in rows:
        stamp, message_id, name, text = row[:4]
        direction = row[4] if len(row) > 4 else "from user"
        excerpt = " ".join(text.split())[:100].replace("|", "/")
        where = uploads_directory(data_dir, bot_id) if is_file_id(name) else path
        lines.append(f"- {stamp} | message {message_id} | {direction} | {where / name} | {excerpt}")
    target = path / INDEX_FILE
    tmp = path / f".{INDEX_FILE}.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, isolation.locked_file_mode())  # the fd, not the name: a bot could swap the name
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    os.replace(tmp, target)
    return target
