This fully integrated, production-ready Python script combines **Fabric/Node Health Auditing**, **OSU / Perftest Micro-benchmarking**, an **Interactive HTML Dashboard with Chart.js**, and a **Zero-Dependency Prometheus Exporter** (both as an HTTP endpoint and a Node Exporter `.prom` textfile writer).

---

### How to Run

#### 1. Quick Audit & HTML Dashboard Only

Runs fabric checks, scans local/discovered Slurm nodes for PCIe/packet drops, and builds an HTML dashboard:

```bash
python3 hpc_network_audit_v2.py --html-out cluster_audit.html

```

#### 2. Full Audit with OSU Benchmarks & Prometheus `.prom` File

Includes inter-node point-to-point bandwidth/latency testing (`osu_bw`/`ib_write_bw`) and writes metrics to a node-exporter textfile folder:

```bash
python3 hpc_network_audit_v2.py \
  --nodes node01 node02 node03 node04 \
  --run-benchmarks \
  --prom-out /var/lib/node_exporter/textfile_collector/hpc_network.prom

```

#### 3. Continuous Prometheus Exporter Daemon Mode

Runs the audit and leaves an HTTP server running on port `9100` so Prometheus can scrape `http://<head-node>:9100/metrics`:

```bash
python3 hpc_network_audit_v2.py --run-benchmarks --prom-port 9100 --daemon

```

---

### Exposed Prometheus Metrics Reference

```text
# HELP hpc_fabric_error_ports_total Number of switch ports reporting hardware errors.
hpc_fabric_error_ports_total 0

# HELP hpc_node_pcie_degraded Number of PCIe devices operating at degraded link speeds.
hpc_node_pcie_degraded{node="node01"} 0
hpc_node_pcie_degraded{node="node02"} 1

# HELP hpc_node_rx_dropped_packets Total dropped RX packets on node network interfaces.
hpc_node_rx_dropped_packets{node="node01"} 0

# HELP hpc_network_bandwidth_gbps Inter-node peak network bandwidth in Gbps.
hpc_network_bandwidth_gbps{pair="node01_to_node02"} 384.5

# HELP hpc_network_latency_microseconds Point-to-point network latency in microseconds.
hpc_network_latency_microseconds{pair="node01_to_node02"} 1.25

```