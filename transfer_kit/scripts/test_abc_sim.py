import numpy as np
import os, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
dump_dir = os.path.join(BASE_DIR, 'v2_dumps')

def simulate_abc(offsets_sec):
    n = len(offsets_sec)
    std_ema = np.zeros(n)
    std_q90 = np.zeros(n)
    
    var = (10e-6)**2
    for i in range(n):
        err = offsets_sec[i]
        var = 0.85 * var + 0.15 * (err**2)
        std_ema[i] = np.sqrt(var) * 1e6
        
    window = 30
    for i in range(n):
        if i < 5:
            std_q90[i] = std_ema[i]
        else:
            w_data = offsets_sec[max(0, i-window+1):i+1]
            q90 = np.percentile(np.abs(w_data), 90)
            valid = w_data[np.abs(w_data) <= q90]
            std_q90[i] = (np.std(valid) if len(valid) > 2 else np.std(w_data)) * 1e6
            
    return std_ema, std_q90

f = os.path.join(dump_dir, 'clocksync_dump_performance_high-freq_baseline.csv')
parsed = []
for line in open(f):
    parts = line.strip().split(',')
    if len(parts) == 4:
        try: parsed.append([float(x) for x in parts])
        except: pass

arr = np.array(parsed)
offsets_sec = arr[:, 2]
stddev_dlmad_us = arr[:, 3]

std_ema, std_q90 = simulate_abc(offsets_sec)

print("EMA (A) Mean StdDev us:", np.mean(std_ema))
print("Quantile 90% (B) Mean StdDev us:", np.mean(std_q90))
print("DLMAD (C) Mean StdDev us:", np.mean(stddev_dlmad_us))
