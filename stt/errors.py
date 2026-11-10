"""Errors whose text is written for the person at the panel.

The panel answers an error nobody planned for with one fixed sentence and
keeps the detail in its log: exception text can carry server paths and
Python internals. Some failures are planned, though, and their message is
the help: "Google Chrome not found — export Word instead", "no Anthropic
API key — add one under Cloud keys in Settings". Those raise UserError, and
the panel shows the text as it is.

A UserError message never holds a filesystem path, a traceback or another
exception's raw text. It is still a RuntimeError, so every caller that
catches RuntimeError keeps working."""


class UserError(RuntimeError):
    """A failure the user can act on. `code` is the HTTP status the panel
    answers with."""

    code = 500
