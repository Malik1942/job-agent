"""Load scripts/.env, then exec the requested command.

launchd's shell environment is intentionally sparse, and macOS can refuse to
source local dotfiles from zsh. This wrapper reads simple KEY=value lines
directly and passes them to the child process without printing secrets.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def _load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(value, comments=True, posix=True)
        except ValueError:
            parsed = []
        env[key] = parsed[0] if parsed else value.strip().strip("\"'")
    return env


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run_with_env.py <command> [args...]", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    env = _load_env(root / "scripts" / ".env")
    os.chdir(root)
    os.execvpe(sys.argv[1], sys.argv[1:], env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
