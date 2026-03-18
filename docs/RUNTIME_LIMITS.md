# Runtime Limits

`xian-contracting` should not enforce process RSS directly. For a blockchain
runtime, that is the wrong layer because RSS varies across hosts, allocators,
and background process state. Hard resource limits belong at the runtime
boundary.

## Recommended Policy

Use the right control plane for the deployment mode:

- Docker and localnet: enforce memory, swap, PID, and file-descriptor limits in
  Compose.
- Native Linux: enforce limits with `systemd`.
- Native macOS: use `launchd` for restart and descriptor policy, but prefer
  Docker Desktop when you need a hard memory boundary.

Do not stack every mechanism at once. Pick one primary supervisor per
deployment mode.

## Docker And Localnet

`xian-stack` now sets sensible defaults and keeps them configurable through
environment variables:

- `XIAN_DOCKER_ABCI_MEMORY_LIMIT=2048m`
- `XIAN_DOCKER_ABCI_MEMORY_RESERVATION=1024m`
- `XIAN_DOCKER_ABCI_MEMORY_SWAP=2048m`
- `XIAN_DOCKER_ABCI_PIDS_LIMIT=512`
- `XIAN_DOCKER_ABCI_NOFILE_SOFT=65536`
- `XIAN_DOCKER_ABCI_NOFILE_HARD=65536`
- `XIAN_DOCKER_FIDELITY_ABCI_MEMORY_LIMIT=1536m`
- `XIAN_DOCKER_FIDELITY_COMETBFT_MEMORY_LIMIT=768m`
- `XIAN_DOCKER_POSTGRES_MEMORY_LIMIT=1024m`
- `XIAN_DOCKER_POSTGRAPHILE_MEMORY_LIMIT=768m`
- `XIAN_LOCALNET_NODE_MEMORY_LIMIT=1536m`
- `XIAN_LOCALNET_ABCI_MEMORY_LIMIT=1024m`
- `XIAN_LOCALNET_COMETBFT_MEMORY_LIMIT=512m`

The same pattern applies to reservations, swap, PID caps, and `nofile` limits
for BDS, split-runtime, and localnet services.

This gives you:

- deterministic enforcement inside the Linux container runtime
- a single override surface for operators and CI
- a clean separation between consensus logic and host policy

On macOS, these limits apply inside Docker Desktop's Linux VM. You still need
to size Docker Desktop itself with enough total memory for the number of nodes
you intend to run.

## Native Linux

If you run the node directly on Linux without Docker, use `systemd`. This is
the native equivalent of the Compose policy:

```ini
[Unit]
Description=Xian Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/xian/xian-abci
Environment=XIAN_CONFIGS_DIR=/opt/xian/xian-configs
ExecStart=/opt/xian/.venv/bin/python -m xian.cli.run_node
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536
TasksMax=512
MemoryHigh=1.5G
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

Recommended Linux defaults:

- `LimitNOFILE=65536`
- `TasksMax=512`
- `MemoryHigh` below the hard cap so the process gets throttled before the kill
- `MemoryMax` as the hard bound
- `Restart=on-failure` rather than ad hoc shell loops

If the node is inside a container on Linux, prefer the container limits and do
not duplicate `MemoryMax` inside the container host unless you are applying a
machine-wide policy on top.

## Native macOS

`launchd` is the right process supervisor on macOS for restarts and descriptor
policy, but it does not give you a cgroup-style hard memory ceiling comparable
to Linux `MemoryMax`. That is why Docker Desktop is the better default when you
need hard memory enforcement on macOS.

Minimal `launchd` example:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>io.xian.node</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Users/operator/.local/bin/python</string>
      <string>-m</string>
      <string>xian.cli.run_node</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/operator/xian/xian-abci</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>XIAN_CONFIGS_DIR</key>
      <string>/Users/operator/xian/xian-configs</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>SoftResourceLimits</key>
    <dict>
      <key>NumberOfFiles</key>
      <integer>65536</integer>
    </dict>
    <key>HardResourceLimits</key>
    <dict>
      <key>NumberOfFiles</key>
      <integer>65536</integer>
    </dict>
  </dict>
</plist>
```

Use that for restart behavior and `nofile`, but rely on Docker Desktop if you
need a hard memory cap on macOS.

## Diagnostics

Memory diagnostics still belong in operator tooling. For the stack, that means
the localnet helpers such as:

- `scripts/localnet-memwatch.py`
- `scripts/localnet-leak-hunt.py`

Those tools are appropriate because they observe the runtime from the outside.
They should not decide transaction validity.
