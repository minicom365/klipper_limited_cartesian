import paramiko, time, os, numpy as np
import matplotlib.pyplot as plt
import scipy.stats as stats
from pathlib import Path

host = '192.168.0.7'
user = 'armbian'
pwd = 'armbian_minicom'

print('=== 1. CONNECTING TO SBC VIA SSH ===')
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=pwd, timeout=10)

print('=== 2. UPLOADING PATCHED clocksync.py & RESTARTING KLIPPER ===')
sftp = client.open_sftp()
local_clocksync = r'C:\Users\admin\Documents\antigravity\agitated-darwin\klipper\klippy\clocksync.py'
remote_clocksync = '/home/armbian/klipper/klippy/clocksync.py'
sftp.put(local_clocksync, remote_clocksync)
sftp.close()

client.exec_command(f'echo {pwd} | sudo -S rm -rf /home/armbian/klipper/klippy/__pycache__ /home/armbian/klipper/klippy/*.pyc /tmp/clocksync_dump.csv')
client.exec_command(f'echo {pwd} | sudo -S systemctl restart klipper')

print('=== 3. COLLECTING LIVE CLOCK DUMP SAMPLES (30s) ===')
# Run background CPU stress to generate rich RTT distribution
client.exec_command('python3 -c "import time; [x**2 for x in range(10000000)]" &')
time.sleep(30)

print('=== 4. DOWNLOADING LIVE CLOCKSYNC DUMP ===')
sftp = client.open_sftp()
local_csv = r'C:\Users\admin\Documents\antigravity\agitated-darwin\klipper\transfer_kit\v2_dumps\live_current_dump.csv'
sftp.get('/tmp/clocksync_dump.csv', local_csv)
sftp.close()
client.close()

# Parse CSV
rows = []
with open(local_csv, 'r', encoding='utf-8') as fp:
    for line in fp:
        parts = line.strip().split(',')
        if len(parts) == 4:
            try: rows.append([float(x) for x in parts])
            except: pass

d = np.array(rows)
if len(d) > 5:
    d = d[5:] # Trim initial warmup
    sent_time = d[:, 0]
    half_rtt_ms = d[:, 1] * 1000.0
    offset_ms = d[:, 2] * 1000.0
    abs_offset_ms = np.abs(offset_ms)
    stddev_us = d[:, 3]

    r_val, p_val = stats.pearsonr(half_rtt_ms, offset_ms)

    # Plot Scatter Diagram
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 6), dpi=300)

    # Scatter points
    scatter = ax.scatter(half_rtt_ms, offset_ms, c=stddev_us, cmap='plasma', alpha=0.85, edgecolors='w', linewidths=0.5, s=65, label='Clock Samples')
    
    # Regression trendline
    slope, intercept = np.polyfit(half_rtt_ms, offset_ms, 1)
    x_line = np.linspace(min(half_rtt_ms), max(half_rtt_ms), 100)
    y_line = slope * x_line + intercept
    ax.plot(x_line, y_line, color='#00ffcc', linestyle='--', linewidth=2.0, label=f'Linear Trendline (slope={slope:.3f}, r={r_val:.3f})')

    # Threshold line at 0.5ms
    ax.axvline(x=0.5, color='#ff3366', linestyle=':', linewidth=1.5, label='Klipper Old Outlier Threshold (0.5ms)')

    ax.set_title(f'LIVE RTT vs Raw Offset Scatter Plot (SBC 192.168.0.7)\nDLMAD + RTT-Aware Outlier Rejection Patch (Pearson r = {r_val:.4f})', fontsize=13, fontweight='bold', pad=12, color='#ffffff')
    ax.set_xlabel('Half RTT (ms)', fontsize=11, fontweight='bold', color='#cccccc')
    ax.set_ylabel('Raw Offset (ms)', fontsize=11, fontweight='bold', color='#cccccc')
    ax.grid(True, linestyle='--', alpha=0.3)
    ax.legend(loc='upper left', frameon=True, facecolor='#111122', edgecolor='#444466')

    cb = fig.colorbar(scatter, ax=ax)
    cb.set_label('DLMAD StdDev (us)', fontsize=10, color='#cccccc')
    cb.ax.yaxis.set_tick_params(color='#cccccc')
    plt.setp(plt.getp(cb.ax.axes, 'yticklabels'), color='#cccccc')

    plt.tight_layout()

    out_png1 = r'C:\Users\admin\Documents\antigravity\agitated-darwin\klipper\transfer_kit\reports\current_rtt_vs_offset_scatter.png'
    out_png2 = r'C:\Users\admin\.gemini\antigravity\brain\1d9eda6c-5886-4bd5-9999-9d40a4ee03a0\current_rtt_vs_offset_scatter.png'
    fig.savefig(out_png1)
    fig.savefig(out_png2)
    plt.close()

    print(f'[SUCCESS] Scatter plot generated successfully!')
    print(f'  • Total Samples: {len(d)}')
    print(f'  • Pearson r:     {r_val:.4f}')
    print(f'  • Slope:         {slope:.4f}')
    print(f'  • High-RTT (>0.5ms Accepted): {np.sum(half_rtt_ms > 0.5)}')
    print(f'  • Saved PNG 1:   {out_png1}')
    print(f'  • Saved PNG 2:   {out_png2}')
else:
    print('[!] Insufficient samples collected:', len(d))
