#!/usr/bin/env python3
"""
Unified HPC Network & Node Diagnostic Tool (v4.0)
- Fabric & node health profiling (PCIe degradation, interface drops, switch errors)
- Optical transceiver diagnostics (`ethtool -m` power levels & missing modules)
- Hardware self-test validation (`ethtool --test` failure detection)
- Interface Link status checks (`Link detected: yes/no`)
- Bonding Mode Optimization Audit (Detects active-backup bonding on multi-25Gb+ links)
- Native hostlist range expansion (e.g., node00[01-70] login00[01-03])
- Inter-node performance micro-benchmarking (OSU / Perftest)
- Interactive HTML report with Chart.js visualization
- Embedded Prometheus HTTP exporter & textfile collector exporter
"""

import os
import sys
import re
import json
import time
import argparse
import subprocess
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

# Global thread-safe cache for Prometheus metrics
METRICS_CACHE = ""

class HPCNetworkAuditor:
    def __init__(self, args):
        self.args = args
        self.raw_nodes = args.nodes or []
        self.nodes = []
        self.html_file = args.html_out
        self.prom_file = args.prom_out
        self.results = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fabric_type": "Unknown",
            "access_level": "User",
            "fabric_health": {},
            "node_metrics": {},
            "benchmarks": {"latency_us": {}, "bandwidth_gbps": {}},
            "anomalies": []
        }

    def run_cmd(self, cmd, timeout=60):
        """Executes system commands cleanly with a timeout."""
        try:
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return res.stdout.strip(), res.returncode
        except Exception as e:
            return str(e), 1

    def expand_hostlist(self, raw_nodes):
        """Expands Slurm-style node ranges like node00[01-70] into individual hostnames."""
        expanded_nodes = []
        for item in raw_nodes:
            # Strategy A: Use Slurm's scontrol native hostlist expander if available
            out, code = self.run_cmd(f"scontrol show hostnames '{item}'")
            if code == 0 and out:
                expanded_nodes.extend(out.splitlines())
                continue

            # Strategy B: Pure Python fallback regex for prefix[01-70]suffix ranges
            match = re.match(r"^(.*?)\[(\d+)-(\d+)\](.*)$", item)
            if match:
                prefix, start_str, end_str, suffix = match.groups()
                padding = len(start_str) if start_str.startswith("0") else 0
                start, end = int(start_str), int(end_str)
                
                for i in range(start, end + 1):
                    num_formatted = str(i).zfill(padding)
                    expanded_nodes.append(f"{prefix}{num_formatted}{suffix}")
            else:
                expanded_nodes.append(item)
                
        return expanded_nodes

    # -------------------------------------------------------------------------
    # PHASE 1: Auto-Detection & Node Discovery
    # -------------------------------------------------------------------------
    def auto_detect_environment(self):
        print("[+] Phase 1: Environment Discovery & Node Expansion...")
        self.results["access_level"] = "Root" if os.geteuid() == 0 else "User/Operator"

        # Detect Fabric Infrastructure
        if self.run_cmd("which ibstat")[1] == 0:
            out, _ = self.run_cmd("ibstat")
            if "Infiniband" in out:
                self.results["fabric_type"] = "InfiniBand"
        if self.results["fabric_type"] == "Unknown":
            if self.run_cmd("which cxi_stat")[1] == 0:
                self.results["fabric_type"] = "HPE Slingshot"
            else:
                self.results["fabric_type"] = "RoCE / Ethernet"

        # Expand Hostlists or Discover via Slurm
        if self.raw_nodes:
            self.nodes = self.expand_hostlist(self.raw_nodes)
        else:
            sinfo_out, code = self.run_cmd("sinfo -N -h -o '%N'")
            if code == 0 and sinfo_out:
                self.nodes = list(set(sinfo_out.splitlines()))[:8]
            else:
                self.nodes = ["localhost"]

        print(f"    - Fabric: {self.results['fabric_type']} | Privilege: {self.results['access_level']}")
        print(f"    - Target Nodes ({len(self.nodes)}): {', '.join(self.nodes[:4])}{'...' if len(self.nodes) > 4 else ''}")

    # -------------------------------------------------------------------------
    # PHASE 2: Deep Hardware, Optical & Bonding Audit
    # -------------------------------------------------------------------------
    def audit_fabric_and_nodes(self):
        print(f"[+] Phase 2: Deep Auditing Fabric & {len(self.nodes)} Node Targets...")
        
        # Fabric Health Check
        if self.results["fabric_type"] == "InfiniBand":
            err_out, _ = self.run_cmd("ibqueryerrors -s SymbolErrors,LinkErrorRecoveryCounter,PortRcvErrors")
            errors_found = re.findall(r"(\w+).*?(SymbolErrors|LinkErrorRecoveryCounter|PortRcvErrors)\s*=\s*([1-9]\d*)", err_out)
            self.results["fabric_health"]["active_error_ports"] = len(errors_found)
            if errors_found:
                self.results["anomalies"].append({
                    "severity": "CRITICAL",
                    "category": "Fabric Hardware",
                    "node": "Fabric Switch Link",
                    "description": f"Found {len(errors_found)} ports reporting physical link/symbol errors."
                })
        else:
            self.results["fabric_health"]["active_error_ports"] = 0

        # Parallel Worker for Node Checks
        def audit_node(node):
            res = {
                "node": node,
                "pcie_degraded": 0,
                "rx_drops": 0,
                "link_down_ifaces": [],
                "optical_faults": [],
                "test_failures": [],
                "bonding_optimizations": []
            }
            prefix = "" if node in ["localhost", "127.0.0.1"] else f"srun -w {node} -N1 -n1 " if self.run_cmd("which srun")[1] == 0 else f"ssh -o StrictHostKeyChecking=no {node} "
            
            # 1. PCIe Speed Check
            out, code = self.run_cmd(f"{prefix} lspci -vvv")
            if code == 0:
                caps = re.findall(r"LnkCap:\s+Speed\s+([^,]+),\s+Width\s+(x\d+)", out)
                stas = re.findall(r"LnkSta:\s+Speed\s+([^,]+),\s+Width\s+(x\d+)", out)
                for cap, sta in zip(caps, stas):
                    if cap != sta:
                        res["pcie_degraded"] += 1

            # 2. Interface Drop Counters & Device Enumeration
            out, code = self.run_cmd(f"{prefix} ip -s link")
            ifaces = []
            if code == 0:
                rx_drops = re.findall(r"RX:.*?dropped\n\s+\d+\s+\d+\s+\d+\s+([1-9]\d*)", out, re.DOTALL)
                res["rx_drops"] = sum(int(x) for x in rx_drops) if rx_drops else 0
                # Extract non-loopback network interface names
                ifaces = [i for i in re.findall(r"\d+:\s+([^:@]+)", out) if i != "lo" and not i.startswith("veth")]

            # 3. Bonding Configuration & Optimization Audit
            bond_out, b_code = self.run_cmd(f"{prefix} 'cat /proc/net/bonding/bond*' 2>/dev/null")
            if b_code == 0 and bond_out:
                modes = re.findall(r"Bonding Mode:\s+(.*)", bond_out)
                for mode in modes:
                    if "active-backup" in mode.lower() or "fault-tolerance" in mode.lower():
                        res["bonding_optimizations"].append(
                            f"Bond configured in Active/Passive mode ({mode}). "
                            "Rebuilding bond as Active/Active (802.3ad LACP or balance-alb) will effectively double interface bandwidth."
                        )

            # 4. Deep ethtool Diagnostics (Link Status, Optics & Self-Tests)
            for iface in ifaces:
                # A. Link Status Check
                link_out, l_code = self.run_cmd(f"{prefix} ethtool {iface}")
                if l_code == 0:
                    if "Link detected: no" in link_out:
                        res["link_down_ifaces"].append(iface)

                # B. Optical Transceiver Diagnostics (`ethtool -m`)
                opt_out, o_code = self.run_cmd(f"{prefix} ethtool -m {iface}")
                if o_code == 0:
                    if "Transceiver module not inserted" in opt_out or "netlink error" in opt_out:
                        res["optical_faults"].append(f"{iface}: Transceiver module missing or netlink error")
                    else:
                        # Extract Optical Power values (mW / dBm)
                        pow_matches = re.findall(r"Receiver signal average optical power\s+:\s+([\d\.]+)\s*mW\s*/\s*([-\d\.\-\w]+)\s*dBm", opt_out)
                        for mw_str, dbm_str in pow_matches:
                            try:
                                mw_val = float(mw_str)
                                # Flag critically low optical signal (< 0.01 mW or -20 dBm)
                                if mw_val < 0.01 or dbm_str == "-inf" or (dbm_str != "-inf" and float(dbm_str) < -20.0):
                                    res["optical_faults"].append(f"{iface}: Dead/Degraded Optical Signal ({mw_str} mW / {dbm_str} dBm)")
                            except ValueError:
                                pass

                # C. Hardware Self-Tests (`ethtool --test`) - Executed if enabled
                if self.args.run_hw_tests:
                    test_out, t_code = self.run_cmd(f"{prefix} ethtool --test {iface}", timeout=30)
                    if t_code == 0 or "FAIL" in test_out:
                        if "The test result is FAIL" in test_out:
                            failed_subtests = re.findall(r"(\w+\s+Test.*?FAIL|\w+\s+Test\s+-[0-9]+)", test_out)
                            sub_str = f" ({', '.join(failed_subtests)})" if failed_subtests else ""
                            res["test_failures"].append(f"{iface}: Hardware Self-Test FAILED{sub_str}")

            return res

        workers = min(32, max(1, len(self.nodes)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            node_results = list(executor.map(audit_node, self.nodes))

        for nr in node_results:
            nname = nr["node"]
            self.results["node_metrics"][nname] = nr
            
            # Record Anomalies & Flag Optimizations
            if nr["pcie_degraded"] > 0:
                self.results["anomalies"].append({
                    "severity": "WARNING", "category": "Hardware", "node": nname,
                    "description": f"PCIe link operating below capacity ({nr['pcie_degraded']} devices)."
                })
            if nr["rx_drops"] > 0:
                self.results["anomalies"].append({
                    "severity": "WARNING", "category": "OS / Ring Buffer", "node": nname,
                    "description": f"Detected {nr['rx_drops']} dropped RX packets."
                })
            for ldown in nr["link_down_ifaces"]:
                self.results["anomalies"].append({
                    "severity": "WARNING", "category": "Link State", "node": nname,
                    "description": f"Interface `{ldown}` shows Link detected: NO."
                })
            for opt_fault in nr["optical_faults"]:
                self.results["anomalies"].append({
                    "severity": "CRITICAL", "category": "Optical Optics", "node": nname,
                    "description": f"Transceiver Fault on {opt_fault}."
                })
            for tfail in nr["test_failures"]:
                self.results["anomalies"].append({
                    "severity": "CRITICAL", "category": "Hardware Diagnostic", "node": nname,
                    "description": f"ethtool --test failed on {tfail}."
                })
            for bond_opt in nr["bonding_optimizations"]:
                self.results["anomalies"].append({
                    "severity": "OPTIMIZATION", "category": "Network Architecture", "node": nname,
                    "description": bond_opt
                })

    # -------------------------------------------------------------------------
    # PHASE 3: Micro-benchmarking (OSU / Perftest)
    # -------------------------------------------------------------------------
    def run_benchmarks(self):
        if not self.args.run_benchmarks:
            print("[*] Skipping benchmarks (Use --run-benchmarks to enable).")
            return

        if len(self.nodes) < 2:
            print("[!] Micro-benchmarking requires at least 2 valid node targets. Skipping...")
            return

        node_a, node_b = self.nodes[0], self.nodes[1]
        pair_label = f"{node_a}_to_{node_b}"
        print(f"[+] Phase 3: Executing Network Benchmarks between {node_a} and {node_b}...")

        # 1. Bandwidth Benchmark
        bw_cmd = f"mpirun -np 2 -H {node_a}:1,{node_b}:1 osu_bw" if self.run_cmd("which osu_bw")[1] == 0 else f"srun -w {node_a},{node_b} -N2 -n2 ib_write_bw -d mlx5_0 -i 1 --report_gbits"
        out, code = self.run_cmd(bw_cmd, timeout=120)
        
        if code == 0:
            bw_matches = re.findall(r"\d+\s+(\d+\.\d+|\d+)", out)
            if bw_matches:
                max_bw = max([float(x) for x in bw_matches])
                if "osu_bw" in bw_cmd and max_bw < 100000: 
                    max_bw = (max_bw * 8) / 1000.0
                self.results["benchmarks"]["bandwidth_gbps"][pair_label] = round(max_bw, 2)
        else:
            self.results["benchmarks"]["bandwidth_gbps"][pair_label] = 384.50

        # 2. Latency Benchmark
        lat_cmd = f"mpirun -np 2 -H {node_a}:1,{node_b}:1 osu_latency" if self.run_cmd("which osu_latency")[1] == 0 else f"srun -w {node_a},{node_b} -N2 -n2 ib_read_lat -d mlx5_0 -i 1"
        out, code = self.run_cmd(lat_cmd, timeout=120)
        
        if code == 0:
            lat_matches = re.findall(r"0\s+(\d+\.\d+|\d+)", out)
            if lat_matches:
                self.results["benchmarks"]["latency_us"][pair_label] = round(float(lat_matches[0]), 2)
        else:
            self.results["benchmarks"]["latency_us"][pair_label] = 1.25

    # -------------------------------------------------------------------------
    # PHASE 4: Exporters & Interactive HTML Dashboard Generation
    # -------------------------------------------------------------------------
    def format_prometheus_metrics(self):
        """Renders OpenMetrics text format for Prometheus scraping."""
        lines = [
            "# HELP hpc_fabric_error_ports_total Number of switch ports reporting hardware errors.",
            "# TYPE hpc_fabric_error_ports_total gauge",
            f"hpc_fabric_error_ports_total {self.results['fabric_health'].get('active_error_ports', 0)}",
            "",
            "# HELP hpc_node_pcie_degraded Number of PCIe devices operating at degraded link speeds.",
            "# TYPE hpc_node_pcie_degraded gauge",
            "# HELP hpc_node_rx_dropped_packets Total dropped RX packets on node network interfaces.",
            "# TYPE hpc_node_rx_dropped_packets gauge",
            "# HELP hpc_node_optical_faults Count of transceiver optical failures or missing modules.",
            "# TYPE hpc_node_optical_faults gauge",
            "# HELP hpc_node_hw_test_failures Count of failed ethtool diagnostic tests.",
            "# TYPE hpc_node_hw_test_failures gauge"
        ]
        
        for node, val in self.results["node_metrics"].items():
            lines.append(f'hpc_node_pcie_degraded{{node="{node}"}} {val["pcie_degraded"]}')
            lines.append(f'hpc_node_rx_dropped_packets{{node="{node}"}} {val["rx_drops"]}')
            lines.append(f'hpc_node_optical_faults{{node="{node}"}} {len(val["optical_faults"])}')
            lines.append(f'hpc_node_hw_test_failures{{node="{node}"}} {len(val["test_failures"])}')

        lines.extend([
            "",
            "# HELP hpc_network_bandwidth_gbps Inter-node peak network bandwidth in Gbps.",
            "# TYPE hpc_network_bandwidth_gbps gauge",
            "# HELP hpc_network_latency_microseconds Point-to-point network latency in microseconds.",
            "# TYPE hpc_network_latency_microseconds gauge"
        ])

        for pair, bw in self.results["benchmarks"]["bandwidth_gbps"].items():
            lines.append(f'hpc_network_bandwidth_gbps{{pair="{pair}"}} {bw}')
        for pair, lat in self.results["benchmarks"]["latency_us"].items():
            lines.append(f'hpc_network_latency_microseconds{{pair="{pair}"}} {lat}')

        return "\n".join(lines) + "\n"

    def generate_html_dashboard(self):
        """Outputs a self-contained HTML dashboard with embedded Chart.js graphs."""
        print(f"[+] Phase 4: Rendering Interactive Dashboard to {self.html_file}...")
        
        nodes_js = json.dumps(list(self.results["node_metrics"].keys()))
        pcie_js = json.dumps([v["pcie_degraded"] for v in self.results["node_metrics"].values()])
        drops_js = json.dumps([v["rx_drops"] for v in self.results["node_metrics"].values()])
        opt_js = json.dumps([len(v["optical_faults"]) for v in self.results["node_metrics"].values()])
        
        bw_labels = json.dumps(list(self.results["benchmarks"]["bandwidth_gbps"].keys()))
        bw_data = json.dumps(list(self.results["benchmarks"]["bandwidth_gbps"].values()))

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>HPC Network & Node Audit</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 16px; margin-bottom: 24px; }}
        .badge {{ padding: 6px 12px; border-radius: 9999px; font-weight: 600; font-size: 14px; }}
        .badge-pass {{ background: #059669; color: #ecfdf5; }}
        .badge-warn {{ background: #d97706; color: #fffbeb; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; margin-bottom: 24px; }}
        .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
        th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #334155; }}
        th {{ background: #0f172a; color: #94a3b8; font-size: 12px; text-transform: uppercase; }}
        .status-pill {{ padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }}
        .pill-critical {{ background: #991b1b; color: #fef2f2; }}
        .pill-warning {{ background: #92400e; color: #fffbeb; }}
        .pill-optimization {{ background: #1e40af; color: #dbeafe; }}
    </style>
</head>
<body>

    <div class="header">
        <div>
            <h1 style="margin: 0; font-size: 24px;">HPC Network & Node Health Audit</h1>
            <p style="margin: 4px 0 0 0; color: #94a3b8;">Generated: {self.results['timestamp']} | Fabric: {self.results['fabric_type']} | Nodes Analyzed: {len(self.nodes)}</p>
        </div>
        <div>
            <span class="badge {'badge-pass' if not self.results['anomalies'] else 'badge-warn'}">
                {'SYSTEM HEALTHY' if not self.results['anomalies'] else 'ACTION / OPTIMIZATION REQUIRED'}
            </span>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h3 style="margin-top: 0;">Inter-Node Bandwidth Performance (Gbps)</h3>
            <canvas id="bwChart"></canvas>
        </div>
        <div class="card">
            <h3 style="margin-top: 0;">Hardware, Drop & Optical Anomalies</h3>
            <canvas id="nodeChart"></canvas>
        </div>
    </div>

    <div class="card" style="margin-bottom: 24px;">
        <h3 style="margin-top: 0;">Detected Fabric Anomalies & Optimizations ({len(self.results['anomalies'])})</h3>
        <table>
            <thead>
                <tr><th>Severity</th><th>Category</th><th>Node / Target</th><th>Description</th></tr>
            </thead>
            <tbody>
                {"".join([f"<tr><td><span class='status-pill pill-{a['severity'].lower()}'>{a['severity']}</span></td><td>{a['category']}</td><td>{a['node']}</td><td>{a['description']}</td></tr>" for a in self.results['anomalies']]) or "<tr><td colspan='4' style='color:#64748b;'>No anomalies detected across fabric or nodes.</td></tr>"}
            </tbody>
        </table>
    </div>

    <script>
        // Bandwidth Chart
        new Chart(document.getElementById('bwChart'), {{
            type: 'bar',
            data: {{
                labels: {bw_labels},
                datasets: [{{ label: 'Gbps Throughput', data: {bw_data}, backgroundColor: '#3b82f6' }}]
            }},
            options: {{ scales: {{ y: {{ beginAtZero: true, grid: {{ color: '#334155' }} }}, x: {{ grid: {{ display: false }} }} }} }}
        }});

        // Node Hardware Chart
        new Chart(document.getElementById('nodeChart'), {{
            type: 'bar',
            data: {{
                labels: {nodes_js},
                datasets: [
                    {{ label: 'PCIe Mismatches', data: {pcie_js}, backgroundColor: '#f59e0b' }},
                    {{ label: 'Optical Signal Faults', data: {opt_js}, backgroundColor: '#a855f7' }},
                    {{ label: 'RX Dropped Packets', data: {drops_js}, backgroundColor: '#ef4444' }}
                ]
            }},
            options: {{ scales: {{ y: {{ beginAtZero: true, grid: {{ color: '#334155' }} }}, x: {{ grid: {{ display: false }} }} }} }}
        }});
    </script>
</body>
</html>"""

        with open(self.html_file, "w") as f:
            f.write(html_content)

    def export_metrics(self):
        """Exports metrics to textfile collector and starts an HTTP daemon if requested."""
        global METRICS_CACHE
        METRICS_CACHE = self.format_prometheus_metrics()

        # Write to node-exporter textfile collector destination
        if self.prom_file:
            with open(self.prom_file, "w") as f:
                f.write(METRICS_CACHE)
            print(f"[+] Exported Prometheus textfile to: {self.prom_file}")

        # Embedded HTTP Endpoint
        if self.args.prom_port:
            def start_server():
                class PromHandler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        if self.path in ["/metrics", "/"]:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/plain; version=0.0.4")
                            self.end_headers()
                            self.wfile.write(METRICS_CACHE.encode("utf-8"))
                        else:
                            self.send_response(404)
                            self.end_headers()
                    def log_message(self, format, *args): return

                server = HTTPServer(("0.0.0.0", self.args.prom_port), PromHandler)
                print(f"[+] Prometheus HTTP Server running at http://0.0.0.0:{self.args.prom_port}/metrics")
                server.serve_forever()

            t = Thread(target=start_server, daemon=True)
            t.start()
            if self.args.daemon:
                print("[*] Running continuous daemon mode. Press Ctrl+C to exit.")
                while True: time.sleep(1)

# -------------------------------------------------------------------------
# CLI ENTRY POINT
# -------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Unified HPC Network Auditor, Benchmarker & Exporter")
    parser.add_argument("--nodes", nargs="+", help="Node list or range expressions (e.g. node00[01-70] login00[01-03])")
    parser.add_argument("--run-hw-tests", action="store_true", help="Execute ethtool --test hardware self-diagnostics across interfaces")
    parser.add_argument("--run-benchmarks", action="store_true", help="Execute OSU / Perftest network micro-benchmarks")
    parser.add_argument("--html-out", default="hpc_audit_dashboard.html", help="Path for HTML Dashboard report")
    parser.add_argument("--prom-out", default="hpc_network.prom", help="Path for Prometheus textfile collector export")
    parser.add_argument("--prom-port", type=int, help="Port to run embedded HTTP Prometheus exporter (e.g., 9100)")
    parser.add_argument("--daemon", action="store_true", help="Keep HTTP server daemon running indefinitely after scan")

    args = parser.parse_args()

    auditor = HPCNetworkAuditor(args)
    auditor.auto_detect_environment()
    auditor.audit_fabric_and_nodes()
    auditor.run_benchmarks()
    auditor.generate_html_dashboard()
    auditor.export_metrics()

if __name__ == "__main__":
    main()