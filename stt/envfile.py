"""The stt.env parser: one KEY=VALUE setting per line, never run as shell.

stt.env is not sourced: the default folder paths hold spaces, and a sourced
line with a space runs the rest of the line as a command. The rules, which
run.sh, setup.sh and init.sh follow in shell:

- A blank line, a line that starts with '#', and a line with no '=' are
  skipped.
- The key is the text before the first '=', and the value is the rest.
  Spaces around both are trimmed.
- One pair of matching quotes around the whole value is removed, as a
  shell would remove it: FOO="a b" and FOO='a b' both read as 'a b'. A
  hand-written quoted path kept its quotes before, so it named a folder
  with quote marks in its name. Nothing else is unescaped: a backslash,
  an '&' or a '|' is plain text.
- A later line for the same key wins over an earlier one.

This module imports nothing from the package, so run_batch.py can read
stt.env before stt.config is imported.
"""
from pathlib import Path


def unquote(value: str) -> str:
    """`value` trimmed, without one pair of matching quotes around it."""
    v = str(value).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_text(text: str) -> dict:
    out = {}
    for ln in str(text).splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if k:
            out[k] = unquote(v)
    return out


def parse(path) -> dict:
    """The settings in the file at `path`, or {} when it is not there."""
    p = Path(path)
    try:
        return parse_text(p.read_text())
    except FileNotFoundError:
        return {}
