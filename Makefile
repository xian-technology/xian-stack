DOCKER_COMPOSE ?= docker compose

XIAN_CLI_DIR ?= ../xian-cli
XIAN_ABCI_DIR ?= ../xian-abci
XIAN_CONFIGS_DIR ?= ../xian-configs
XIAN_CONTRACTING_DIR ?= ../xian-contracting
XIAN_PY_DIR ?= ../xian-py
XIAN_COMETBFT_HOME ?= ./.cometbft
XIAN_BDS_DATA_DIR ?= ./.bds.db
XIAN_CONTRACTS_DIR ?= ./contracts
XIAN_DOCKER_ABCI_MEMORY_LIMIT ?= 2048m
XIAN_DOCKER_ABCI_MEMORY_RESERVATION ?= 1024m
XIAN_DOCKER_ABCI_MEMORY_SWAP ?= 2048m
XIAN_DOCKER_ABCI_PIDS_LIMIT ?= 512
XIAN_DOCKER_ABCI_NOFILE_SOFT ?= 65536
XIAN_DOCKER_ABCI_NOFILE_HARD ?= 65536
XIAN_DOCKER_POSTGRES_MEMORY_LIMIT ?= 1024m
XIAN_DOCKER_POSTGRES_MEMORY_RESERVATION ?= 512m
XIAN_DOCKER_POSTGRES_MEMORY_SWAP ?= 1024m
XIAN_DOCKER_POSTGRES_PIDS_LIMIT ?= 256
XIAN_DOCKER_POSTGRES_NOFILE_SOFT ?= 65536
XIAN_DOCKER_POSTGRES_NOFILE_HARD ?= 65536
XIAN_DOCKER_POSTGRAPHILE_MEMORY_LIMIT ?= 768m
XIAN_DOCKER_POSTGRAPHILE_MEMORY_RESERVATION ?= 256m
XIAN_DOCKER_POSTGRAPHILE_MEMORY_SWAP ?= 768m
XIAN_DOCKER_POSTGRAPHILE_PIDS_LIMIT ?= 256
XIAN_DOCKER_POSTGRAPHILE_NOFILE_SOFT ?= 65536
XIAN_DOCKER_POSTGRAPHILE_NOFILE_HARD ?= 65536
XIAN_LOCALNET_NODE_MEMORY_LIMIT ?= 1536m
XIAN_LOCALNET_NODE_MEMORY_RESERVATION ?= 1024m
XIAN_LOCALNET_NODE_MEMORY_SWAP ?= 1536m
XIAN_LOCALNET_NODE_PIDS_LIMIT ?= 512
XIAN_LOCALNET_NODE_NOFILE_SOFT ?= 65536
XIAN_LOCALNET_NODE_NOFILE_HARD ?= 65536

export XIAN_CLI_DIR := $(abspath $(XIAN_CLI_DIR))
export XIAN_ABCI_DIR := $(abspath $(XIAN_ABCI_DIR))
export XIAN_CONFIGS_DIR := $(abspath $(XIAN_CONFIGS_DIR))
export XIAN_CONTRACTING_DIR := $(abspath $(XIAN_CONTRACTING_DIR))
export XIAN_PY_DIR := $(abspath $(XIAN_PY_DIR))
export XIAN_COMETBFT_HOME := $(abspath $(XIAN_COMETBFT_HOME))
export XIAN_BDS_DATA_DIR := $(abspath $(XIAN_BDS_DATA_DIR))
export XIAN_CONTRACTS_DIR := $(abspath $(XIAN_CONTRACTS_DIR))
export XIAN_DOCKER_ABCI_MEMORY_LIMIT := $(XIAN_DOCKER_ABCI_MEMORY_LIMIT)
export XIAN_DOCKER_ABCI_MEMORY_RESERVATION := $(XIAN_DOCKER_ABCI_MEMORY_RESERVATION)
export XIAN_DOCKER_ABCI_MEMORY_SWAP := $(XIAN_DOCKER_ABCI_MEMORY_SWAP)
export XIAN_DOCKER_ABCI_PIDS_LIMIT := $(XIAN_DOCKER_ABCI_PIDS_LIMIT)
export XIAN_DOCKER_ABCI_NOFILE_SOFT := $(XIAN_DOCKER_ABCI_NOFILE_SOFT)
export XIAN_DOCKER_ABCI_NOFILE_HARD := $(XIAN_DOCKER_ABCI_NOFILE_HARD)
export XIAN_DOCKER_POSTGRES_MEMORY_LIMIT := $(XIAN_DOCKER_POSTGRES_MEMORY_LIMIT)
export XIAN_DOCKER_POSTGRES_MEMORY_RESERVATION := $(XIAN_DOCKER_POSTGRES_MEMORY_RESERVATION)
export XIAN_DOCKER_POSTGRES_MEMORY_SWAP := $(XIAN_DOCKER_POSTGRES_MEMORY_SWAP)
export XIAN_DOCKER_POSTGRES_PIDS_LIMIT := $(XIAN_DOCKER_POSTGRES_PIDS_LIMIT)
export XIAN_DOCKER_POSTGRES_NOFILE_SOFT := $(XIAN_DOCKER_POSTGRES_NOFILE_SOFT)
export XIAN_DOCKER_POSTGRES_NOFILE_HARD := $(XIAN_DOCKER_POSTGRES_NOFILE_HARD)
export XIAN_DOCKER_POSTGRAPHILE_MEMORY_LIMIT := $(XIAN_DOCKER_POSTGRAPHILE_MEMORY_LIMIT)
export XIAN_DOCKER_POSTGRAPHILE_MEMORY_RESERVATION := $(XIAN_DOCKER_POSTGRAPHILE_MEMORY_RESERVATION)
export XIAN_DOCKER_POSTGRAPHILE_MEMORY_SWAP := $(XIAN_DOCKER_POSTGRAPHILE_MEMORY_SWAP)
export XIAN_DOCKER_POSTGRAPHILE_PIDS_LIMIT := $(XIAN_DOCKER_POSTGRAPHILE_PIDS_LIMIT)
export XIAN_DOCKER_POSTGRAPHILE_NOFILE_SOFT := $(XIAN_DOCKER_POSTGRAPHILE_NOFILE_SOFT)
export XIAN_DOCKER_POSTGRAPHILE_NOFILE_HARD := $(XIAN_DOCKER_POSTGRAPHILE_NOFILE_HARD)
export XIAN_LOCALNET_NODE_MEMORY_LIMIT := $(XIAN_LOCALNET_NODE_MEMORY_LIMIT)
export XIAN_LOCALNET_NODE_MEMORY_RESERVATION := $(XIAN_LOCALNET_NODE_MEMORY_RESERVATION)
export XIAN_LOCALNET_NODE_MEMORY_SWAP := $(XIAN_LOCALNET_NODE_MEMORY_SWAP)
export XIAN_LOCALNET_NODE_PIDS_LIMIT := $(XIAN_LOCALNET_NODE_PIDS_LIMIT)
export XIAN_LOCALNET_NODE_NOFILE_SOFT := $(XIAN_LOCALNET_NODE_NOFILE_SOFT)
export XIAN_LOCALNET_NODE_NOFILE_HARD := $(XIAN_LOCALNET_NODE_NOFILE_HARD)

ABCI_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-abci.yml
ABCI_BDS_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-abci.yml -f docker-compose-abci-bds.yml
ABCI_DEV_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-abci.yml -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml
CONTRACTING_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-contracting.yml
LOCALNET_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-localnet.yml

LOCALNET_NODES ?= 4
LOCALNET_MEMWATCH_MINUTES ?= 10
LOCALNET_LEAK_HUNT_MINUTES ?= 10

.DEFAULT_GOAL := help

.PHONY: help print-env validate smoke smoke-cli prepare-dirs \
	dev-contracting-shell dev-contracting-up dev-contracting-build dev-contracting-down \
	dev-abci-build dev-abci-up dev-abci-down dev-abci-shell \
	abci-build abci-up abci-down dev-base-abci-shell \
	abci-bds-build abci-bds-up abci-bds-down dev-bds-abci-shell \
	wipe-bds node-wipe node-wipe-all node-reset \
	node-stop node-start node-start-bds node-init node-configure node-id \
	node-status \
	localnet-init localnet-build localnet-up localnet-down localnet-status \
	localnet-burst localnet-memwatch localnet-leak-hunt \
	localnet-clean localnet-logs localnet-shell

help:
	@printf "Available targets:\n"
	@printf "  %-24s %s\n" "print-env" "Show resolved workspace and data paths"
	@printf "  %-24s %s\n" "validate" "Validate compose topology and required local paths"
	@printf "  %-24s %s\n" "smoke" "Run the smallest real ABCI bring-up and shutdown path"
	@printf "  %-24s %s\n" "smoke-cli" "Run the cross-repo operator flow through xian-cli"
	@printf "  %-24s %s\n" "abci-build" "Build the base ABCI image"
	@printf "  %-24s %s\n" "abci-up" "Start the base ABCI container"
	@printf "  %-24s %s\n" "abci-down" "Stop the base ABCI container"
	@printf "  %-24s %s\n" "abci-bds-build" "Build the ABCI + BDS image stack"
	@printf "  %-24s %s\n" "abci-bds-up" "Start the ABCI + BDS stack"
	@printf "  %-24s %s\n" "abci-bds-down" "Stop the ABCI + BDS stack"
	@printf "  %-24s %s\n" "node-init" "Initialize the CometBFT home inside the ABCI container"
	@printf "  %-24s %s\n" "node-configure" "Render node config via xian-abci's configure helper"
	@printf "  %-24s %s\n" "node-start/node-stop" "Start or stop the node runtime inside the container"
	@printf "  %-24s %s\n" "node-start-bds" "Start the node runtime with block-service mode"
	@printf "  %-24s %s\n" "node-status" "Report backend container and PM2 process state as JSON"
	@printf "  %-24s %s\n" "dev-abci-build/dev-abci-up" "Developer-only ABCI dev stack targets"
	@printf "  %-24s %s\n" "dev-contracting-build" "Developer-only contracting image build"
	@printf "\n  Localnet (multi-node):\n"
	@printf "  %-24s %s\n" "localnet-init" "Generate keys, genesis, configs for N nodes (LOCALNET_NODES=4)"
	@printf "  %-24s %s\n" "localnet-build" "Build the localnet Docker image"
	@printf "  %-24s %s\n" "localnet-up" "Start all localnet nodes"
	@printf "  %-24s %s\n" "localnet-down" "Stop all localnet nodes"
	@printf "  %-24s %s\n" "localnet-status" "Show block height and peer count for each node"
	@printf "  %-24s %s\n" "localnet-burst" "Drive mixed tx load against the localnet"
	@printf "  %-24s %s\n" "localnet-memwatch" "Sample container memory during localnet tx load"
	@printf "  %-24s %s\n" "localnet-leak-hunt" "Split localnet memory growth by process"
	@printf "  %-24s %s\n" "localnet-logs" "Tail logs from all nodes"
	@printf "  %-24s %s\n" "localnet-shell" "Open a shell in node-0"
	@printf "  %-24s %s\n" "localnet-clean" "Stop nodes and delete all localnet data"

print-env:
	@printf "XIAN_CLI_DIR=%s\n" "$(XIAN_CLI_DIR)"
	@printf "XIAN_ABCI_DIR=%s\n" "$(XIAN_ABCI_DIR)"
	@printf "XIAN_CONFIGS_DIR=%s\n" "$(XIAN_CONFIGS_DIR)"
	@printf "XIAN_CONTRACTING_DIR=%s\n" "$(XIAN_CONTRACTING_DIR)"
	@printf "XIAN_PY_DIR=%s\n" "$(XIAN_PY_DIR)"
	@printf "XIAN_COMETBFT_HOME=%s\n" "$(XIAN_COMETBFT_HOME)"
	@printf "XIAN_BDS_DATA_DIR=%s\n" "$(XIAN_BDS_DATA_DIR)"
	@printf "XIAN_CONTRACTS_DIR=%s\n" "$(XIAN_CONTRACTS_DIR)"
	@printf "XIAN_DOCKER_ABCI_MEMORY_LIMIT=%s\n" "$(XIAN_DOCKER_ABCI_MEMORY_LIMIT)"
	@printf "XIAN_DOCKER_ABCI_MEMORY_RESERVATION=%s\n" "$(XIAN_DOCKER_ABCI_MEMORY_RESERVATION)"
	@printf "XIAN_DOCKER_ABCI_MEMORY_SWAP=%s\n" "$(XIAN_DOCKER_ABCI_MEMORY_SWAP)"
	@printf "XIAN_DOCKER_ABCI_PIDS_LIMIT=%s\n" "$(XIAN_DOCKER_ABCI_PIDS_LIMIT)"
	@printf "XIAN_DOCKER_ABCI_NOFILE_SOFT=%s\n" "$(XIAN_DOCKER_ABCI_NOFILE_SOFT)"
	@printf "XIAN_DOCKER_ABCI_NOFILE_HARD=%s\n" "$(XIAN_DOCKER_ABCI_NOFILE_HARD)"
	@printf "XIAN_DOCKER_POSTGRES_MEMORY_LIMIT=%s\n" "$(XIAN_DOCKER_POSTGRES_MEMORY_LIMIT)"
	@printf "XIAN_DOCKER_POSTGRES_MEMORY_RESERVATION=%s\n" "$(XIAN_DOCKER_POSTGRES_MEMORY_RESERVATION)"
	@printf "XIAN_DOCKER_POSTGRES_MEMORY_SWAP=%s\n" "$(XIAN_DOCKER_POSTGRES_MEMORY_SWAP)"
	@printf "XIAN_DOCKER_POSTGRES_PIDS_LIMIT=%s\n" "$(XIAN_DOCKER_POSTGRES_PIDS_LIMIT)"
	@printf "XIAN_DOCKER_POSTGRES_NOFILE_SOFT=%s\n" "$(XIAN_DOCKER_POSTGRES_NOFILE_SOFT)"
	@printf "XIAN_DOCKER_POSTGRES_NOFILE_HARD=%s\n" "$(XIAN_DOCKER_POSTGRES_NOFILE_HARD)"
	@printf "XIAN_DOCKER_POSTGRAPHILE_MEMORY_LIMIT=%s\n" "$(XIAN_DOCKER_POSTGRAPHILE_MEMORY_LIMIT)"
	@printf "XIAN_DOCKER_POSTGRAPHILE_MEMORY_RESERVATION=%s\n" "$(XIAN_DOCKER_POSTGRAPHILE_MEMORY_RESERVATION)"
	@printf "XIAN_DOCKER_POSTGRAPHILE_MEMORY_SWAP=%s\n" "$(XIAN_DOCKER_POSTGRAPHILE_MEMORY_SWAP)"
	@printf "XIAN_DOCKER_POSTGRAPHILE_PIDS_LIMIT=%s\n" "$(XIAN_DOCKER_POSTGRAPHILE_PIDS_LIMIT)"
	@printf "XIAN_DOCKER_POSTGRAPHILE_NOFILE_SOFT=%s\n" "$(XIAN_DOCKER_POSTGRAPHILE_NOFILE_SOFT)"
	@printf "XIAN_DOCKER_POSTGRAPHILE_NOFILE_HARD=%s\n" "$(XIAN_DOCKER_POSTGRAPHILE_NOFILE_HARD)"
	@printf "XIAN_LOCALNET_NODE_MEMORY_LIMIT=%s\n" "$(XIAN_LOCALNET_NODE_MEMORY_LIMIT)"
	@printf "XIAN_LOCALNET_NODE_MEMORY_RESERVATION=%s\n" "$(XIAN_LOCALNET_NODE_MEMORY_RESERVATION)"
	@printf "XIAN_LOCALNET_NODE_MEMORY_SWAP=%s\n" "$(XIAN_LOCALNET_NODE_MEMORY_SWAP)"
	@printf "XIAN_LOCALNET_NODE_PIDS_LIMIT=%s\n" "$(XIAN_LOCALNET_NODE_PIDS_LIMIT)"
	@printf "XIAN_LOCALNET_NODE_NOFILE_SOFT=%s\n" "$(XIAN_LOCALNET_NODE_NOFILE_SOFT)"
	@printf "XIAN_LOCALNET_NODE_NOFILE_HARD=%s\n" "$(XIAN_LOCALNET_NODE_NOFILE_HARD)"

validate:
	./scripts/validate-stack.sh

smoke:
	./scripts/smoke-stack.sh

smoke-cli:
	./scripts/smoke-cli.sh

prepare-dirs:
	mkdir -p "$(XIAN_COMETBFT_HOME)" "$(XIAN_BDS_DATA_DIR)" "$(XIAN_CONTRACTS_DIR)"


# Dev-only contracting commands
dev-contracting-shell: dev-contracting-up

dev-contracting-up: prepare-dirs
	$(CONTRACTING_COMPOSE) up -d
	$(CONTRACTING_COMPOSE) exec contracting /bin/bash

dev-contracting-build: prepare-dirs
	$(CONTRACTING_COMPOSE) build

dev-contracting-down:
	$(CONTRACTING_COMPOSE) down


# Dev-only ABCI commands
dev-abci-build: prepare-dirs
	$(ABCI_DEV_COMPOSE) build --no-cache

dev-abci-up: prepare-dirs
	$(ABCI_DEV_COMPOSE) up -d

dev-abci-down:
	$(ABCI_DEV_COMPOSE) down

dev-abci-shell: dev-abci-up
	$(ABCI_DEV_COMPOSE) exec -w /usr/src/app/xian-abci abci /bin/bash


# Runtime container commands
abci-build: prepare-dirs
	$(ABCI_COMPOSE) build --no-cache

abci-up: prepare-dirs
	$(ABCI_COMPOSE) up -d

abci-down:
	$(ABCI_COMPOSE) down

dev-base-abci-shell: abci-up
	$(ABCI_COMPOSE) exec -w /usr/src/app/xian-abci abci /bin/bash


# ABCI BDS Commands
abci-bds-build: prepare-dirs
	$(ABCI_BDS_COMPOSE) build --no-cache

abci-bds-up: prepare-dirs
	$(ABCI_BDS_COMPOSE) up -d

abci-bds-down:
	$(ABCI_BDS_COMPOSE) down

dev-bds-abci-shell: abci-bds-up
	$(ABCI_BDS_COMPOSE) exec -w /usr/src/app/xian-abci abci /bin/bash

wipe-bds:
	rm -rf "$(XIAN_BDS_DATA_DIR)"/*


# Node runtime commands
node-wipe:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make wipe"

node-wipe-all: wipe-bds node-wipe

node-reset:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make dwu"

node-stop:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make down"

node-start:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make up"

node-start-bds:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make up-bds"

node-init:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make init"

node-configure:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && python -m xian.cli.configure_node ${CONFIGURE_ARGS}"

node-id:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make node-id"

node-status:
	@./scripts/node-status.sh


# ── Localnet (multi-node) ────────────────────────────────────────────

localnet-init:
	uv run --project "$(XIAN_ABCI_DIR)" python3 ./scripts/localnet-init.py --nodes $(LOCALNET_NODES) --clean

localnet-build:
	$(LOCALNET_COMPOSE) build

localnet-up:
	@if [ ! -f docker-compose-localnet.yml ]; then \
		echo "ERROR: Run 'make localnet-init' first." >&2; exit 1; \
	fi
	$(LOCALNET_COMPOSE) up -d

localnet-down:
	@if [ -f docker-compose-localnet.yml ]; then \
		$(LOCALNET_COMPOSE) down; \
	fi

localnet-status:
	@./scripts/localnet-status.sh

localnet-burst:
	uv run --project "$(XIAN_PY_DIR)" python3 ./scripts/localnet-burst-test.py

localnet-memwatch:
	uv run --project "$(XIAN_PY_DIR)" python3 ./scripts/localnet-memwatch.py $(LOCALNET_MEMWATCH_MINUTES)

localnet-leak-hunt:
	uv run --project "$(XIAN_PY_DIR)" python3 ./scripts/localnet-leak-hunt.py $(LOCALNET_LEAK_HUNT_MINUTES)

localnet-logs:
	$(LOCALNET_COMPOSE) logs -f --tail=50

localnet-shell:
	$(LOCALNET_COMPOSE) exec node-0 /bin/bash

localnet-clean: localnet-down
	rm -rf .localnet
	rm -f docker-compose-localnet.yml
