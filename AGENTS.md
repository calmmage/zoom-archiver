# Working on Zoom Archiver

Read [README.md](README.md) for the product contract. For credential setup or an operator-authorized archive operation, follow its OAuth setup and command sections.

## Install and check

Prerequisites: Python 3.11 or newer and `uv` on PATH. From the repository root, use the same install path as the README:

```sh
uv sync
uv run zoom-archiver --help
uv run pytest -q
```

The first sync needs access to the package index and creates a checkout-local `.venv`, including the development test dependency. Runtime dependencies are `httpx` and `typer`.

If `make` is available, `make wizard` explains prerequisites and runs `make setup`, which wraps the same `uv sync` installer followed by `make doctor`. `make help` lists these targets. The wizard is noninteractive and starts installation immediately; it does not configure credentials or install a scheduler.

Help and the fixture tests need no Zoom credentials or existing archive. The tests use temporary roots and fake HTTP responses. Once dependencies are installed, both checks work offline; `UV_OFFLINE=1` also disables uv's package-network access. `make doctor` sets this flag and uses `uv run --no-sync` for help and pytest, so it checks the installed environment without installing dependencies. Run the README installer before doctor in a fresh checkout.

Validation is complete when help exits 0 and lists `inventory`, `download`, `verify`, `safe-to-trash`, `trash`, and `mirror`, and pytest exits 0 with all tests passing. Doctor must exit 0 and print both `cli: ok` and `fixtures: ok`; the CLI line alone does not prove the fixture suite passed. Report the actual commands and results.

## Archive changes

- Use temporary roots and fake HTTP responses for development checks. Inventory and download previews still query Zoom; `--dry-run` is not an offline test.
- Keep credentials, signed download URLs, and real archive metadata out of source, fixtures, arguments, and reports. Installation checks do not require credential setup or a scheduler.
- When changing archive behavior, read the README's download guarantees, manifest fields, and mirror limitations, then run the fixture suite. Preserve unknown manifest fields and existing recording bytes, including partials and conflicting destinations.
