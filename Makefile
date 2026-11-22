UV ?= uv
BUSYBOX ?= busybox
TEST_ARGS ?=
export BUSYBOX

.PHONY: dev build test test-serial validate-generated lint format check clean

dev:
	$(UV) sync --locked
	$(UV) run --locked prek install

build:
	$(UV) run --locked python scripts/build_installer.py

validate-generated: build
	@command -v $(BUSYBOX) >/dev/null || { echo "BusyBox is required for the offline test suite." >&2; exit 1; }
	$(UV) run --locked python scripts/check_shell.py --generated

test: validate-generated
	$(UV) run --locked pytest $(TEST_ARGS)

test-serial: validate-generated
	$(UV) run --locked pytest -n 0 $(TEST_ARGS)

lint:
	$(UV) run --locked prek run --all-files

format:
	$(UV) run --locked ruff check --fix scripts tests
	$(UV) run --locked ruff format scripts tests
	$(UV) run --locked shfmt -w -ln posix -i 4 -ci src/*.sh tools/*.sh
	$(UV) run --locked pymarkdown fix README.md AGENTS.md tests/fixtures/README.md

check: lint test

clean:
	rm -rf dist
