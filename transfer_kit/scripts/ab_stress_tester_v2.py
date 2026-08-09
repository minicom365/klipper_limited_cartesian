import paramiko
import time
import json
import os
import sys
import getpass
import argparse
from pathlib import Path

# Interactive / CLI Argument Parser for Sanitized Credentials
parser = argparse.ArgumentParser(description="Klipper ClockSync 4x4 Stress Tester")
parser.add_argument("--host", type=str, default=os.getenv("SBC_HOST", ""), help="SBC Hostname or IP (e.g. 192.168.0.7)")
parser.add_argument("--user", type=str, default=os.getenv("SBC_USER", "armbian"), help="SSH Username (e.g. armbian)")
parser.add_argument("--password", type=str, default=os.getenv("SBC_PASSWORD", ""), help="SSH/Sudo Password")
parser.add_argument("--duration", type=int, default=60, help="Phase Duration in seconds (default: 60)")

args, _ = parser.parse_known_args()

hostname = args.host
if not hostname:
    hostname = input("Enter SBC Hostname/IP [192.168.0.7]: ").strip() or "192.168.0.7"

username = args.user
if not username:
    username = input("Enter SSH Username [armbian]: ").strip() or "armbian"

password = args.password
if not password:
    password = getpass.getpass(f"Enter SSH Password for {username}: ")

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
dump_dir = BASE_DIR / "v2_dumps"
local_syslogger_path = SCRIPT_DIR / "syslogger.py"

phase_duration = args.duration # seconds
governors = ["ondemand", "performance"]
phases = ["baseline", "cpu_stress", "io_stress", "combo_stress"]

def get_ssh():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname, username=username, password=password, timeout=10)
    return client

def setup_syslogger(client):
    print("Uploading syslogger.py...", flush=True)
    sftp = client.open_sftp()
    sftp.put(local_syslogger_path, "/tmp/syslogger.py")
    sftp.close()

def set_governor(client, gov):
    print(f"Setting CPU governor to {gov}...", flush=True)
    # Direct write to Armbian cpufreq policy0 (Proven working)
    client.exec_command(f"echo {password} | sudo -S sh -c 'echo {gov} > /sys/devices/system/cpu/cpufreq/policy0/scaling_governor'")
    client.exec_command(f"echo {password} | sudo -S sh -c 'echo {gov} > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'")
    time.sleep(1)

def restart_klipper(client):
    print("Restarting Klipper...", flush=True)
    client.exec_command("rm -f /tmp/clocksync_dump.csv /tmp/syslog.csv")
    client.exec_command(f"echo {password} | sudo -S systemctl stop klipper 2>/dev/null; pkill -f klippy.py 2>/dev/null")
    time.sleep(2)
    client.exec_command(f"echo {password} | sudo -S systemctl start klipper")
    time.sleep(10)

def run_phase(client, phase, motion_mode="high-freq"):
    print(f"Running phase: {phase} for {phase_duration} seconds (Motion Mode: {motion_mode})...", flush=True)
    
    client.exec_command("pkill -f syslogger.py")
    client.exec_command("python3 /tmp/syslogger.py > /dev/null 2>&1 &")
    time.sleep(1)
    
    if phase == "cpu_stress":
        client.exec_command("for i in 1 2 3 4; do dd if=/dev/zero of=/dev/null & done")
    
    elif phase == "io_stress":
        # Write to ~/test_io.img on MicroSD card (9.8GB free space) so RAM disk /tmp is not affected
        client.exec_command("dd if=/dev/zero of=~/test_io.img bs=1M count=2000 oflag=direct > /dev/null 2>&1 &")
        
    elif phase == "combo_stress":
        stress_cmd = 'dd if=/dev/urandom | gzip -1 > /dev/null &'
        client.exec_command(stress_cmd)
        
        print(f"Generating stress.gcode (Motion Mode: {motion_mode})...", flush=True)
        cmd = "cat > ~/printer_data/gcodes/stress.gcode"
        stdin, stdout, stderr = client.exec_command(cmd)
        
        if motion_mode == "low-freq":
            stress_gcode = (
                "SET_KINEMATIC_POSITION X=0 Y=0 Z=50\n"
                "SET_VELOCITY_LIMIT ACCEL=15000 ACCEL_TO_DECEL=15000\n"
                "G91\n"
                + "G1 X25 F36000\nG1 X-50 F36000\nG1 X25 F36000\n" * 5000
            )
        else:
            stress_gcode = (
                "SET_KINEMATIC_POSITION X=0 Y=0 Z=50\n"
                "G91\n"
                + "G1 X0.1 F50000\nG1 X-0.1 F50000\n" * 500000
            )
            
        stdin.write(stress_gcode)
        stdin.write("\n")
        stdin.close()
        stdout.channel.recv_exit_status()
        
        print("Starting virtual high-speed print via Moonraker...", flush=True)
        client.exec_command("curl -s -X POST 'http://localhost:7125/printer/print/start?filename=stress.gcode'")
        
    # Clear dumps right before timer sleep so every phase logs exact duration without setup overhead
    client.exec_command("rm -f /tmp/clocksync_dump.csv /tmp/syslog.csv")
    time.sleep(phase_duration)
    
    client.exec_command("curl -s -X POST 'http://localhost:7125/printer/print/cancel'")
    client.exec_command("pkill -f 'dd if=/dev/urandom'")
    client.exec_command("pkill -f 'dd if=/dev/zero'")
    client.exec_command("pkill -f syslogger.py")
    client.exec_command("rm -f ~/test_io.img /tmp/test_io.img")

def download_dumps(client, gov, mode, phase):
    os.makedirs(dump_dir, exist_ok=True)
    print(f"Downloading dumps for {gov}_{mode}_{phase}...", flush=True)
    sftp = client.open_sftp()
    
    paths = {
        "/tmp/clocksync_dump.csv": f"clocksync_dump_{gov}_{mode}_{phase}.csv",
        "/tmp/syslog.csv": f"syslog_dump_{gov}_{mode}_{phase}.csv"
    }
    
    for remote_path, local_name in paths.items():
        local_path = os.path.join(dump_dir, local_name)
        try:
            sftp.get(remote_path, local_path)
            print(f"Saved to {local_path}", flush=True)
        except Exception as e:
            print(f"Error downloading {remote_path}: {e}", flush=True)
            
    sftp.close()

def main():
    client = get_ssh()
    setup_syslogger(client)
    
    try:
        for gov in governors:
            print(f"\n======================================", flush=True)
            print(f"      STARTING GOVERNOR: {gov.upper()}", flush=True)
            print(f"======================================", flush=True)
            set_governor(client, gov)
            
            for mode in ["high-freq", "low-freq"]:
                for phase in phases:
                    # Optimization: baseline, cpu_stress, and io_stress do not run G-code motion.
                    # We can safely reuse the high-freq run dumps to avoid wasting test time.
                    if mode == "low-freq" and phase in ["baseline", "cpu_stress", "io_stress"]:
                        print(f"Optimizing: copying {gov}_high-freq_{phase} -> {gov}_low-freq_{phase}...", flush=True)
                        for prefix in ["clocksync_dump", "syslog_dump"]:
                            src = os.path.join(dump_dir, f"{prefix}_{gov}_high-freq_{phase}.csv")
                            dst = os.path.join(dump_dir, f"{prefix}_{gov}_low-freq_{phase}.csv")
                            if os.path.exists(src):
                                shutil.copy(src, dst)
                        continue
                        
                    print(f"\n--- Phase: {gov} | {mode} | {phase} ---", flush=True)
                    restart_klipper(client)
                    run_phase(client, phase, motion_mode=mode)
                    download_dumps(client, gov, mode, phase)
                    
    except KeyboardInterrupt:
        print("Interrupted!", flush=True)
    finally:
        # Restore ondemand as a safety measure
        client.exec_command(f"echo {password} | sudo -S cpufreq-set -g ondemand")
        client.exec_command(f"echo {password} | sudo -S sh -c 'echo ondemand | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'")
        client.close()
        print("\nAll V2 phases complete! Cleaned up.", flush=True)

if __name__ == "__main__":
    main()
