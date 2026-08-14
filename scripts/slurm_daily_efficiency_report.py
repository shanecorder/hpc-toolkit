#!/usr/bin/env python3
"""
Slurm Daily Efficiency Reporter
Runs daily as root to parse yesterday's completed jobs per user,
generates a custom efficiency breakdown, and writes to ~/.share/seff/
"""

import sys
import os
import pwd
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from collections import defaultdict

# Minimum UID to consider (prevents generating reports for system daemons)
MIN_UID = 1000

def get_date_range(target_date=None):
    """Returns (start_str, end_str, display_str) for the query period."""
    if target_date is None:
        target_date = datetime.now() - timedelta(days=1)
    
    start_str = target_date.strftime("%Y-%m-%d") + "T00:00:00"
    end_str = target_date.strftime("%Y-%m-%d") + "T23:59:59"
    display_str = target_date.strftime("%Y-%m-%d")
    return start_str, end_str, display_str

def get_yesterday_jobs(start_str, end_str):
    """Queries sacct for completed/failed/cancelled jobs in the window."""
    cmd = [
        "sacct", "-a", "-X", "--parsable2", "--noheader",
        f"--starttime={start_str}",
        f"--endtime={end_str}",
        "--format=JobID,User,JobName,State"
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"Error querying sacct: {e.stderr}\n")
        return {}

    user_jobs = defaultdict(list)
    for line in res.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            job_id, user, job_name, state = parts[0], parts[1], parts[2], parts[3]
            if not user or user == "root":
                continue
            # Filter out job array sub-steps or interactive steps if -X missed any
            if "." not in job_id:
                user_jobs[user].append({
                    "job_id": job_id,
                    "job_name": job_name,
                    "state": state
                })
    return user_jobs

def parse_seff_output(job_id):
    """Runs seff <job_id> and extracts CPU and Memory metrics."""
    cmd = ["seff", str(job_id)]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        return None

    output = res.stdout
    metrics = {
        "cores": "N/A",
        "cpu_eff": None,
        "mem_req": "N/A",
        "mem_used": "N/A",
        "mem_eff": None
    }

    # Regex extractors
    cores_m = re.search(r"Cores:\s+(\d+)", output)
    if cores_m:
        metrics["cores"] = cores_m.group(1)

    cpu_eff_m = re.search(r"CPU Efficiency:\s+([\d\.]+)%", output)
    if cpu_eff_m:
        metrics["cpu_eff"] = float(cpu_eff_m.group(1))

    mem_req_m = re.search(r"Memory Efficiency:\s+[\d\.]+% of ([\d\.]+\s+[A-Za-z]+)", output)
    if mem_req_m:
        metrics["mem_req"] = mem_req_m.group(1)

    mem_used_m = re.search(r"Memory Utilized:\s+([\d\.]+\s+[A-Za-z]+)", output)
    if mem_used_m:
        metrics["mem_used"] = mem_used_m.group(1)

    mem_eff_m = re.search(r"Memory Efficiency:\s+([\d\.]+)%", output)
    if mem_eff_m:
        metrics["mem_eff"] = float(mem_eff_m.group(1))

    return metrics

def calculate_rating(avg_cpu, avg_mem):
    """Computes overall efficiency and categorizes it."""
    overall_score = (avg_cpu + avg_mem) / 2.0
    if overall_score >= 80.0:
        return overall_score, "EXCELLENT"
    elif overall_score >= 50.0:
        return overall_score, "GOOD"
    else:
        return overall_score, "BAD (Needs Improvement)"

def generate_recommendations(avg_cpu, avg_mem, rating):
    """Produces targeted advice based on where the resource bottlenecks are."""
    tips = []
    
    if rating == "EXCELLENT":
        tips.append("• Your jobs are utilizing assigned resources exceptionally well! Keep it up.")
        return tips

    if avg_mem < 50.0:
        tips.append("• Memory Underutilization Detected:")
        tips.append("  - You requested significantly more RAM than your jobs consumed.")
        tips.append("  - Action: Lower '--mem' or '--mem-per-cpu' in your sbatch script to match actual peak usage.")
        tips.append("  - Benefit: Smaller memory footprints reduce queue wait time and backfill faster.")

    if avg_cpu < 50.0:
        tips.append("• CPU / Core Underutilization Detected:")
        tips.append("  - Multiple cores were requested, but were mostly idle during execution.")
        tips.append("  - Action: If running single-threaded software (e.g., standard Python/R scripts), use '--cpus-per-task=1'.")
        tips.append("  - Action: If using multithreading, verify OMP_NUM_THREADS matches your requested '--cpus-per-task'.")
        tips.append("  - Benefit: Frees cluster slots and allows higher throughput for your job arrays.")

    if avg_cpu >= 50.0 and avg_mem >= 50.0:
        tips.append("• Moderate efficiency across your workload.")
        tips.append("  - Review the per-job table above and adjust walltimes/cores on outliers.")

    return tips

def build_report_text(username, date_str, job_data_list, avg_cpu, avg_mem, overall_score, rating):
    """Formats the ASCII report table and advisory text."""
    lines = []
    lines.append("=" * 80)
    lines.append(f" SLURM DAILY JOB EFFICIENCY REPORT | User: {username} | Date: {date_str}")
    lines.append("=" * 80)
    lines.append("")
    
    # Table Header
    header = f"{'JobID':<12} {'State':<12} {'Cores':<6} {'CPU Eff%':<10} {'Req Mem':<12} {'Used Mem':<12} {'Mem Eff%':<10}"
    lines.append(header)
    lines.append("-" * len(header))

    for job in job_data_list:
        cpu_eff_str = f"{job['cpu_eff']:.1f}%" if job['cpu_eff'] is not None else "N/A"
        mem_eff_str = f"{job['mem_eff']:.1f}%" if job['mem_eff'] is not None else "N/A"
        lines.append(
            f"{job['job_id']:<12} "
            f"{job['state'][:11]:<12} "
            f"{job['cores']:<6} "
            f"{cpu_eff_str:<10} "
            f"{job['mem_req']:<12} "
            f"{job['mem_used']:<12} "
            f"{mem_eff_str:<10}"
        )

    lines.append("-" * len(header))
    lines.append("")
    lines.append(f"Summary Metrics (Total Jobs: {len(job_data_list)}):")
    lines.append(f"  • Average CPU Efficiency:    {avg_cpu:.1f}%")
    lines.append(f"  • Average Memory Efficiency: {avg_mem:.1f}%")
    lines.append(f"  • Combined Efficiency Score: {overall_score:.1f}%")
    lines.append(f"  • Overall Rating:            [{rating}]")
    lines.append("")
    lines.append("Optimization Recommendations:")
    
    tips = generate_recommendations(avg_cpu, avg_mem, rating)
    for tip in tips:
        lines.append(f"  {tip}")

    lines.append("")
    lines.append("=" * 80)
    return "\n".join(lines) + "\n"

def deliver_report(username, report_content):
    """Writes report to user's ~/.share/seff/ directory with safe permissions."""
    try:
        user_info = pwd.getpwnam(username)
    except KeyError:
        return

    # Skip system users
    if user_info.pw_uid < MIN_UID:
        return

    home_dir = user_info.pw_dir
    if not os.path.exists(home_dir):
        return

    seff_dir = os.path.join(home_dir, ".share", "seff")
    report_file = os.path.join(seff_dir, "yesterday_report.txt")

    try:
        # Create directory with 0700 permissions
        os.makedirs(seff_dir, mode=0o700, exist_ok=True)
        os.chown(seff_dir, user_info.pw_uid, user_info.pw_gid)

        # Write report file with 0600 permissions
        with open(report_file, "w") as f:
            f.write(report_content)

        os.chmod(report_file, 0o600)
        os.chown(report_file, user_info.pw_uid, user_info.pw_gid)
    except OSError as e:
        sys.stderr.write(f"Failed writing report for {username}: {e}\n")

def main():
    start_str, end_str, display_str = get_date_range()
    user_jobs = get_yesterday_jobs(start_str, end_str)

    if not user_jobs:
        return

    for username, jobs in user_jobs.items():
        processed_jobs = []
        cpu_scores = []
        mem_scores = []

        for j in jobs:
            metrics = parse_seff_output(j["job_id"])
            if metrics is None:
                continue

            j_data = {
                "job_id": j["job_id"],
                "state": j["state"],
                "cores": metrics["cores"],
                "cpu_eff": metrics["cpu_eff"],
                "mem_req": metrics["mem_req"],
                "mem_used": metrics["mem_used"],
                "mem_eff": metrics["mem_eff"]
            }
            processed_jobs.append(j_data)

            if metrics["cpu_eff"] is not None:
                cpu_scores.append(metrics["cpu_eff"])
            if metrics["mem_eff"] is not None:
                mem_scores.append(metrics["mem_eff"])

        if not processed_jobs:
            continue

        avg_cpu = sum(cpu_scores) / len(cpu_scores) if cpu_scores else 0.0
        avg_mem = sum(mem_scores) / len(mem_scores) if mem_scores else 0.0
        overall_score, rating = calculate_rating(avg_cpu, avg_mem)

        report_text = build_report_text(
            username=username,
            date_str=display_str,
            job_data_list=processed_jobs,
            avg_cpu=avg_cpu,
            avg_mem=avg_mem,
            overall_score=overall_score,
            rating=rating
        )

        deliver_report(username, report_text)

if __name__ == "__main__":
    main()