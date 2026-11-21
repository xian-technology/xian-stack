# Monitoring

## Purpose

This folder contains the Prometheus and Grafana assets for the optional Xian
monitoring stack.

The Stack release workflow publishes these shared assets as the signed
`xian-monitoring.tar` release asset with a SHA-256 checksum. `xian-deploy`
installs that bundle and renders host-specific configuration from its
inventory and node profile. To package the same assets for a local rehearsal,
run `python3 scripts/package_monitoring.py /absolute/output-directory` from
the Stack checkout.

## Contents

- `alertmanager/`: example alert routing variants for the shipped Prometheus
  alerts
- `prometheus/`: scrape configuration and rule files
- `prometheus/rules/`: alert variants aligned with runtime posture
- `grafana/`: dashboards and provisioning assets
  - `xian-vm-runtime.json` is the dedicated VM runtime view
  - `xian-bds-recovery.json` is the dedicated BDS catch-up and recovery view
  - the overview and profile-specific dashboards also carry VM summary panels

## Notes

- These assets are optional, but they are part of the validated operator path.
- The dashboards and alert rules are intentionally aligned with the operator
  profiles used across `xian-configs`, `xian-cli`, and `xian-deploy`.
- `shared-network` is the current profile-specific monitoring posture.
  Local indexed nodes use the overview, VM runtime, and BDS recovery dashboards
  without a separate profile variant.
- Runtime monitoring is part of the main operator surface, so VM panels live
  here with the rest of the stack monitoring assets.
- Alertmanager is not provisioned by default, but the example routing file is
  kept here so operators can route runtime alerts separately from BDS recovery
  warnings without inventing their own alert taxonomy first.
- `node-exporter` starts with Prometheus and Grafana. It reads the host root
  read-only, publishes no port, and is scraped as job `xian_node` for the disk
  alerts.

## Alerts

`prometheus/rules/shared-network.yml` holds the shared-network alerts.
`prometheus/rules/block-cadence.yml` holds the CometBFT committed-height stall
alert for policies that produce empty blocks.
`integrated.yml` (used by `make monitoring-up` and `backend.py start
--monitoring`) loads both files. Remote deployments load the shared-network
rules for the `shared_network` profile and add the cadence rule only for
`idle_interval` and `periodic`.

| Alert | Fires when | Severity |
| --- | --- | --- |
| `XianBlockProductionStalled` | CometBFT's committed height has not moved for 5 minutes | critical |
| `XianNoPeers` | 0 connected peers for 2 minutes | critical |
| `XianPeersLow` | fewer than 2 peers for 10 minutes | warning |
| `XianValidatorNotSigning` | this node's validator last signed more than 10 blocks below the committed height for 2 minutes | critical |
| `XianValidatorMissedBlocks` | this node's validator missed 5 or more commits in 10 minutes | warning |
| `XianDiskSpaceCritical` / `XianDiskSpaceLow` | a real filesystem has less than 5% (5 min) / 15% (10 min) free | critical / warning |
| `XianDiskFillingUp` | under 25% free and empty within 24 hours at the 6-hour rate | warning |

Plus the target, node-info, app block-progress and BDS alerts already in the
file. The validator alerts read CometBFT's `validator_last_signed_height` and
`validator_missed_blocks`, which CometBFT exports only for the node's own key,
so they stay silent on non-validators. The stall alert assumes a block policy
that produces empty blocks (`idle_interval` or `periodic`, as in the shared
network manifests); on an `on_demand` chain an idle period also stops the
height.

`./scripts/test-monitoring-rules.sh` (part of `make validate`) runs
`promtool check` and the unit tests in `prometheus/tests/`, and checks the
example Alertmanager file with `amtool`. Add a test case there with every rule
change.

## Delivering Alerts With Alertmanager

Prometheus evaluates the rules and shows them on its Alerts page, but notifies
nobody until it has an Alertmanager. Nothing here points at an external
service; you supply the receivers.

1. Copy `alertmanager/alertmanager.example.yml` and replace the example
   webhook receivers with your own (`webhook_configs`, `email_configs`,
   `slack_configs`, `pagerduty_configs`, ...). Check it with
   `amtool check-config alertmanager.yml`.
2. Run Alertmanager on the monitoring network, for example in a compose file
   of your own next to `docker-compose-monitoring.yml`:

   ```yaml
   services:
     alertmanager:
       profiles: ["monitoring"]
       image: prom/alertmanager:v0.34.2@sha256:<digest>
       command: ["--config.file=/etc/alertmanager/alertmanager.yml"]
       volumes:
         - /srv/xian/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
       networks:
         - xian-net
   ```

   Keep its port unpublished, or bind it to `127.0.0.1` and put
   authentication in front of it, like Prometheus and Grafana.
3. Add an `alerting` section to the Prometheus config you run (your copy of
   `integrated.yml`, which already loads
   the rules under `/etc/prometheus/rules/`):

   ```yaml
   alerting:
     alertmanagers:
       - static_configs:
           - targets:
               - alertmanager:9093
   ```

4. Restart Prometheus and confirm that `http://127.0.0.1:9090/api/v1/alertmanagers`
   lists the Alertmanager as active. Send a test alert with
   `amtool alert add XianTestAlert --alertmanager.url=http://alertmanager:9093`
   from a container on the same network and check that your receiver gets it.
