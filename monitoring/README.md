# Monitoring

## Purpose

This folder contains the Prometheus and Grafana assets for the optional Xian
monitoring stack.

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
- The dashboards and alert rules are intentionally aligned with the operator and
  monitoring profiles used across `xian-configs`, `xian-cli`, and
  `xian-deploy`.
- `shared-network` is the current profile-specific monitoring posture.
  Local indexed nodes use the overview, VM runtime, and BDS recovery dashboards
  without a separate profile variant.
- Runtime monitoring is part of the main operator surface now, so VM panels live
  here with the rest of the stack monitoring assets.
- Alertmanager is not provisioned by default, but the example routing file is
  kept here so operators can route runtime alerts separately from BDS recovery
  warnings without inventing their own alert taxonomy first.
