#!/bin/bash
# RoCE v2 Capability and Configuration Checker for RHEL
# SMCs HPC Toolkit

set -euo pipefail

function section() {
  echo -e "\n==== $1 ===="
}

section "RoCE v2 Capability and Configuration Report"

# 1. Check for RDMA-capable NICs (Mellanox, etc.)
section "[1] RDMA-capable NICs:"
lspci | grep -iE 'Mellanox|Ethernet controller' || echo "  No Mellanox/Ethernet RDMA NICs found."

# 2. Check for required kernel modules
section "[2] Kernel Modules:"
for mod in mlx5_core rdma_ucm rdma_cm ib_uverbs ib_core; do
  lsmod | grep -q $mod && echo "  $mod loaded" || echo "  $mod NOT loaded"
done

# 3. Check for RoCE v2 support in drivers
section "[3] RoCE v2 Driver Support:"
if modinfo mlx5_core 2>/dev/null | grep -q roce; then
  echo "  mlx5_core supports RoCE"
else
  echo "  mlx5_core does NOT support RoCE or not installed"
fi

# 4. List RDMA interfaces
section "[4] RDMA Interfaces:"
if command -v rdma &>/dev/null; then
  rdma link show
else
  echo "  'rdma' tool not found (part of rdma-core)"
fi

# 5. Check for RoCE v2 mode
section "[5] RoCE v2 Mode:"
for dev in /sys/class/infiniband/*; do
  [ -d "$dev" ] || continue
  devname=$(basename "$dev")
  echo "  Device: $devname"
  for port in "$dev"/ports/*; do
    portnum=$(basename "$port")
    roce_mode_file="$port/roce_mode"
    if [ -f "$roce_mode_file" ]; then
      mode=$(cat "$roce_mode_file")
      echo "    Port $portnum: RoCE mode = $mode"
      if [ "$mode" != "2" ]; then
        echo "      (Needs to be set to 2 for RoCE v2)"
      fi
    else
      echo "    Port $portnum: No roce_mode file (may not support RoCE)"
    fi
  done

done

# 6. Check sysctl and network settings
section "[6] Sysctl and Network Settings:"
sysctl -a | grep -E 'rdma|mlx5|infiniband' || echo "  No relevant sysctl settings found."

# 7. Check firewall for UDP 4791 (RoCE v2 default port)
section "[7] Firewall Rules (UDP 4791):"
if command -v firewall-cmd &>/dev/null; then
  firewall-cmd --list-ports | grep -q 4791/udp && echo "  UDP 4791 open" || echo "  UDP 4791 NOT open"
else
  echo "  firewall-cmd not found"
fi

# 8. Summarize and recommend changes
section "[8] Recommendations:"
# (This section would be filled in by logic above, e.g., if RoCE mode != 2, suggest how to set it)
echo "  - If any kernel modules are missing, load them with 'modprobe <module>'"
echo "  - If RoCE mode is not 2, set it with: echo 2 > /sys/class/infiniband/<dev>/ports/<port>/roce_mode"
echo "  - Ensure UDP port 4791 is open in the firewall"
echo "  - For persistent changes, update driver options in /etc/modprobe.d/ or /etc/rdma/rdma.conf"
echo "  - For Mellanox, use 'mstconfig' or 'mlxconfig' to set RoCE mode in firmware if needed"

echo -e "\n=== End of Report ==="
