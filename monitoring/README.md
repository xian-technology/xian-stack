# Monitoring

## Purpose

This folder contains the Prometheus and Grafana assets for the optional Xian
monitoring stack.

## Contents

- `prometheus/`: scrape configuration and rule files
- `prometheus/rules/`: alert presets aligned with runtime posture
- `grafana/`: dashboards and provisioning assets
  - `xian-vm-runtime.json` is the dedicated native/shadow rollout view
  - the overview and preset dashboards also carry VM summary panels

## Notes

- These assets are optional, but they are part of the validated operator path.
- The dashboards and alert rules are intentionally aligned with the operator and
  monitoring profiles used across `xian-configs`, `xian-cli`, and
  `xian-deploy`.
- `embedded-backend` and `shared-network` are the main current monitoring
  postures.
- VM rollout monitoring is part of the main operator surface now, so mismatch
  alerts and VM panels live here with the rest of the stack monitoring assets.
