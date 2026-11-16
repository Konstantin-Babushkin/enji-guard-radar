.PHONY: install verify test lint typecheck check-markers fmt debt.list

install:
	uv sync

verify: lint typecheck check-markers test

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

check-markers: ## fail on fake-done markers in shipped code
	@! grep -rnE '\b(TODO|FIXME|XXX)\b|NotImplementedError' src || { echo "fake-done marker found"; exit 1; }

fmt:
	uv run ruff format .
	uv run ruff check --fix .

debt.list:
	@find docs/tech_debt/active -name '20*.md' | sort
