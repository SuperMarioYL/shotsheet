.PHONY: install dev test lint fmt build clean

install:
	uv sync

dev:
	uv sync --extra dev

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

build:
	uv build

clean:
	rm -rf dist build .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
