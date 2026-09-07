.PHONY: help wizard setup doctor

help:
	@printf '%s\n' \
		'Zoom Archiver requires Python 3.11+ and uv on PATH; these wrappers also require make.' \
		'make wizard  Explain prerequisites, then run make setup.' \
		'make setup   Run the README installer (uv sync), then make doctor.' \
		'make doctor  Check CLI help and offline fixtures in the installed environment.' \
		'First setup needs package-index access and creates .venv in this checkout.' \
		'Checks need no Zoom credentials or archive; no scheduler is installed.'

wizard:
	@$(MAKE) help
	@$(MAKE) setup

setup:
	@command -v uv >/dev/null 2>&1 || { printf '%s\n' 'uv is required on PATH; follow the uv installation link in README.md.' >&2; exit 1; }
	uv sync
	@$(MAKE) doctor

doctor:
	UV_OFFLINE=1 uv run --no-sync zoom-archiver --help
	@printf '%s\n' 'cli: ok'
	UV_OFFLINE=1 uv run --no-sync pytest -q
	@printf '%s\n' 'fixtures: ok'
