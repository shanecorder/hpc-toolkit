#!/bin/bash
set -e

# System Paths
WEB_DIR="/var/www/net-audit"
HIST_DIR="${WEB_DIR}/history"
SCRIPT_PATH="/app/hpc-toolkit/scripts/network_audit.py"  # Adjust to your script's full path
TIMESTAMP=$(date +%Y-%m-%d)

# 1. Ensure target directories exist
mkdir -p "$WEB_DIR" "$HIST_DIR"

# 2. Run the network audit script
python3 "$SCRIPT_PATH" \
  --nodes hpctpa3pc[0001-0070] hpctpa3pl[0001-0003] hpctpa3ph[0001-0002] 10.14.193.185 10.14.193.221 \
  --run-hw-tests \
  --no-eth-check eth0 em2 p3p2 eno \
  --html-out "${WEB_DIR}/hpc_audit_dashboard.html" \
  --prom-out "${WEB_DIR}/hpc_network.prom"

# 3. Create timestamped historical backups
cp "${WEB_DIR}/hpc_audit_dashboard.html" "${HIST_DIR}/hpc_audit_dashboard_${TIMESTAMP}.html"
cp "${WEB_DIR}/hpc_network.prom" "${HIST_DIR}/hpc_network_${TIMESTAMP}.prom"

# 4. Prune older backups — strictly retain only the 4 most recent files
ls -dt "${HIST_DIR}"/hpc_audit_dashboard_*.html 2>/dev/null | tail -n +5 | xargs -r rm -f
ls -dt "${HIST_DIR}"/hpc_network_*.prom 2>/dev/null | tail -n +5 | xargs -r rm -f