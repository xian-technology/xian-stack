CONTRACTING_BRANCH ?= mainnet
ABCI_BRANCH ?= mainnet

# ::: Xian Stack Setup & Git Commands
# ::: For setting up the xian-abci and xian-contracting repositories and pulling the latest changes

setup:
	git submodule sync xian-abci xian-contracting
	git submodule update --init xian-abci xian-contracting
	cd xian-abci && git fetch && git checkout $(ABCI_BRANCH) && git pull
	cd xian-contracting && git fetch && git checkout $(CONTRACTING_BRANCH) && git pull
	mkdir -p ./.bds.db

pull:
	cd xian-abci && git pull
	cd xian-contracting && git pull

checkout:
	cd xian-abci && git fetch && git checkout $(ABCI_BRANCH) && git pull
	cd xian-contracting && git fetch && git checkout $(CONTRACTING_BRANCH) && git pull


# ::: Contracting Dev Commands
# ::: For developing on / running tests on the xian-contracting package
contracting-dev-shell:
	make contracting-dev-up

contracting-dev-up:
	docker compose -f docker-compose-contracting.yml up -d
	docker compose -f docker-compose-contracting.yml exec contracting /bin/bash

contracting-dev-build:
	docker compose -f docker-compose-contracting.yml build


contracting-dev-down:
	docker compose -f docker-compose-contracting.yml down


# ::: ABCI Dev Commands
# ::: For developing on / running tests on the xian-abci package

abci-dev-build:
	docker compose -f docker-compose-abci.yml -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml build --no-cache

abci-dev-up:
	docker compose -f docker-compose-abci.yml -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml up -d

abci-dev-down:
	docker compose -f docker-compose-abci.yml -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml down

abci-dev-shell:
	make abci-dev-up
	docker compose -f docker-compose-abci.yml -f docker-compose-abci-dev.yml exec -w /usr/src/app/xian-abci abci /bin/bash

# ::: ABCI Commands
# ::: For running a xian-node

abci-build:
	docker compose -f docker-compose-abci.yml build --no-cache

abci-up:
	docker compose -f docker-compose-abci.yml up -d

abci-down:
	docker compose -f docker-compose-abci.yml down

abci-shell:
	make abci-up
	docker compose -f docker-compose-abci.yml exec -w /usr/src/app/xian-abci abci /bin/bash

# ::: ABCI BDS Commands
# ::: For running a xian-node with Blockchain Data Service enabled

abci-bds-build:
	docker compose -f docker-compose-abci.yml -f docker-compose-abci-bds.yml build --no-cache

abci-bds-up:
	docker compose -f docker-compose-abci.yml -f docker-compose-abci-bds.yml up -d

abci-bds-down:
	docker compose -f docker-compose-abci.yml -f docker-compose-abci-bds.yml down

abci-bds-shell:
	make abci-bds-up
	docker compose -f docker-compose-abci.yml -f docker-compose-abci-bds.yml exec -w /usr/src/app/xian-abci abci /bin/bash

wipe-bds:
	rm -rf ./.bds.db/*

# ::: ABCI Node Commands
# ::: For interacting with cometbft / xian abci running inside a container
# ::: container must be UP, see make commands abci-dev-up / abci-up / abci-bds-up

wipe:
	docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -c "cd xian-abci && make wipe"

wipe-all:
	make wipe-bds
	make wipe

dwu:
	docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -c "cd xian-abci && make dwu"

down:
	docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -c "cd xian-abci && make down"

up:
	docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -c "cd xian-abci && make up"

up-bds:
	docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -c "cd xian-abci && make up-bds"

init:
	docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -c "cd xian-abci && make init"


# '--moniker some-node-moniker --genesis-file-name genesis-devnet.json --validator-privkey priv_key --seed-node <seed_ip> --copy-genesis --service-node'

configure:
	docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -c "cd xian-abci/src/xian/tools/ && python configure.py ${CONFIGURE_ARGS}"

node-id:
	docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -c "cd xian-abci && make node-id"
