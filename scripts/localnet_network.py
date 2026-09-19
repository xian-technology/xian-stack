"""Stable Docker addresses for disposable E2E network partitions."""

from __future__ import annotations

import ipaddress
import json
import uuid

from localnet_chain_checks import command, require, write_config


async def pin_localnet_addresses(compose_path):
    """Let Docker choose an unused subnet, then pin every service before startup.

    CometBFT resolves persistent peer names at startup. Releasing an endpoint
    during a partition must not let a restarting peer steal its address.
    Explicit IPAM also permits reconnecting with --ip on older Docker Engines.
    """
    compose = json.loads(compose_path.read_text())
    network = compose["networks"]["localnet"]
    allocation = network.get("ipam", {}).get("config")
    if not allocation:
        reservation = "xian-e2e-subnet-" + uuid.uuid4().hex
        await command("docker", "network", "create", reservation)
        try:
            info = json.loads(await command("docker", "network", "inspect", reservation))[0]
            allocation = [
                {"subnet": item["Subnet"], "gateway": item["Gateway"]}
                for item in info["IPAM"]["Config"]
                if ":" not in item["Subnet"]
            ]
        finally:
            await command("docker", "network", "rm", reservation)
        network["ipam"] = {"config": allocation}
    ipv4 = next(item for item in allocation if ":" not in item["subnet"])
    subnet = ipaddress.ip_network(ipv4["subnet"])
    gateway = ipaddress.ip_address(ipv4.get("gateway", str(subnet.network_address + 1)))
    addresses = (address for address in subnet.hosts() if address != gateway)
    assigned = {}
    for name, service in compose["services"].items():
        networks = service.get("networks", [])
        if "localnet" not in networks:
            continue
        if isinstance(networks, list):
            networks = {name: {} for name in networks}
            service["networks"] = networks
        address = next(addresses, None)
        require(address is not None, "Localnet subnet has too few addresses")
        networks["localnet"] = {**(networks["localnet"] or {}), "ipv4_address": str(address)}
        assigned[name] = str(address)
    write_config(compose_path, json.dumps(compose, indent=2) + "\n")
    return {"subnet": str(subnet), "addresses": assigned}
