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


def max_file_bytes() -> int:
    """Per-file upload limit: BOT_OFFICE_MAX_UPLOAD_MB (default 50)."""
    try:
        mb = float(os.environ.get("BOT_OFFICE_MAX_UPLOAD_MB", DEFAULT_MAX_FILE_MB))
    except ValueError:
        mb = DEFAULT_MAX_FILE_MB
    return int(max(mb, 0.001) * 1024 * 1024)


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


def collect_outgoing(data_dir: Path, bot_id, workspace: Path, audit: str, text: str,
                     referenced: dict[str, set[str]], source_bot_id=None) -> tuple[str, list[str]]:
    """Copy valid final-reply and sidecar images and remove successfully attached markup.

    The copies go into *bot_id*'s store. *source_bot_id* is the bot that wrote the reply when that
    is another bot (the bot-to-bot copy): its workspace and files are the readable roots."""
    directory(data_dir, bot_id)  # validate the bot directory before trusting it as a root
    directory(data_dir, source_bot_id or bot_id)
    roots = [workspace.resolve(), (data_dir / "bots" / str(source_bot_id or bot_id)).resolve()]
    matches = list(IMAGE_MARKDOWN.finditer(audit))
    paths = [m.group(2).removeprefix("<").removesuffix(">") for m in matches]
    try:
        _, sidecar = _read_local(SIDECAR, roots, MAX_SIDECAR_BYTES, truncate=True)
        paths.extend(sidecar.decode("utf-8", errors="replace").splitlines())
    except (OSError, ValueError):
        pass
    saved: set[Path] = set()
    ids: list[str] = []
    quota = False
    skipped = 0
    # candidates past the cap are never opened; remote URLs stay as the bot wrote them
    skipped += sum(1 for p in paths[MAX_CANDIDATES:] if not _is_remote(p))
    paths = paths[:MAX_CANDIDATES]
    for position, path in enumerate(paths):
        if len(ids) >= MAX_OUTGOING:
            skipped += sum(1 for p in paths[position:] if not _is_remote(p))
            break
        try:
            resolved, data = _read_local(path, roots, sniff=True)
            if resolved in saved:
                continue
            mime = sniff_image(data)
            if not mime:
                raise ValueError("not an image")
            name = save(data_dir, bot_id, data, mime, referenced)
            ids.append(name)
            referenced.setdefault(str(bot_id), set()).add(name)
            saved.add(resolved)
        except OverflowError:
            quota = True
        except (OSError, ValueError):
            if not _is_remote(path):
                skipped += 1

    def replace(match: re.Match) -> str:
        path = match.group(2).removeprefix("<").removesuffix(">")
        candidate = Path(os.path.abspath(roots[0] / path))
        return match.group(1) if candidate in saved else match.group(0)

    text = IMAGE_MARKDOWN.sub(replace, text)
    if quota:
        text += "\n\nAn image could not be attached because the image storage quota is full."
    if skipped:
        noun = "image" if skipped == 1 else "images"
        text += f"\n\n{skipped} {noun} could not be attached."
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


def prune(data_dir: Path, referenced: dict[str, set[str]]) -> None:
    root = data_dir / "bots"
    if not root.exists():
        return
    cutoff = time.time() - UNREFERENCED_TTL
    for bot in root.iterdir():
        try:
            path = directory(data_dir, bot.name)
            if not path.exists():
                continue
            for entry in path.iterdir():
                if ID_RE.fullmatch(entry.name) and entry.name not in referenced.get(bot.name, set()):
                    info = entry.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        entry.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
        try:
            files = uploads_directory(data_dir, bot.name)
            if files.exists():
                for entry in files.iterdir():
                    if FILE_ID_RE.fullmatch(entry.name) and entry.name not in referenced.get(bot.name, set()):
                        info = entry.lstat()
                        if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                            entry.unlink(missing_ok=True)
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
    if not stat.S_ISREG(info.st_mode) or info.st_size > (max_file_bytes() if is_file_id(attachment_id) else MAX_BYTES):
        raise ValueError("invalid attachment file")
    return info.st_size


def read(data_dir: Path, bot_id, attachment_id: str) -> bytes:
    if not is_valid_id(attachment_id):
        raise ValueError("invalid attachment id")
    path = folder_for(data_dir, bot_id, attachment_id) / attachment_id
    limit = max_file_bytes() if is_file_id(attachment_id) else MAX_BYTES
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
        f"Every image is kept permanently in {path}/ . Open files from here, never from an old job directory.",
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
