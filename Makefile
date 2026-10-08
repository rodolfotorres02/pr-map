ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO ?= $(ROOT)

.PHONY: install build test dev-api dev-ui install-cli

install:
	cd backend && uv sync
	cd frontend && npm install

build:
	cd frontend && npm run build

test:
	cd backend && uv run pytest

dev-api:
	cd backend && uv run prmap --repo "$(REPO)" --no-browser

dev-ui:
	cd frontend && npm run dev

# Installs `prmap` on your PATH (editable: backend changes apply immediately;
# run `make build` first so the UI is bundled).
install-cli:
	uv tool install --editable ./backend --force
