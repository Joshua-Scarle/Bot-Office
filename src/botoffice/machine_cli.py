"""``machine``: a bot's helper for the owner's paired computers (bin/machine).

Access is a session grant for this task (this job): the first call (or ``machine request --scope``)
asks the owner ONCE with one card (bot, computer, task, scope, how long) and blocks (like
``desk ask``) until the owner decides in the chat or Needs-you, or the card times out, which is a
deny. After "Allow for this task" every call within that scope runs without another card until the
task ends, the owner ends the grant, or the grant's time cap is reached. A call that needs a wider
scope (read < files < shell) raises one widen card. Output from the computer is data, wrapped in
<untrusted-data>. Files come into and go out of this job workspace only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

USAGE = """usage: machine [--machine NAME] COMMAND ...
  machine list                                   paired computers (no approval)
  machine request --scope read|files|shell       ask for this task's access up front (one card)
  machine ls PATH [--depth 1-3]                  list a folder
  machine read PATH [--offset N] [--length N] [--b64]   read up to 1 MiB of a file
  machine run [--cwd DIR] [--timeout S] -- PROGRAM ARG...   run a command (no shell)
  machine run --sh "COMMAND" [--cwd DIR] [--timeout S]      run a shell string
  machine pull REMOTE_PATH LOCAL_DEST [--overwrite]   copy a file here (this workspace)
  machine push LOCAL_FILE REMOTE_PATH [--overwrite]   copy a file from this workspace there
  machine shot LOCAL_DEST                         screenshot into this workspace
Scopes: read = ls, read, pull, shot; files = read + push; shell = files + run.
The first call of a task waits for the owner's one approval card for this task; later calls in
that scope run at once. No answer in time means no. Exit 3: denied, expired or the grant ended."""
TRANSFER_MAX = 100 * 1024 * 1024
POLL_SECONDS = 1.0
EXIT_DENIED = 3


class HelperError(Exception):
    pass


class Client:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + token}

    def call(self, method: str, path: str, data: object = None, *, raw: bytes | None = None,
             timeout: float = 30.0, binary: bool = False):
        headers = dict(self.headers)
        body = None
        if raw is not None:
            body, headers["Content-Type"] = raw, "application/octet-stream"
        elif data is not None:
            body, headers["Content-Type"] = json.dumps(data).encode(), "application/json"
        request = urllib.request.Request(self.url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if binary:
                    return response.read(TRANSFER_MAX + 1), dict(response.headers)
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("detail")
            except (ValueError, AttributeError):
                detail = None
            raise HelperError(f"{exc.code}: {detail or exc.reason}") from None


def _workspace() -> Path:
    from .outgoing_images import find_workspace, job_cwd
    try:
        return Path(os.path.realpath(find_workspace(job_cwd(), Path.cwd())))
    except ValueError as exc:
        raise HelperError(f"{exc}: files go in and out of a job workspace only") from None


def _inside(workspace: Path, name: str) -> Path:
    """*name* (relative to the current directory) as a path inside the workspace, or HelperError."""
    full = Path(os.path.abspath(os.path.join(os.path.realpath(Path.cwd()), name)))
    if full == workspace or workspace not in full.parents:
        raise HelperError(f"{name}: files go in and out of this job workspace only ({workspace})")
    return full


def _wait(client: Client, item: dict) -> dict:
    if item["status"] == "pending":
        print(f"machine: approval #{item['approval_id']} is waiting for the owner: {item['summary']}", file=sys.stderr)
    deadline = None
    if item.get("expires_at"):
        deadline = datetime.fromisoformat(item["expires_at"]).timestamp() + 5
    while item["status"] == "pending":
        if deadline is not None and time.time() > deadline:
            client.call("POST", f"/v1/agent/machines/requests/{item['id']}/cancel")
            raise HelperError("denied: no answer in time")
        time.sleep(POLL_SECONDS)
        item = client.call("GET", f"/v1/agent/machines/requests/{item['id']}")
    if item["status"] not in ("approved", "done"):
        raise HelperError(f"denied: the request is {item['status']}" + (f" ({item['error']})" if item.get("error") else ""))
    return item


def _request(client: Client, machine: str | None, tool: str, args: dict) -> dict:
    return _wait(client, client.call("POST", "/v1/agent/machines/requests",
                                     {"machine": machine, "tool": tool, "args": args}, timeout=60))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="machine", usage=USAGE, add_help=False)
    parser.add_argument("--machine")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list")
    sub.add_parser("help")
    request = sub.add_parser("request")
    request.add_argument("--scope", required=True, choices=("read", "files", "shell"))
    ls = sub.add_parser("ls")
    ls.add_argument("path")
    ls.add_argument("--depth", type=int, default=1)
    read = sub.add_parser("read")
    read.add_argument("path")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--length", type=int, default=None)
    read.add_argument("--b64", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--cwd")
    run.add_argument("--timeout", type=int, default=None)
    run.add_argument("--sh")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    pull = sub.add_parser("pull")
    pull.add_argument("path")
    pull.add_argument("dest")
    pull.add_argument("--overwrite", action="store_true")
    push = sub.add_parser("push")
    push.add_argument("source")
    push.add_argument("path")
    push.add_argument("--overwrite", action="store_true")
    shot = sub.add_parser("shot")
    shot.add_argument("dest")
    return parser


def _save(workspace: Path, dest: Path, data: bytes, headers: dict, overwrite: bool) -> None:
    from . import safefs
    expected = {key.lower(): value for key, value in headers.items()}
    if hashlib.sha256(data).hexdigest() != expected.get("x-machine-sha256") or str(len(data)) != expected.get("x-machine-size"):
        raise HelperError("the copy is incomplete (sha256 or size mismatch); nothing was saved")
    safefs.add_root(workspace)
    if safefs.lstat(dest) is not None and not overwrite:
        raise HelperError(f"{dest} exists; pass --overwrite")
    safefs.write_bytes(dest, data, 0o600, dir_mode=0o700)


def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.command in (None, "help"):
        print(USAGE)
        return 0
    token, url = os.environ.get("BOT_OFFICE_AGENT_TOKEN"), os.environ.get("BOT_OFFICE_API_URL")
    if not token or not url:
        raise HelperError("BOT_OFFICE_AGENT_TOKEN and BOT_OFFICE_API_URL are required (run inside a Bot Office turn)")
    client = Client(url, token)
    if args.command == "list":
        for item in client.call("GET", "/v1/agent/machines"):
            state = "online" if item["online"] else "offline"
            print(f"{item['name']}\t{item['hostname']}\t{item['os']}\t{state}\tlast seen {item['last_seen_at'] or 'never'}")
        return 0
    if args.command == "request":
        item = _wait(client, client.call("POST", "/v1/agent/machines/requests",
                                         {"machine": args.machine, "tool": "grant", "scope": args.scope}, timeout=60))
        print(f"allowed: {args.scope} access for this task" + (f" (grant #{item['grant_id']})" if item.get("grant_id") else ""))
        return 0
    workspace = _workspace() if args.command in ("pull", "push", "shot") else None
    if args.command == "ls":
        item = _request(client, args.machine, "ls", {"path": args.path, "depth": args.depth})
    elif args.command == "read":
        body = {"path": args.path, "offset": args.offset, "b64": args.b64}
        if args.length is not None:
            body["length"] = args.length
        item = _request(client, args.machine, "read", body)
    elif args.command == "run":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if bool(command) == bool(args.sh):
            raise HelperError("run takes a command after -- or --sh STRING, not both")
        body = {"argv": command or None, "sh": args.sh, "cwd": args.cwd}
        if args.timeout is not None:
            body["timeout"] = args.timeout
        item = _request(client, args.machine, "run", body)
    elif args.command in ("pull", "shot"):
        dest = _inside(workspace, args.dest)
        overwrite = getattr(args, "overwrite", False)
        from . import safefs
        safefs.add_root(workspace)
        if safefs.lstat(dest) is not None and not overwrite:
            raise HelperError(f"{dest} exists; pass --overwrite")
        shown = str(dest.relative_to(workspace))
        body = {"path": args.path, "dest": shown} if args.command == "pull" else {"dest": shown}
        item = _request(client, args.machine, args.command, body)
        data, headers = client.call("POST", f"/v1/agent/machines/requests/{item['id']}/execute", timeout=960, binary=True)
        _save(workspace, dest, data, headers, overwrite)
        print(f"saved {shown} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
        return 0
    else:  # push
        from . import safefs
        source = _inside(workspace, args.source)
        safefs.add_root(workspace)
        data = safefs.read_bytes(source, TRANSFER_MAX + 1)
        if len(data) > TRANSFER_MAX:
            raise HelperError("push is for files up to 100 MiB")
        digest = hashlib.sha256(data).hexdigest()
        item = _request(client, args.machine, "push", {
            "path": args.path, "source": str(source.relative_to(workspace)), "size": len(data), "sha256": digest,
            "overwrite": args.overwrite})
        result = client.call("POST", f"/v1/agent/machines/requests/{item['id']}/execute", raw=data, timeout=960)
        print(f"copied to {result['path']} ({result['size']} bytes, sha256 {result['sha256']})")
        return 0
    result = client.call("POST", f"/v1/agent/machines/requests/{item['id']}/execute", timeout=420)
    print(result["text"])
    if args.command == "run":
        code = result.get("exit")
        return code if isinstance(code, int) and 0 <= code <= 255 and code != EXIT_DENIED else (0 if code == 0 else 1)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(list(sys.argv[1:] if argv is None else argv))
    except HelperError as exc:
        print(f"machine: {exc}", file=sys.stderr)
        return EXIT_DENIED if str(exc).startswith(("denied", "403: denied", "403: declined")) else 1
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"machine: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
