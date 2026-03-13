DOCKER_COMPOSE ?= docker compose

XIAN_ABCI_DIR ?= ../xian-abci
XIAN_CONFIGS_DIR ?= ../xian-configs
XIAN_CONTRACTING_DIR ?= ../xian-contracting
XIAN_PY_DIR ?= ../xian-py
XIAN_COMETBFT_HOME ?= ./.cometbft
XIAN_BDS_DATA_DIR ?= ./.bds.db
XIAN_CONTRACTS_DIR ?= ./contracts

export XIAN_ABCI_DIR := $(abspath $(XIAN_ABCI_DIR))
export XIAN_CONFIGS_DIR := $(abspath $(XIAN_CONFIGS_DIR))
export XIAN_CONTRACTING_DIR := $(abspath $(XIAN_CONTRACTING_DIR))
export XIAN_PY_DIR := $(abspath $(XIAN_PY_DIR))
export XIAN_COMETBFT_HOME := $(abspath $(XIAN_COMETBFT_HOME))
export XIAN_BDS_DATA_DIR := $(abspath $(XIAN_BDS_DATA_DIR))
export XIAN_CONTRACTS_DIR := $(abspath $(XIAN_CONTRACTS_DIR))

ABCI_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-abci.yml
ABCI_BDS_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-abci.yml -f docker-compose-abci-bds.yml
ABCI_DEV_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-abci.yml -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml
CONTRACTING_COMPOSE = $(DOCKER_COMPOSE) -f docker-compose-contracting.yml

.DEFAULT_GOAL := help

.PHONY: help print-env validate smoke prepare-dirs \
	dev-contracting-shell dev-contracting-up dev-contracting-build dev-contracting-down \
	dev-abci-build dev-abci-up dev-abci-down dev-abci-shell \
	abci-build abci-up abci-down dev-base-abci-shell \
	abci-bds-build abci-bds-up abci-bds-down dev-bds-abci-shell \
	wipe-bds node-wipe node-wipe-all node-reset \
	node-stop node-start node-start-bds node-init node-configure node-id

help:
	@printf "Available targets:\n"
	@printf "  %-24s %s\n" "print-env" "Show resolved workspace and data paths"
	@printf "  %-24s %s\n" "validate" "Validate compose topology and required local paths"
	@printf "  %-24s %s\n" "smoke" "Run the smallest real ABCI bring-up and shutdown path"
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
	@printf "  %-24s %s\n" "dev-abci-build/dev-abci-up" "Developer-only ABCI dev stack targets"
	@printf "  %-24s %s\n" "dev-contracting-build" "Developer-only contracting image build"

print-env:
	@printf "XIAN_ABCI_DIR=%s\n" "$(XIAN_ABCI_DIR)"
	@printf "XIAN_CONFIGS_DIR=%s\n" "$(XIAN_CONFIGS_DIR)"
	@printf "XIAN_CONTRACTING_DIR=%s\n" "$(XIAN_CONTRACTING_DIR)"
	@printf "XIAN_PY_DIR=%s\n" "$(XIAN_PY_DIR)"
	@printf "XIAN_COMETBFT_HOME=%s\n" "$(XIAN_COMETBFT_HOME)"
	@printf "XIAN_BDS_DATA_DIR=%s\n" "$(XIAN_BDS_DATA_DIR)"
	@printf "XIAN_CONTRACTS_DIR=%s\n" "$(XIAN_CONTRACTS_DIR)"

validate:
	./scripts/validate-stack.sh

smoke:
	./scripts/smoke-stack.sh

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
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && python -m xian.tools.configure ${CONFIGURE_ARGS}"

node-id:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make node-id"
