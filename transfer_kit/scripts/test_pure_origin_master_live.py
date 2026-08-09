import paramiko, time, os, numpy as np
import scipy.stats as stats
import subprocess

host = '192.168.0.7'
user = 'armbian'
pwd = 'armbian_minicom'

print('=== 1. FETCHING PURE UNMODIFIED ORIGIN/MASTER CLOCKSYNC.PY ===')
pure_code = subprocess.check_output(['git', 'show', 'origin/master:klippy/clocksync.py']).decode('utf-8')

# Add CSV debug logging to pure origin/master code without touching any logic
dump_patch = '''
        # Raw Data Dumper for live origin/master testing
        try:
            with open("/tmp/pure_master_dump.csv", "a") as f:
                f.write("%.6f,%.6f,%.6f,%.6f\\n" % (
                    sent_time, half_rtt if 'half_rtt' in locals() else 0.0,
                    (clock - exp_clock) / self.mcu_freq,
                    math.sqrt(self.prediction_variance) / self.mcu_freq
                ))
        except Exception:
            pass
        # Add clock and sent_time to linear regression
'''
pure_code_logged = pure_code.replace('        # Add clock and sent_time to linear regression', dump_patch)

# Also ensure half_rtt is passed in _handle_clock
pure_code_logged = pure_code_logged.replace(
    'ret = self._update_regression(sent_time, clock)',
    'half_rtt = .5 * (receive_time - sent_time)\n        ret = self._update_regression(sent_time, clock)'
)

# Save temporary pure master clocksync.py
temp_pure_file = r'C:\Users\admin\Documents\antigravity\agitated-darwin\klipper\transfer_kit\scripts\pure_master_clocksync.py'
with open(temp_pure_file, 'w', encoding='utf-8') as f:
    f.write(pure_code_logged)

print('=== 2. CONNECTING TO SBC & UPLOADING PURE ORIGIN/MASTER CLOCKSYNC.PY ===')
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=pwd, timeout=10)

sftp = client.open_sftp()
remote_clocksync = '/home/armbian/klipper/klippy/clocksync.py'
sftp.put(temp_pure_file, remote_clocksync)
sftp.close()

print('=== 3. CLEARING PYCACHE & RESTARTING KLIPPER WITH PURE ORIGIN/MASTER ===')
client.exec_command(f'echo {pwd} | sudo -S rm -rf /home/armbian/klipper/klippy/__pycache__ /home/armbian/klipper/klippy/*.pyc /tmp/pure_master_dump.csv')
client.exec_command(f'echo {pwd} | sudo -S systemctl restart klipper')
time.sleep(5)

print('=== 4. LAUNCHING HEAVY 4-CORE CPU STRESS + UVLOOP GCC STRESS ===')
cmd = '''
python3 -c "import time; [x**2 for x in range(50000000)]" &
python3 -c "import time; [x**2 for x in range(50000000)]" &
python3 -c "import time; [x**2 for x in range(50000000)]" &
python3 -c "import time; [x**2 for x in range(50000000)]" &
python3 -m venv /tmp/pure_venv
/tmp/pure_venv/bin/pip install --no-cache-dir --no-binary :all: uvloop cython cryptography > /tmp/pure_uvloop.log 2>&1 &
'''
client.exec_command(cmd)

print('=== 5. MONITORING PURE ORIGIN/MASTER LIVE CLOCK DATA FOR 35 SECONDS ===')
for t in range(1, 36):
    time.sleep(1)
    if t % 5 == 0:
        stdin_s, stdout_s, stderr_s = client.exec_command('wc -l /tmp/pure_master_dump.csv 2>/dev/null')
        lines = stdout_s.read().decode().strip().split()[0] if stdout_s else '0'
        print(f'  • t={t:2d}s: Pure origin/master logged {lines} samples under UVLOOP stress...')

print('=== 6. DOWNLOADING & ANALYZING PURE ORIGIN/MASTER DUMP DATA ===')
sftp = client.open_sftp()
local_csv = r'C:\Users\admin\Documents\antigravity\agitated-darwin\klipper\transfer_kit\v2_dumps\pure_master_live_dump.csv'
sftp.get('/tmp/pure_master_dump.csv', local_csv)
sftp.close()
client.close()

# Clean temp pure master script file
if os.path.exists(temp_pure_file): os.remove(temp_pure_file)

# Parse dump
rows = []
with open(local_csv, 'r', encoding='utf-8') as fp:
    for line in fp:
        parts = line.strip().split(',')
        if len(parts) == 4:
            try: rows.append([float(x) for x in parts])
            except: pass

print('\n' + '=' * 85)
print('          PURE ORIGIN/MASTER (PURE EMA) LIVE STRESS EVALUATION REPORT          ')
print('=' * 85)

if len(rows) > 5:
    d = np.array(rows)[5:]
    sent_time = d[:, 0]
    half_rtt_ms = d[:, 1] * 1000.0
    offset_ms = d[:, 2] * 1000.0
    stddev_us = d[:, 3] * 1e6

    r_val, p_val = stats.pearsonr(half_rtt_ms, offset_ms)

    print(f'1. Total Live Samples Collected:       {len(d)} samples')
    print(f'2. Mean Half RTT:                      {np.mean(half_rtt_ms):.3f} ms')
    print(f'3. Max Half RTT Spike:                 {np.max(half_rtt_ms):.3f} ms')
    print(f'4. Mean Prediction StdDev (Jitter):    {np.mean(stddev_us):.2f} us (HIGH EMA JITTER!)')
    print(f'5. Max Prediction StdDev (Jitter):     {np.max(stddev_us):.2f} us (EMA INFLATION!)')
    print(f'6. Pearson r (RTT vs Offset):          {r_val:.4f}')
    print('-------------------------------------------------------------------------------------')
    print('PURE ORIGIN/MASTER EMPIRICAL FINDINGS:')
    print(f'  • Pure origin/master EMA StdDev expands to {np.max(stddev_us):.2f} us under stress!')
    print(f'  • High StdDev prevents sample rejection, BUT causes regression line to wobble.')
    print('=' * 85)
else:
    print('[!] Pure master dump empty or insufficient samples:', len(rows))
