# Contributing

Thank you for your interest in `recruit-mail-observer`.

## Development Setup

```sh
uv sync
uv run --locked pytest -q
```

## Guidelines

- Keep runtime dependencies at zero (Python 3.9+ standard library only).
- Never commit `.env`, personal emails, personal Notion database IDs, or live session data.
- Add or update unit tests under `tests/` for any bug fix or feature change.
- Run `uv run --locked pytest -q` before opening a pull request.
