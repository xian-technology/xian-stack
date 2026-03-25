# Monitoring

## Purpose

This folder contains the Prometheus and Grafana assets for the optional Xian
monitoring stack.

## Contents

- `prometheus/`: scrape configuration and rule files
- `prometheus/rules/`: alert presets aligned with runtime posture
- `grafana/`: dashboards and provisioning assets

## Notes

- These assets are optional, but they are part of the validated operator path.
- The dashboards and alert rules are intentionally aligned with the operator and
  monitoring profiles used across `xian-configs`, `xian-cli`, and
  `xian-deploy`.
- `embedded-backend` and `shared-network` are the main current monitoring
  postures.
