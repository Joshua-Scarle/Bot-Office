from __future__ import annotations

import asyncio

from . import isolation

async def run_desktop_command(
    slot: str, command: list[str], timeout: int = 60
) -> dict[str, str | int]:
    if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
        raise ValueError("command must be a non-empty argv list")
    # isolated layout: podman runs as the bots user (their containers) through the launcher
    options, header = isolation.exec_options()
    proc = await asyncio.create_subprocess_exec(
        *isolation.wrap(["podman", "exec", slot, *command], env_stdin=True, runtime_dir=True),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )
    try:
        communicate = proc.communicate(header) if header else proc.communicate()
        stdout, stderr = await asyncio.wait_for(communicate, timeout=timeout)
        code = proc.returncode or 0
    except asyncio.TimeoutError:
        # isolated: sudo relays SIGTERM to podman (a SIGKILL would orphan it)
        proc.terminate() if isolation.active() else proc.kill()
        stdout, stderr = await proc.communicate()
        code = 124
        stderr += b"\nBot-Office: desktop command timed out"
    return {
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
        "exit_code": code,
    }
