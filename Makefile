ABCI_BRANCH ?= master
CONTRACTING_BRANCH ?= master
DOCKER_COMPOSE ?= docker compose

XIAN_ABCI_DIR ?= $(if $(wildcard ../xian-abci/pyproject.toml),../xian-abci,xian-abci)
XIAN_CONTRACTING_DIR ?= $(if $(wildcard ../xian-contracting/pyproject.toml),../xian-contracting,xian-contracting)
XIAN_PY_DIR ?= $(if $(wildcard ../xian-py/pyproject.toml),../xian-py,xian-py)
XIAN_COMETBFT_HOME ?= ./.cometbft
XIAN_BDS_DATA_DIR ?= ./.bds.db
XIAN_CONTRACTS_DIR ?= ./contracts

export XIAN_ABCI_DIR := $(abspath $(XIAN_ABCI_DIR))
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

.PHONY: help print-env validate prepare-dirs setup setup-submodules pull checkout \
	contracting-dev-shell contracting-dev-up contracting-dev-build contracting-dev-down \
	abci-dev-build abci-dev-up abci-dev-down abci-dev-shell \
	abci-build abci-up abci-down abci-shell \
	abci-bds-build abci-bds-up abci-bds-down abci-bds-shell \
	wipe-bds wipe wipe-all dwu down up up-bds init configure node-id

help:
	@printf "Available targets:\n"
	@printf "  %-24s %s\n" "print-env" "Show resolved workspace and data paths"
	@printf "  %-24s %s\n" "validate" "Validate compose topology and required local paths"
	@printf "  %-24s %s\n" "setup-submodules" "Sync nested xian-abci and xian-contracting submodules"
	@printf "  %-24s %s\n" "abci-build" "Build the base ABCI image"
	@printf "  %-24s %s\n" "abci-up" "Start the base ABCI container"
	@printf "  %-24s %s\n" "abci-bds-build" "Build the ABCI + BDS image stack"
	@printf "  %-24s %s\n" "abci-bds-up" "Start the ABCI + BDS stack"
	@printf "  %-24s %s\n" "abci-dev-build" "Build the development stack"
	@printf "  %-24s %s\n" "abci-dev-up" "Start the development stack"
	@printf "  %-24s %s\n" "contracting-dev-build" "Build the contracting dev image"
	@printf "  %-24s %s\n" "contracting-dev-up" "Start the contracting dev shell"
	@printf "  %-24s %s\n" "init/configure/up/down" "Proxy CometBFT and node lifecycle commands into xian-abci"

print-env:
	@printf "XIAN_ABCI_DIR=%s\n" "$(XIAN_ABCI_DIR)"
	@printf "XIAN_CONTRACTING_DIR=%s\n" "$(XIAN_CONTRACTING_DIR)"
	@printf "XIAN_PY_DIR=%s\n" "$(XIAN_PY_DIR)"
	@printf "XIAN_COMETBFT_HOME=%s\n" "$(XIAN_COMETBFT_HOME)"
	@printf "XIAN_BDS_DATA_DIR=%s\n" "$(XIAN_BDS_DATA_DIR)"
	@printf "XIAN_CONTRACTS_DIR=%s\n" "$(XIAN_CONTRACTS_DIR)"

validate:
	./scripts/validate-stack.sh

prepare-dirs:
	mkdir -p "$(XIAN_COMETBFT_HOME)" "$(XIAN_BDS_DATA_DIR)" "$(XIAN_CONTRACTS_DIR)"

setup: setup-submodules

setup-submodules: prepare-dirs
	git submodule sync xian-abci xian-contracting
	git submodule update --init xian-abci xian-contracting
	cd xian-abci && git fetch && git checkout $(ABCI_BRANCH) && git pull
	cd xian-contracting && git fetch && git checkout $(CONTRACTING_BRANCH) && git pull

pull:
	cd xian-abci && git pull
	cd xian-contracting && git pull

checkout:
	cd xian-abci && git fetch && git checkout $(ABCI_BRANCH) && git pull
	cd xian-contracting && git fetch && git checkout $(CONTRACTING_BRANCH) && git pull


# Contracting Dev Commands
contracting-dev-shell: contracting-dev-up

contracting-dev-up: prepare-dirs
	$(CONTRACTING_COMPOSE) up -d
	$(CONTRACTING_COMPOSE) exec contracting /bin/bash

contracting-dev-build: prepare-dirs
	$(CONTRACTING_COMPOSE) build

contracting-dev-down:
	$(CONTRACTING_COMPOSE) down


# ABCI Dev Commands
abci-dev-build: prepare-dirs
	$(ABCI_DEV_COMPOSE) build --no-cache

abci-dev-up: prepare-dirs
	$(ABCI_DEV_COMPOSE) up -d

abci-dev-down:
	$(ABCI_DEV_COMPOSE) down

abci-dev-shell: abci-dev-up
	$(ABCI_DEV_COMPOSE) exec -w /usr/src/app/xian-abci abci /bin/bash


# ABCI Commands
abci-build: prepare-dirs
	$(ABCI_COMPOSE) build --no-cache

abci-up: prepare-dirs
	$(ABCI_COMPOSE) up -d

abci-down:
	$(ABCI_COMPOSE) down

abci-shell: abci-up
	$(ABCI_COMPOSE) exec -w /usr/src/app/xian-abci abci /bin/bash


# ABCI BDS Commands
abci-bds-build: prepare-dirs
	$(ABCI_BDS_COMPOSE) build --no-cache

abci-bds-up: prepare-dirs
	$(ABCI_BDS_COMPOSE) up -d

abci-bds-down:
	$(ABCI_BDS_COMPOSE) down

abci-bds-shell: abci-bds-up
	$(ABCI_BDS_COMPOSE) exec -w /usr/src/app/xian-abci abci /bin/bash

wipe-bds:
	rm -rf "$(XIAN_BDS_DATA_DIR)"/*


# ABCI Node Commands
wipe:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make wipe"

wipe-all: wipe-bds wipe

dwu:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make dwu"

down:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make down"

up:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make up"

up-bds:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make up-bds"

init:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make init"

configure:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci/src/xian/tools && python configure.py ${CONFIGURE_ARGS}"

node-id:
	$(ABCI_COMPOSE) exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make node-id"
