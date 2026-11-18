SHELL := /bin/bash
.DEFAULT_GOAL := run

VENV ?= .venv
GATEWAY_PORT ?= 8000
STUDIO_PORT ?= 2025
GATEWAY_DOCKER_HOST ?= 172.17.0.1

export PYTHONPATH := src$(if $(PYTHONPATH),:$(PYTHONPATH))
LANGSMITH_TRACING ?= true
export LANGSMITH_TRACING

.PHONY: run stop gateway studio qa-image
stop:
	"$(VENV)/bin/python" -m maf.stop

run:
	@set -eu; \
	test -x "$(VENV)/bin/langgraph" || { echo 'Cần langgraph-cli[inmem] trong .venv.' >&2; exit 1; }; \
	"$(VENV)/bin/python" -m maf.serve --port "$(GATEWAY_PORT)" --docker-host "$(GATEWAY_DOCKER_HOST)" & gateway=$$!; \
	"$(VENV)/bin/langgraph" dev --host 127.0.0.1 --port "$(STUDIO_PORT)" --no-reload --no-browser & studio=$$!; \
	trap 'kill "$$gateway" "$$studio" 2>/dev/null || true; wait "$$gateway" "$$studio" 2>/dev/null || true' EXIT; \
	trap 'exit 130' INT; trap 'exit 143' TERM; \
	wait -n "$$gateway" "$$studio"

gateway:
	"$(VENV)/bin/python" -m maf.serve --port "$(GATEWAY_PORT)" --docker-host "$(GATEWAY_DOCKER_HOST)"

studio:
	"$(VENV)/bin/langgraph" dev --host 127.0.0.1 --port "$(STUDIO_PORT)" --no-reload --no-browser

qa-image:
	@docker image inspect maf-pytest:local >/dev/null 2>&1 || \
		docker build --file docker/pytest.Dockerfile --tag maf-pytest:local docker
