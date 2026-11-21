"""A bot workspace laid out like a fresh clone, for tests of the git guard."""
import tempfile
from pathlib import Path


def clone_workspace(default="main"):
    """A workspace holding a clone's git files: origin's and upstream's default branch is `default`
    (refs/remotes/<remote>/HEAD), as after `git clone`. The guard reads these files, never runs git."""
    root = Path(tempfile.mkdtemp(prefix="bo-guard-")) / "work"
    for remote in ("origin", "upstream"):
        (root / ".git" / "refs" / "remotes" / remote).mkdir(parents=True)
        (root / ".git" / "refs" / "remotes" / remote / "HEAD").write_text(f"ref: refs/remotes/{remote}/{default}\n")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/feature\n")
    (root / "repo").mkdir()
    return str(root)
