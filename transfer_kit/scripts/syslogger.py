import time
import json
import urllib.request
import csv

# We use standard library to avoid missing psutil in some Orange Pi images, 
# but if psutil is available it's better. We'll try to import it, else read /proc.
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

def get_cpu_ram():
    if HAS_PSUTIL:
        return psutil.cpu_percent(interval=None), psutil.virtual_memory().percent
    else:
        # Fallback reading /proc/stat and /proc/meminfo
        try:
            with open('/proc/stat', 'r') as f:
                lines = f.readlines()
                cpu_line = lines[0].split()[1:]
                idle = float(cpu_line[3])
                total = sum(float(x) for x in cpu_line)
                # This is a bit tricky without keeping state, just approximate or return 0
        except:
            pass
        return 0, 0

def get_cpu_temp_freq():
    temp = 0.0
    freq = 0.0
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp = float(f.read().strip()) / 1000.0
    except:
        pass
    try:
        with open('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 'r') as f:
            freq = float(f.read().strip()) / 1000.0
    except:
        pass
    return temp, freq

def main():
    if HAS_PSUTIL:
        psutil.cpu_percent(interval=0.1) # Initialize
        
    out_file = '/tmp/syslog.csv'
    print(f"Starting syslogger to {out_file}...")
    
    with open(out_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['host_time', 'live_velocity', 'max_accel', 'cpu_load', 'ram_load', 'cpu_temp', 'cpu_freq'])
        
        while True:
            t = time.monotonic()
            cpu, ram = get_cpu_ram()
            cpu_temp, cpu_freq = get_cpu_temp_freq()
            
            live_vel = 0.0
            max_accel = 0.0
            
            try:
                # Query Moonraker API
                req = urllib.request.Request("http://localhost:7125/printer/objects/query?toolhead&motion_report")
                with urllib.request.urlopen(req, timeout=0.5) as response:
                    data = json.loads(response.read().decode())
                    status = data.get('result', {}).get('status', {})
                    if 'motion_report' in status:
                        live_vel = status['motion_report'].get('live_velocity', 0.0)
                    if 'toolhead' in status:
                        max_accel = status['toolhead'].get('max_accel', 0.0)
            except Exception as e:
                pass
                
            writer.writerow([f"{t:.6f}", live_vel, max_accel, cpu, ram, cpu_temp, cpu_freq])
            f.flush()
            
            time.sleep(0.1)

if __name__ == '__main__':
    main()
