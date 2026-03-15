#!/usr/bin/env bash
# Show the status of all localnet nodes: block height, peers, voting power.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STACK_DIR="$(dirname "$SCRIPT_DIR")"
NETWORK_JSON="$STACK_DIR/.localnet/network.json"

if [ ! -f "$NETWORK_JSON" ]; then
    echo "ERROR: $NETWORK_JSON not found. Run 'make localnet-init' first." >&2
    exit 1
fi

nodes=$(python3 -c "
import json, sys
with open('$NETWORK_JSON') as f:
    data = json.load(f)
for n in data['nodes']:
    print(f\"{n['moniker']} {n['host_rpc_port']} {n['node_id'][:12]}\")
")

printf "%-10s %-8s %-8s %-5s %s\n" "NODE" "HEIGHT" "PEERS" "VOTE" "STATUS"
printf "%-10s %-8s %-8s %-5s %s\n" "----" "------" "-----" "----" "------"

while IFS=' ' read -r moniker port node_id; do
    url="http://127.0.0.1:${port}/status"
    if result=$(curl -sf --max-time 2 "$url" 2>/dev/null); then
        height=$(echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['result']['sync_info']['latest_block_height'])" 2>/dev/null || echo "?")
        peers=$(echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['result']['node_info']['other']['n_peers'] if 'other' in d['result']['node_info'] else '?')" 2>/dev/null || echo "?")
        voting=$(echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['result']['validator_info']['voting_power'])" 2>/dev/null || echo "?")
        printf "%-10s %-8s %-8s %-5s %s\n" "$moniker" "$height" "$peers" "$voting" "UP"
    else
        printf "%-10s %-8s %-8s %-5s %s\n" "$moniker" "-" "-" "-" "DOWN"
    fi
done <<< "$nodes"
