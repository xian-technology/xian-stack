DOCKER_COMPOSE ?= docker compose

XIAN_CLI_DIR ?= ../xian-cli
XIAN_ABCI_DIR ?= ../xian-abci
XIAN_CONFIGS_DIR ?= ../xian-configs
XIAN_CONTRACTING_DIR ?= ../xian-contracting
XIAN_PY_DIR ?= ../xian-py
XIAN_COMETBFT_HOME ?= ./.cometbft
XIAN_BDS_DATA_DIR ?= ./.bds.db
XIAN_CONTRACTS_DIR ?= ./contracts
XIAN_COMETBFT_VERSION ?= 0.38.12
XIAN_S6_OVERLAY_VERSION ?= 3.2.1.0
XIAN_S6_VERBOSITY ?= 1
XIAN_STACK_TOPOLOGY ?= integrated
XIAN_DOCKER_ABCI_MEMORY_LIMIT ?= 2048m
XIAN_DOCKER_ABCI_MEMORY_RESERVATION ?= 1024m
XIAN_DOCKER_ABCI_MEMORY_SWAP ?= 2048m
XIAN_DOCKER_ABCI_PIDS_LIMIT ?= 512
XIAN_DOCKER_ABCI_NOFILE_SOFT ?= 65536
XIAN_DOCKER_ABCI_NOFILE_HARD ?= 65536
XIAN_DOCKER_FIDELITY_ABCI_MEMORY_LIMIT ?= 1536m
XIAN_DOCKER_FIDELITY_ABCI_MEMORY_RESERVATION ?= 768m
XIAN_DOCKER_FIDELITY_ABCI_MEMORY_SWAP ?= 1536m
XIAN_DOCKER_FIDELITY_ABCI_PIDS_LIMIT ?= 384
XIAN_DOCKER_FIDELITY_ABCI_NOFILE_SOFT ?= 65536
XIAN_DOCKER_FIDELITY_ABCI_NOFILE_HARD ?= 65536
XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_LIMIT ?= 768m
XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_RESERVATION ?= 256m
XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_SWAP ?= 768m
XIAN_DOCKER_FIDELITY_COMETBFT_PIDS_LIMIT ?= 256
XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_SOFT ?= 65536
XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_HARD ?= 65536
XIAN_DOCKER_DASHBOARD_MEMORY_LIMIT ?= 512m
XIAN_DOCKER_DASHBOARD_MEMORY_RESERVATION ?= 256m
XIAN_DOCKER_DASHBOARD_MEMORY_SWAP ?= 512m
XIAN_DOCKER_DASHBOARD_PIDS_LIMIT ?= 256
XIAN_DOCKER_DASHBOARD_NOFILE_SOFT ?= 65536
XIAN_DOCKER_DASHBOARD_NOFILE_HARD ?= 65536
XIAN_DASHBOARD_HOST ?= 127.0.0.1
XIAN_DASHBOARD_PORT ?= 8080
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
XIAN_LOCALNET_TOPOLOGY ?= integrated
XIAN_LOCALNET_NODE_MEMORY_LIMIT ?= 1536m
XIAN_LOCALNET_NODE_MEMORY_RESERVATION ?= 1024m
XIAN_LOCALNET_NODE_MEMORY_SWAP ?= 1536m
XIAN_LOCALNET_NODE_PIDS_LIMIT ?= 512
XIAN_LOCALNET_NODE_NOFILE_SOFT ?= 65536
XIAN_LOCALNET_NODE_NOFILE_HARD ?= 65536
XIAN_LOCALNET_ABCI_MEMORY_LIMIT ?= 1024m
XIAN_LOCALNET_ABCI_MEMORY_RESERVATION ?= 768m
XIAN_LOCALNET_ABCI_MEMORY_SWAP ?= 1024m
XIAN_LOCALNET_ABCI_PIDS_LIMIT ?= 384
XIAN_LOCALNET_ABCI_NOFILE_SOFT ?= 65536
XIAN_LOCALNET_ABCI_NOFILE_HARD ?= 65536
XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT ?= 512m
XIAN_LOCALNET_COMETBFT_MEMORY_RESERVATION ?= 256m
XIAN_LOCALNET_COMETBFT_MEMORY_SWAP ?= 512m
XIAN_LOCALNET_COMETBFT_PIDS_LIMIT ?= 256
XIAN_LOCALNET_COMETBFT_NOFILE_SOFT ?= 65536
XIAN_LOCALNET_COMETBFT_NOFILE_HARD ?= 65536

export XIAN_CLI_DIR := $(abspath $(XIAN_CLI_DIR))
export XIAN_ABCI_DIR := $(abspath $(XIAN_ABCI_DIR))
export XIAN_CONFIGS_DIR := $(abspath $(XIAN_CONFIGS_DIR))
export XIAN_CONTRACTING_DIR := $(abspath $(XIAN_CONTRACTING_DIR))
export XIAN_PY_DIR := $(abspath $(XIAN_PY_DIR))
export XIAN_COMETBFT_HOME := $(abspath $(XIAN_COMETBFT_HOME))
export XIAN_BDS_DATA_DIR := $(abspath $(XIAN_BDS_DATA_DIR))
export XIAN_CONTRACTS_DIR := $(abspath $(XIAN_CONTRACTS_DIR))
export XIAN_COMETBFT_VERSION := $(XIAN_COMETBFT_VERSION)
export XIAN_S6_OVERLAY_VERSION := $(XIAN_S6_OVERLAY_VERSION)
export XIAN_S6_VERBOSITY := $(XIAN_S6_VERBOSITY)
export XIAN_STACK_TOPOLOGY := $(XIAN_STACK_TOPOLOGY)
export XIAN_DOCKER_ABCI_MEMORY_LIMIT := $(XIAN_DOCKER_ABCI_MEMORY_LIMIT)
export XIAN_DOCKER_ABCI_MEMORY_RESERVATION := $(XIAN_DOCKER_ABCI_MEMORY_RESERVATION)
export XIAN_DOCKER_ABCI_MEMORY_SWAP := $(XIAN_DOCKER_ABCI_MEMORY_SWAP)
export XIAN_DOCKER_ABCI_PIDS_LIMIT := $(XIAN_DOCKER_ABCI_PIDS_LIMIT)
export XIAN_DOCKER_ABCI_NOFILE_SOFT := $(XIAN_DOCKER_ABCI_NOFILE_SOFT)
export XIAN_DOCKER_ABCI_NOFILE_HARD := $(XIAN_DOCKER_ABCI_NOFILE_HARD)
export XIAN_DOCKER_FIDELITY_ABCI_MEMORY_LIMIT := $(XIAN_DOCKER_FIDELITY_ABCI_MEMORY_LIMIT)
export XIAN_DOCKER_FIDELITY_ABCI_MEMORY_RESERVATION := $(XIAN_DOCKER_FIDELITY_ABCI_MEMORY_RESERVATION)
export XIAN_DOCKER_FIDELITY_ABCI_MEMORY_SWAP := $(XIAN_DOCKER_FIDELITY_ABCI_MEMORY_SWAP)
export XIAN_DOCKER_FIDELITY_ABCI_PIDS_LIMIT := $(XIAN_DOCKER_FIDELITY_ABCI_PIDS_LIMIT)
export XIAN_DOCKER_FIDELITY_ABCI_NOFILE_SOFT := $(XIAN_DOCKER_FIDELITY_ABCI_NOFILE_SOFT)
export XIAN_DOCKER_FIDELITY_ABCI_NOFILE_HARD := $(XIAN_DOCKER_FIDELITY_ABCI_NOFILE_HARD)
export XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_LIMIT := $(XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_LIMIT)
export XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_RESERVATION := $(XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_RESERVATION)
export XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_SWAP := $(XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_SWAP)
export XIAN_DOCKER_FIDELITY_COMETBFT_PIDS_LIMIT := $(XIAN_DOCKER_FIDELITY_COMETBFT_PIDS_LIMIT)
export XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_SOFT := $(XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_SOFT)
export XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_HARD := $(XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_HARD)
export XIAN_DOCKER_DASHBOARD_MEMORY_LIMIT := $(XIAN_DOCKER_DASHBOARD_MEMORY_LIMIT)
export XIAN_DOCKER_DASHBOARD_MEMORY_RESERVATION := $(XIAN_DOCKER_DASHBOARD_MEMORY_RESERVATION)
export XIAN_DOCKER_DASHBOARD_MEMORY_SWAP := $(XIAN_DOCKER_DASHBOARD_MEMORY_SWAP)
export XIAN_DOCKER_DASHBOARD_PIDS_LIMIT := $(XIAN_DOCKER_DASHBOARD_PIDS_LIMIT)
export XIAN_DOCKER_DASHBOARD_NOFILE_SOFT := $(XIAN_DOCKER_DASHBOARD_NOFILE_SOFT)
export XIAN_DOCKER_DASHBOARD_NOFILE_HARD := $(XIAN_DOCKER_DASHBOARD_NOFILE_HARD)
export XIAN_DASHBOARD_HOST := $(XIAN_DASHBOARD_HOST)
export XIAN_DASHBOARD_PORT := $(XIAN_DASHBOARD_PORT)
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
export XIAN_LOCALNET_TOPOLOGY := $(XIAN_LOCALNET_TOPOLOGY)
export XIAN_LOCALNET_NODE_MEMORY_LIMIT := $(XIAN_LOCALNET_NODE_MEMORY_LIMIT)
export XIAN_LOCALNET_NODE_MEMORY_RESERVATION := $(XIAN_LOCALNET_NODE_MEMORY_RESERVATION)
export XIAN_LOCALNET_NODE_MEMORY_SWAP := $(XIAN_LOCALNET_NODE_MEMORY_SWAP)
export XIAN_LOCALNET_NODE_PIDS_LIMIT := $(XIAN_LOCALNET_NODE_PIDS_LIMIT)
export XIAN_LOCALNET_NODE_NOFILE_SOFT := $(XIAN_LOCALNET_NODE_NOFILE_SOFT)
export XIAN_LOCALNET_NODE_NOFILE_HARD := $(XIAN_LOCALNET_NODE_NOFILE_HARD)
export XIAN_LOCALNET_ABCI_MEMORY_LIMIT := $(XIAN_LOCALNET_ABCI_MEMORY_LIMIT)
export XIAN_LOCALNET_ABCI_MEMORY_RESERVATION := $(XIAN_LOCALNET_ABCI_MEMORY_RESERVATION)
export XIAN_LOCALNET_ABCI_MEMORY_SWAP := $(XIAN_LOCALNET_ABCI_MEMORY_SWAP)
export XIAN_LOCALNET_ABCI_PIDS_LIMIT := $(XIAN_LOCALNET_ABCI_PIDS_LIMIT)
export XIAN_LOCALNET_ABCI_NOFILE_SOFT := $(XIAN_LOCALNET_ABCI_NOFILE_SOFT)
export XIAN_LOCALNET_ABCI_NOFILE_HARD := $(XIAN_LOCALNET_ABCI_NOFILE_HARD)
export XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT := $(XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT)
export XIAN_LOCALNET_COMETBFT_MEMORY_RESERVATION := $(XIAN_LOCALNET_COMETBFT_MEMORY_RESERVATION)
export XIAN_LOCALNET_COMETBFT_MEMORY_SWAP := $(XIAN_LOCALNET_COMETBFT_MEMORY_SWAP)
export XIAN_LOCALNET_COMETBFT_PIDS_LIMIT := $(XIAN_LOCALNET_COMETBFT_PIDS_LIMIT)
export XIAN_LOCALNET_COMETBFT_NOFILE_SOFT := $(XIAN_LOCALNET_COMETBFT_NOFILE_SOFT)
export XIAN_LOCALNET_COMETBFT_NOFILE_HARD := $(XIAN_LOCALNET_COMETBFT_NOFILE_HARD)

ABCI_COMPOSE = XIAN_SERVICE_NODE=0 $(DOCKER_COMPOSE) --profile integrated -f docker-compose-abci.yml
ABCI_BDS_COMPOSE = XIAN_SERVICE_NODE=1 $(DOCKER_COMPOSE) --profile integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml
ABCI_FIDELITY_COMPOSE = XIAN_SERVICE_NODE=0 $(DOCKER_COMPOSE) --profile fidelity -f docker-compose-abci.yml
ABCI_DEV_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml
CONTRACTING_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-contracting.yml
LOCALNET_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-localnet.yml

LOCALNET_NODES ?= 4
LOCALNET_MEMWATCH_MINUTES ?= 10
LOCALNET_LEAK_HUNT_MINUTES ?= 10

.DEFAULT_GOAL := help

.PHONY: help print-env validate smoke smoke-cli prepare-dirs \
	dev-contracting-shell dev-contracting-up dev-contracting-build dev-contracting-down \
	dev-abci-build dev-abci-up dev-abci-down dev-abci-shell \
	abci-build abci-up abci-down abci-fidelity-build abci-fidelity-up abci-fidelity-down dev-base-abci-shell \
	dashboard-build dashboard-up dashboard-down dashboard-bds-up dashboard-bds-down dashboard-fidelity-build dashboard-fidelity-up dashboard-fidelity-down \
	abci-bds-build abci-bds-up abci-bds-down dev-bds-abci-shell \
	wipe-bds node-wipe node-wipe-all node-reset \
	node-stop node-start node-start-bds node-init node-configure node-id \
	node-status node-status-fidelity \
	localnet-init localnet-build localnet-up localnet-down localnet-status \
	localnet-burst localnet-memwatch localnet-leak-hunt \
	localnet-clean localnet-logs localnet-shell

help:
	@printf "Available targets:\n"
	@printf "  %-24s %s\n" "print-env" "Show resolved workspace and data paths"
	@printf "  %-24s %s\n" "validate" "Validate compose topology and required local paths"
	@printf "  %-24s %s\n" "smoke" "Run the smallest real ABCI bring-up and shutdown path"
	@printf "  %-24s %s\n" "smoke-cli" "Run the cross-repo operator flow through xian-cli"
	@printf "  %-24s %s\n" "abci-build" "Build the default integrated node image"
	@printf "  %-24s %s\n" "abci-up" "Start the integrated node runtime container"
	@printf "  %-24s %s\n" "abci-down" "Stop the integrated node runtime container"
	@printf "  %-24s %s\n" "abci-fidelity-build" "Build the split ABCI/CometBFT fidelity images"
	@printf "  %-24s %s\n" "abci-fidelity-up" "Start the split fidelity runtime"
	@printf "  %-24s %s\n" "abci-fidelity-down" "Stop the split fidelity runtime"
	@printf "  %-24s %s\n" "dashboard-build" "Build the optional integrated dashboard image"
	@printf "  %-24s %s\n" "dashboard-up" "Start the optional integrated dashboard service"
	@printf "  %-24s %s\n" "dashboard-down" "Stop the optional integrated dashboard service"
	@printf "  %-24s %s\n" "dashboard-bds-up" "Start the optional dashboard with integrated BDS mode"
	@printf "  %-24s %s\n" "dashboard-bds-down" "Stop the optional dashboard in integrated BDS mode"
	@printf "  %-24s %s\n" "dashboard-fidelity-build" "Build the optional fidelity dashboard image"
	@printf "  %-24s %s\n" "dashboard-fidelity-up" "Start the optional fidelity dashboard service"
	@printf "  %-24s %s\n" "dashboard-fidelity-down" "Stop the optional fidelity dashboard service"
	@printf "  %-24s %s\n" "abci-bds-build" "Build the integrated ABCI + BDS stack"
	@printf "  %-24s %s\n" "abci-bds-up" "Start the integrated ABCI + BDS stack"
	@printf "  %-24s %s\n" "abci-bds-down" "Stop the integrated ABCI + BDS stack"
	@printf "  %-24s %s\n" "node-init" "Initialize the CometBFT home via a helper container"
	@printf "  %-24s %s\n" "node-configure" "Render node config via xian-abci's configure helper"
	@printf "  %-24s %s\n" "node-start/node-stop" "Start or stop the integrated node runtime"
	@printf "  %-24s %s\n" "node-start-bds" "Start the node runtime with block-service mode"
	@printf "  %-24s %s\n" "node-status" "Report integrated runtime state as JSON"
	@printf "  %-24s %s\n" "node-status-fidelity" "Report split fidelity runtime state as JSON"
	@printf "  %-24s %s\n" "dev-abci-build/dev-abci-up" "Developer-only ABCI dev stack targets"
	@printf "  %-24s %s\n" "dev-contracting-build" "Developer-only contracting image build"
	@printf "\n  Localnet (multi-node):\n"
	@printf "  %-24s %s\n" "localnet-init" "Generate localnet assets (set XIAN_LOCALNET_TOPOLOGY=integrated|fidelity)"
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
	@printf "XIAN_COMETBFT_VERSION=%s\n" "$(XIAN_COMETBFT_VERSION)"
	@printf "XIAN_S6_OVERLAY_VERSION=%s\n" "$(XIAN_S6_OVERLAY_VERSION)"
	@printf "XIAN_S6_VERBOSITY=%s\n" "$(XIAN_S6_VERBOSITY)"
	@printf "XIAN_STACK_TOPOLOGY=%s\n" "$(XIAN_STACK_TOPOLOGY)"
	@printf "XIAN_DOCKER_ABCI_MEMORY_LIMIT=%s\n" "$(XIAN_DOCKER_ABCI_MEMORY_LIMIT)"
	@printf "XIAN_DASHBOARD_HOST=%s\n" "$(XIAN_DASHBOARD_HOST)"
	@printf "XIAN_DASHBOARD_PORT=%s\n" "$(XIAN_DASHBOARD_PORT)"
	@printf "XIAN_DOCKER_ABCI_MEMORY_RESERVATION=%s\n" "$(XIAN_DOCKER_ABCI_MEMORY_RESERVATION)"
	@printf "XIAN_DOCKER_ABCI_MEMORY_SWAP=%s\n" "$(XIAN_DOCKER_ABCI_MEMORY_SWAP)"
	@printf "XIAN_DOCKER_ABCI_PIDS_LIMIT=%s\n" "$(XIAN_DOCKER_ABCI_PIDS_LIMIT)"
	@printf "XIAN_DOCKER_ABCI_NOFILE_SOFT=%s\n" "$(XIAN_DOCKER_ABCI_NOFILE_SOFT)"
	@printf "XIAN_DOCKER_ABCI_NOFILE_HARD=%s\n" "$(XIAN_DOCKER_ABCI_NOFILE_HARD)"
	@printf "XIAN_DOCKER_FIDELITY_ABCI_MEMORY_LIMIT=%s\n" "$(XIAN_DOCKER_FIDELITY_ABCI_MEMORY_LIMIT)"
	@printf "XIAN_DOCKER_FIDELITY_ABCI_MEMORY_RESERVATION=%s\n" "$(XIAN_DOCKER_FIDELITY_ABCI_MEMORY_RESERVATION)"
	@printf "XIAN_DOCKER_FIDELITY_ABCI_MEMORY_SWAP=%s\n" "$(XIAN_DOCKER_FIDELITY_ABCI_MEMORY_SWAP)"
	@printf "XIAN_DOCKER_FIDELITY_ABCI_PIDS_LIMIT=%s\n" "$(XIAN_DOCKER_FIDELITY_ABCI_PIDS_LIMIT)"
	@printf "XIAN_DOCKER_FIDELITY_ABCI_NOFILE_SOFT=%s\n" "$(XIAN_DOCKER_FIDELITY_ABCI_NOFILE_SOFT)"
	@printf "XIAN_DOCKER_FIDELITY_ABCI_NOFILE_HARD=%s\n" "$(XIAN_DOCKER_FIDELITY_ABCI_NOFILE_HARD)"
	@printf "XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_LIMIT=%s\n" "$(XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_LIMIT)"
	@printf "XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_RESERVATION=%s\n" "$(XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_RESERVATION)"
	@printf "XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_SWAP=%s\n" "$(XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_SWAP)"
	@printf "XIAN_DOCKER_FIDELITY_COMETBFT_PIDS_LIMIT=%s\n" "$(XIAN_DOCKER_FIDELITY_COMETBFT_PIDS_LIMIT)"
	@printf "XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_SOFT=%s\n" "$(XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_SOFT)"
	@printf "XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_HARD=%s\n" "$(XIAN_DOCKER_FIDELITY_COMETBFT_NOFILE_HARD)"
	@printf "XIAN_DOCKER_DASHBOARD_MEMORY_LIMIT=%s\n" "$(XIAN_DOCKER_DASHBOARD_MEMORY_LIMIT)"
	@printf "XIAN_DOCKER_DASHBOARD_MEMORY_RESERVATION=%s\n" "$(XIAN_DOCKER_DASHBOARD_MEMORY_RESERVATION)"
	@printf "XIAN_DOCKER_DASHBOARD_MEMORY_SWAP=%s\n" "$(XIAN_DOCKER_DASHBOARD_MEMORY_SWAP)"
	@printf "XIAN_DOCKER_DASHBOARD_PIDS_LIMIT=%s\n" "$(XIAN_DOCKER_DASHBOARD_PIDS_LIMIT)"
	@printf "XIAN_DOCKER_DASHBOARD_NOFILE_SOFT=%s\n" "$(XIAN_DOCKER_DASHBOARD_NOFILE_SOFT)"
	@printf "XIAN_DOCKER_DASHBOARD_NOFILE_HARD=%s\n" "$(XIAN_DOCKER_DASHBOARD_NOFILE_HARD)"
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
	@printf "XIAN_LOCALNET_TOPOLOGY=%s\n" "$(XIAN_LOCALNET_TOPOLOGY)"
	@printf "XIAN_LOCALNET_NODE_MEMORY_LIMIT=%s\n" "$(XIAN_LOCALNET_NODE_MEMORY_LIMIT)"
	@printf "XIAN_LOCALNET_NODE_MEMORY_RESERVATION=%s\n" "$(XIAN_LOCALNET_NODE_MEMORY_RESERVATION)"
	@printf "XIAN_LOCALNET_NODE_MEMORY_SWAP=%s\n" "$(XIAN_LOCALNET_NODE_MEMORY_SWAP)"
	@printf "XIAN_LOCALNET_NODE_PIDS_LIMIT=%s\n" "$(XIAN_LOCALNET_NODE_PIDS_LIMIT)"
	@printf "XIAN_LOCALNET_NODE_NOFILE_SOFT=%s\n" "$(XIAN_LOCALNET_NODE_NOFILE_SOFT)"
	@printf "XIAN_LOCALNET_NODE_NOFILE_HARD=%s\n" "$(XIAN_LOCALNET_NODE_NOFILE_HARD)"
	@printf "XIAN_LOCALNET_ABCI_MEMORY_LIMIT=%s\n" "$(XIAN_LOCALNET_ABCI_MEMORY_LIMIT)"
	@printf "XIAN_LOCALNET_ABCI_MEMORY_RESERVATION=%s\n" "$(XIAN_LOCALNET_ABCI_MEMORY_RESERVATION)"
	@printf "XIAN_LOCALNET_ABCI_MEMORY_SWAP=%s\n" "$(XIAN_LOCALNET_ABCI_MEMORY_SWAP)"
	@printf "XIAN_LOCALNET_ABCI_PIDS_LIMIT=%s\n" "$(XIAN_LOCALNET_ABCI_PIDS_LIMIT)"
	@printf "XIAN_LOCALNET_ABCI_NOFILE_SOFT=%s\n" "$(XIAN_LOCALNET_ABCI_NOFILE_SOFT)"
	@printf "XIAN_LOCALNET_ABCI_NOFILE_HARD=%s\n" "$(XIAN_LOCALNET_ABCI_NOFILE_HARD)"
	@printf "XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT=%s\n" "$(XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT)"
	@printf "XIAN_LOCALNET_COMETBFT_MEMORY_RESERVATION=%s\n" "$(XIAN_LOCALNET_COMETBFT_MEMORY_RESERVATION)"
	@printf "XIAN_LOCALNET_COMETBFT_MEMORY_SWAP=%s\n" "$(XIAN_LOCALNET_COMETBFT_MEMORY_SWAP)"
	@printf "XIAN_LOCALNET_COMETBFT_PIDS_LIMIT=%s\n" "$(XIAN_LOCALNET_COMETBFT_PIDS_LIMIT)"
	@printf "XIAN_LOCALNET_COMETBFT_NOFILE_SOFT=%s\n" "$(XIAN_LOCALNET_COMETBFT_NOFILE_SOFT)"
	@printf "XIAN_LOCALNET_COMETBFT_NOFILE_HARD=%s\n" "$(XIAN_LOCALNET_COMETBFT_NOFILE_HARD)"

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
	$(ABCI_DEV_COMPOSE) exec abci /bin/bash


# Runtime container commands
abci-build: prepare-dirs
	$(ABCI_COMPOSE) build --no-cache abci

abci-up: prepare-dirs
	$(ABCI_COMPOSE) up -d abci

abci-down:
	$(ABCI_COMPOSE) down --remove-orphans

abci-fidelity-build: prepare-dirs
	$(ABCI_FIDELITY_COMPOSE) build --no-cache abci-app cometbft

abci-fidelity-up: prepare-dirs
	$(ABCI_FIDELITY_COMPOSE) up -d abci-app cometbft

abci-fidelity-down:
	$(ABCI_FIDELITY_COMPOSE) down --remove-orphans

dashboard-build: prepare-dirs
	$(DOCKER_COMPOSE) --profile integrated --profile dashboard-integrated -f docker-compose-abci.yml build --no-cache dashboard

dashboard-up: prepare-dirs
	$(DOCKER_COMPOSE) --profile integrated --profile dashboard-integrated -f docker-compose-abci.yml up -d --build abci dashboard

dashboard-down:
	$(DOCKER_COMPOSE) --profile integrated --profile dashboard-integrated -f docker-compose-abci.yml rm -sf dashboard

dashboard-bds-up: prepare-dirs
	$(DOCKER_COMPOSE) --profile integrated --profile dashboard-integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml up -d --build abci postgres postgraphile dashboard

dashboard-bds-down:
	$(DOCKER_COMPOSE) --profile integrated --profile dashboard-integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml rm -sf dashboard

dashboard-fidelity-build: prepare-dirs
	$(DOCKER_COMPOSE) --profile fidelity --profile dashboard-fidelity -f docker-compose-abci.yml build --no-cache dashboard-fidelity

dashboard-fidelity-up: prepare-dirs
	$(DOCKER_COMPOSE) --profile fidelity --profile dashboard-fidelity -f docker-compose-abci.yml up -d --build abci-app cometbft dashboard-fidelity

dashboard-fidelity-down:
	$(DOCKER_COMPOSE) --profile fidelity --profile dashboard-fidelity -f docker-compose-abci.yml rm -sf dashboard-fidelity

dev-base-abci-shell:
	$(ABCI_COMPOSE) run --rm --no-deps --entrypoint /bin/bash abci


# ABCI BDS Commands
abci-bds-build: prepare-dirs
	$(ABCI_BDS_COMPOSE) build --no-cache abci postgres postgraphile

abci-bds-up: prepare-dirs
	$(ABCI_BDS_COMPOSE) up -d

abci-bds-down:
	$(ABCI_BDS_COMPOSE) down --remove-orphans

dev-bds-abci-shell:
	$(ABCI_BDS_COMPOSE) run --rm --no-deps --entrypoint /bin/bash abci

wipe-bds:
	rm -rf "$(XIAN_BDS_DATA_DIR)"/*


# Node runtime commands
node-wipe:
	$(ABCI_COMPOSE) run --rm --no-deps --entrypoint /bin/bash abci -lc "rm -rf /root/.cometbft/xian && cometbft unsafe-reset-all"

node-wipe-all: wipe-bds node-wipe

node-reset: node-wipe node-init

node-stop:
	$(ABCI_COMPOSE) down --remove-orphans

node-start:
	$(ABCI_COMPOSE) up -d abci

node-start-bds:
	$(ABCI_BDS_COMPOSE) up -d

node-init:
	$(ABCI_COMPOSE) run --rm --no-deps --entrypoint cometbft abci init

node-configure:
	$(ABCI_COMPOSE) run --rm --no-deps --entrypoint /bin/bash abci -lc "xian-configure-node ${CONFIGURE_ARGS}"

node-id:
	$(ABCI_COMPOSE) run --rm --no-deps --entrypoint cometbft abci show-node-id

node-status:
	@./scripts/node-status.sh

node-status-fidelity:
	@XIAN_STACK_TOPOLOGY=fidelity ./scripts/node-status.sh


# ── Localnet (multi-node) ────────────────────────────────────────────

localnet-init:
	uv run --project "$(XIAN_ABCI_DIR)" python3 ./scripts/localnet-init.py --nodes $(LOCALNET_NODES) --topology $(XIAN_LOCALNET_TOPOLOGY) --clean

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
	@service="$$(python3 -c 'import json, pathlib; data=json.loads(pathlib.Path(".localnet/network.json").read_text()); print("node-0-abci" if data.get("topology") == "fidelity" else "node-0")')"; \
	$(LOCALNET_COMPOSE) exec "$$service" /bin/bash

localnet-clean: localnet-down
	rm -rf .localnet
	rm -f docker-compose-localnet.yml
