"""Fake subprocess that supports both ``communicate`` (status calls) and line streaming (exec)."""

import asyncio


class FakeStdin:
    def __init__(self, inputs: list):
        self.inputs, self.data = inputs, b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.inputs.append(self.data or None)


class StreamProc:
    """Must be created inside a running loop (it builds asyncio StreamReaders)."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, inputs=None):
        self.inputs = inputs if inputs is not None else []
        self._out, self._err = stdout, stderr
        self.returncode = returncode
        self.pid = 0
        self.stdin = FakeStdin(self.inputs)
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        for reader, data in ((self.stdout, stdout), (self.stderr, stderr)):
            reader.feed_data(data)
            reader.feed_eof()

    async def communicate(self, input=None):
        self.inputs.append(input)
        return self._out, self._err

    async def wait(self) -> int:
        return self.returncode


def jsonl(*objects) -> bytes:
    import json

    return b"".join(json.dumps(o).encode() + b"\n" for o in objects)
