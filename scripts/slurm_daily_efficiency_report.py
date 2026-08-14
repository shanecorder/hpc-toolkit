#!/usr/bin/env python3
"""
Slurm Daily Efficiency Reporter (Python 3.6+ Compatible)
Queries completed Slurm jobs per user, calculates efficiency metrics via seff,
and writes per-user reports to ~/.local/seff/yesterday_report.txt.
"""

import sys
import os
import pwd
import re
import shutil
import argparse
import subprocess
from datetime import datetime, timedelta
from collections import defaultdict

# Minimum UID to consider (prevents generating reports for system accounts)
MIN_UID = 1000

def get_binary_path(name):
    path = shutil.which(name)
    if not path:
        # Check standard Bright / Slurm installation paths
        for candidate in [
            f"/cm/shared/apps/slurm/current/bin/{name}",
            f"/usr/bin/{name}",
            f"/usr/local/bin/{name}",
            f"/opt/slurm/bin/{name}"
        ]:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return path

def parse_args():
    parser = argparse.ArgumentParser(description="Generate daily Slurm job efficiency reports per user.")
    parser.add_argument("--date", help="Target date in YYYY-MM-DD format (default: yesterday)")
    parser.add_argument("--days-ago", type=int, default=1, help="Number of days ago to report on (default: 1)")
    parser.add_argument("--user", help="Run only for a specific username or UID (for testing)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose diagnostic output")
    return parser.parse_args()

def get_date_range(args):
    """Calculates start and end timestamps for the query window."""
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = datetime.now() - timedelta(days=args.days_ago)

    start_str = target_date.strftime("%Y-%m-%d") + "T00:00:00"
    end_str = target_date.strftime("%Y-%m-%d") + "T23:59:59"
    display_str = target_date.strftime("%Y-%m-%d")
    return start_str, end_str, display_str

def get_jobs(sacct_bin, start_str, end_str, target_user=None, verbose=False):
    """Queries sacct for jobs within the target time window."""
    cmd = [
        sacct_bin, "-a", "-X", "--parsable2", "--noheader",
        f"--starttime={start_str}",
        f"--endtime={end_str}",
        "--format=JobID,User,JobName,State"
    ]
    if target_user:
        cmd.extend(["-u", str(target_user)])

    if verbose:
        print(f"[DEBUG] Executing: {' '.join(cmd)}")

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"[ERROR] sacct query failed: {e.stderr}\n")
        return {}

    user_jobs = defaultdict(list)
    raw_lines = res.stdout.strip().splitlines()
    if verbose:
        print(f"[DEBUG] sacct returned {len(raw_lines)} job records.")

    for line in raw_lines:
        parts = line.split("|")
        if len(parts) >= 4:
            job_id, user, job_name, state = parts[0], parts[1], parts[2], parts[3]
            if not user or user == "root":
                continue
            # Skip job array sub-steps if any slip through
            if "." not in job_id:
                user_jobs[user].append({
                    "job_id": job_id,
                    "job_name": job_name,
                    "state": state
                })
    return user_jobs

def parse_seff_output(seff_bin, job_id, verbose=False):
    """Runs seff on a job ID and extracts CPU and memory utilization metrics."""
    cmd = [seff_bin, str(job_id)]
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )
    if res.returncode != 0:
        if verbose:
            print(f"[DEBUG] seff failed for JobID {job_id}: {res.stderr.strip()}")
        return None

    output = res.stdout
    metrics = {
        "cores": "N/A",
        "cpu_eff": None,
        "mem_req": "N/A",
        "mem_used": "N/A",
        "mem_eff": None
    }

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
    overall_score = (avg_cpu + avg_mem) / 2.0
    if overall_score >= 80.0:
        return overall_score, "EXCELLENT"
    elif overall_score >= 50.0:
        return overall_score, "GOOD"
    else:
        return overall_score, "BAD (Needs Improvement)"

def generate_recommendations(avg_cpu, avg_mem, rating):
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
        tips.append("  - Action: If running single-threaded code (e.g. basic Python/R), use '--cpus-per-task=1'.")
        tips.append("  - Action: If multithreading, verify OMP_NUM_THREADS matches your requested '--cpus-per-task'.")
        tips.append("  - Benefit: Frees cluster slots and improves throughput for subsequent array jobs.")

    if avg_cpu >= 50.0 and avg_mem >= 50.0:
        tips.append("• Moderate efficiency across your workload.")
        tips.append("  - Review the per-job table above and adjust walltimes/cores on outliers.")

    return tips

def build_report_text(username, date_str, job_data_list, avg_cpu, avg_mem, overall_score, rating):
    lines = []
    lines.append("=" * 80)
    lines.append(f" SLURM DAILY JOB EFFICIENCY REPORT | User: {username} | Date: {date_str}")
    lines.append("=" * 80)
    lines.append("")
    
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

def resolve_user_info(username):
    """Resolves passwd entry by username or by UID."""
    try:
        return pwd.getpwnam(str(username))
    except KeyError:
        if str(username).isdigit():
            try:
                return pwd.getpwuid(int(username))
            except KeyError:
                return None
    return None

def deliver_report(username, report_content, verbose=False):
    """Creates ~/.local/seff/ if missing, writes yesterday_report.txt, and sets user ownership."""
    user_info = resolve_user_info(username)
    if not user_info:
        if verbose:
            print(f"[WARN] User '{username}' not found in system passwd/NSS database.")
        return False

    if user_info.pw_uid < MIN_UID:
        if verbose:
            print(f"[INFO] Skipping system user '{username}' (UID: {user_info.pw_uid} < {MIN_UID}).")
        return False

    home_dir = user_info.pw_dir
    if not os.path.exists(home_dir):
        if verbose:
            print(f"[WARN] Home directory '{home_dir}' does not exist for user '{username}'.")
        return False

    local_dir = os.path.join(home_dir, ".local")
    seff_dir = os.path.join(local_dir, "seff")
    report_file = os.path.join(seff_dir, "yesterday_report.txt")

    try:
        # Ensure ~/.local exists
        if not os.path.exists(local_dir):
            os.makedirs(local_dir, mode=0o755, exist_ok=True)
            os.chown(local_dir, user_info.pw_uid, user_info.pw_gid)

        # Ensure ~/.local/seff exists (0700 permissions)
        if not os.path.exists(seff_dir):
            os.makedirs(seff_dir, mode=0o700, exist_ok=True)
            os.chown(seff_dir, user_info.pw_uid, user_info.pw_gid)

        # Write report file (0600 permissions)
        with open(report_file, "w") as f:
            f.write(report_content)

        os.chmod(report_file, 0o600)
        os.chown(report_file, user_info.pw_uid, user_info.pw_gid)
        
        if verbose or sys.stdout.isatty():
            print(f"[SUCCESS] Report written to: {report_file}")
        return True

    except OSError as e:
        sys.stderr.write(f"[ERROR] Failed writing report for '{username}': {e}\n")
        return False

def main():
    args = parse_args()
    is_interactive = sys.stdout.isatty()
    verbose = args.verbose or is_interactive

    sacct_bin = get_binary_path("sacct")
    seff_bin = get_binary_path("seff")

    if not sacct_bin or not seff_bin:
        sys.stderr.write(f"[ERROR] Could not locate 'sacct' ({sacct_bin}) or 'seff' ({seff_bin}). Check PATH.\n")
        sys.exit(1)

    start_str, end_str, display_str = get_date_range(args)

    if verbose:
        print(f"--- Slurm Daily Efficiency Generator ---")
        print(f"Target Date: {display_str} ({start_str} to {end_str})")
        if args.user:
            print(f"Target User: {args.user}")

    user_jobs = get_jobs(sacct_bin, start_str, end_str, target_user=args.user, verbose=args.verbose)

    if not user_jobs:
        if verbose:
            print(f"[INFO] No Slurm jobs found for the specified period ({display_str}). Nothing to generate.")
        return

    generated_count = 0
    for username, jobs in user_jobs.items():
        if verbose:
            print(f"\nProcessing user '{username}' ({len(jobs)} jobs)...")

        processed_jobs = []
        cpu_scores = []
        mem_scores = []

        for j in jobs:
            metrics = parse_seff_output(seff_bin, j["job_id"], verbose=args.verbose)
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
            if verbose:
                print(f"[INFO] No parsable efficiency data from seff for '{username}'.")
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

        if deliver_report(username, report_text, verbose=args.verbose):
            generated_count += 1

    if verbose:
        print(f"\n[DONE] Generated reports for {generated_count} user(s).")

if __name__ == "__main__":
    main()