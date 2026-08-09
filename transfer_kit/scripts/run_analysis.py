import numpy as np
import matplotlib.pyplot as plt
import os, sys
import scipy.stats as stats

from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
dump_dir = BASE_DIR / 'v2_dumps'
reports_dir = BASE_DIR / 'reports'
reports_dir.mkdir(parents=True, exist_ok=True)

def calc_discrete_shannon_entropy(data, bin_width=0.01):
    if len(data) == 0: return 0.0
    min_val, max_val = np.min(data), np.max(data)
    if min_val == max_val: return 0.0
    bins = np.arange(min_val, max_val + bin_width, bin_width)
    counts, _ = np.histogram(data, bins=bins)
    probs = counts / np.sum(counts)
    probs = probs[probs > 0]
    return -np.sum(probs * np.log2(probs))

def calc_shannon_entropy(data, bins=30):
    if len(data) == 0: return 0.0
    counts, _ = np.histogram(data, bins=bins, density=True)
    counts = counts[counts > 0]
    return -np.sum(counts * np.log2(counts))

def simulate_abc(offsets_sec):
    n = len(offsets_sec)
    std_ema = np.zeros(n)
    std_q90 = np.zeros(n)
    std_dlmad = np.zeros(n)
    
    # Method A: Pure Master Klipper EMA (DECAY = 0.005)
    DECAY = 0.005
    var = (10e-6)**2
    for i in range(n):
        err = offsets_sec[i]
        var = (1.0 - DECAY) * (var + (err**2) * DECAY)
        std_ema[i] = np.sqrt(var) * 1e6 # us
        
    # Method B: Maintainer PR #7299 (nefelim4ag: Rolling 30, Quantile 90%)
    window = 30
    for i in range(n):
        if i < 5:
            std_q90[i] = std_ema[i]
            std_dlmad[i] = std_ema[i]
        else:
            w_data = offsets_sec[max(0, i-window+1):i+1]
            # Method B
            q90 = np.percentile(np.abs(w_data), 90)
            valid = w_data[np.abs(w_data) <= q90]
            std_q90[i] = (np.std(valid) if len(valid) > 2 else np.std(w_data)) * 1e6
            
            # Method C: Pure DLMAD Unpadded Jitter (1.4826 * MAD)
            med = np.median(w_data)
            mad = np.median(np.abs(w_data - med))
            std_dlmad[i] = (mad * 1.4826) * 1e6 # us
            
    return std_ema, std_q90, std_dlmad

def load_data(gov, mode, phase):
    clock_file = os.path.join(dump_dir, f"clocksync_dump_{gov}_{mode}_{phase}.csv")
    sys_file = os.path.join(dump_dir, f"syslog_dump_{gov}_{mode}_{phase}.csv")
    
    if not os.path.exists(clock_file):
        clock_file = os.path.join(dump_dir, f"clocksync_dump_{gov}_{phase}.csv")
    if not os.path.exists(sys_file):
        sys_file = os.path.join(dump_dir, f"syslog_dump_{gov}_{phase}.csv")
        
    if not os.path.exists(clock_file): return None
    
    clock_parsed = []
    with open(clock_file, 'r') as f:
        for line in f:
            parts = line.strip().split(',')
            if len(parts) == 4:
                try: clock_parsed.append([float(x) for x in parts])
                except ValueError: pass
    if len(clock_parsed) < 10: return None
    clock_data = np.array(clock_parsed)[10:]
    
    sys_data = None
    if os.path.exists(sys_file):
        sys_parsed = []
        with open(sys_file, 'r') as f:
            header = f.readline()
            for line in f:
                parts = line.strip().split(',')
                if len(parts) >= 7:
                    try: sys_parsed.append([float(x) for x in parts[:7]])
                    except ValueError: pass
        if len(sys_parsed) >= 10:
            sys_data = np.array(sys_parsed)[10:]
            
    t_clock = clock_data[:, 0]
    rtt_ms = clock_data[:, 1] * 1000.0
    offset_ms = clock_data[:, 2] * 1000.0
    abs_offset_ms = np.abs(offset_ms)
    
    std_ema, std_q90, std_dlmad = simulate_abc(clock_data[:, 2])
    
    temp_interp = np.zeros_like(t_clock)
    freq_interp = np.zeros_like(t_clock)
    
    if sys_data is not None:
        t_sys = sys_data[:, 0]
        temp_interp = np.interp(t_clock, t_sys, sys_data[:, 5])
        freq_interp = np.interp(t_clock, t_sys, sys_data[:, 6])
        
    return {
        'time': t_clock - t_clock[0],
        'rtt': rtt_ms,
        'offset': offset_ms,
        'abs_offset': abs_offset_ms,
        'stddev_us': std_dlmad, # Pure unpadded DLMAD jitter (us)
        'stddev_ema': std_ema,
        'stddev_q90': std_q90,
        'temp': temp_interp,
        'freq': freq_interp
    }

def run_full_analysis():
    print("=========================================================================================================")
    print("                     UNIFIED MASTER ANALYSIS SUITE (4x4 MATRIX EVALUATION)")
    print("=========================================================================================================\n")
    
    governors = ['ondemand', 'performance']
    modes = ['high-freq', 'low-freq']
    phases = ['baseline', 'cpu_stress', 'io_stress', 'combo_stress']
    
    matrix = {}
    metrics_table = []
    
    for gov in governors:
        for mode in modes:
            for phase in phases:
                key = f"{gov}_{mode}_{phase}"
                data = load_data(gov, mode, phase)
                if data:
                    matrix[key] = data
                    
                    # Compute Linearity R^2 and Pearson r
                    rtt = data['rtt']
                    off = data['abs_offset']
                    if len(rtt) > 2 and np.std(rtt) > 0 and np.std(off) > 0:
                        r, _ = stats.pearsonr(rtt, off)
                        r2 = r**2
                    else:
                        r, r2 = 0.0, 0.0
                        
                    # Entropy and Kurtosis
                    h_rtt = calc_discrete_shannon_entropy(rtt, bin_width=0.01)
                    h_off = calc_discrete_shannon_entropy(off, bin_width=0.01)
                    kurt = stats.kurtosis(data['stddev_us'])
                    
                    metrics_table.append((key, r2, r, h_rtt, h_off, kurt, np.mean(data['temp']), np.mean(data['freq'])))

    # 1. Print Mathematical Table
    print(f"{'CASE':<35} | {'Linearity R^2':<13} | {'Pearson r':<10} | {'Raw Offset Entropy (H)':<22} | {'Kurtosis':<10} | {'Mean Temp (°C)':<15} | {'Mean Freq (MHz)':<15}")
    print("-" * 135)
    for row in metrics_table:
        print(f"{row[0]:<35} | {row[1]:<13.3f} | {row[2]:<10.3f} | {row[4]:<22.3f} | {row[5]:<10.2f} | {row[6]:<15.1f} | {row[7]:<15.1f}")
        
    plt.style.use('dark_background')
    
    # 2. Render 4x4 Variance Time Series Plot
    fig_var, axes_var = plt.subplots(4, 4, figsize=(24, 18), sharex=True, sharey=True)
    fig_var.suptitle("4x4 Matrix: DLMAD ClockSync Prediction Variance (StdDev us)", fontsize=20, fontweight='bold', y=0.98)
    
    # 3. Render 4x4 Scatter Plot (RTT vs Offset)
    fig_scat, axes_scat = plt.subplots(4, 4, figsize=(24, 18), sharex=True, sharey=True)
    fig_scat.suptitle("4x4 Matrix: RTT vs Absolute Offset Scatter (Uniform Range 0~3.0ms)", fontsize=20, fontweight='bold', y=0.98)
    
    # 4. Render 4x4 Temperature & Frequency Plot
    fig_temp, axes_temp = plt.subplots(4, 4, figsize=(24, 18), sharex=True, sharey=False)
    fig_temp.suptitle("4x4 Matrix: Real-time CPU Temperature (°C) & CPU Frequency (MHz)", fontsize=20, fontweight='bold', y=0.98)
    
    # 5. Render 4x4 Raw Data Entropy & PDF Plot
    fig_ent, axes_ent = plt.subplots(4, 4, figsize=(24, 18), sharex=True, sharey=True)
    fig_ent.suptitle("4x4 Matrix: Raw Offset Probability Density & Shannon Entropy (Uniform Range 0~1.5ms)", fontsize=20, fontweight='bold', y=0.98)
    
    col_idx = 0
    for gov in governors:
        for mode in modes:
            for row_idx, phase in enumerate(phases):
                key = f"{gov}_{mode}_{phase}"
                data = matrix.get(key)
                
                ax_v = axes_var[row_idx, col_idx]
                ax_s = axes_scat[row_idx, col_idx]
                ax_t = axes_temp[row_idx, col_idx]
                ax_e = axes_ent[row_idx, col_idx]
                
                title = f"{gov.upper()} | {mode}\n{phase}"
                
                if data:
                    t = data['time']
                    
                    # Variance (ABC Algorithm Normalized 3-Line Comparison on 0~300us Scale)
                    ax_v.plot(t, data['stddev_ema'], color='#ff3333', linewidth=1.2, label='A: Master EMA')
                    ax_v.plot(t, data['stddev_q90'], color='#3399ff', linewidth=1.2, label='B: Quantile 90%')
                    ax_v.plot(t, data['stddev_us'], color='#00ffaa', linewidth=1.5, label='C: DLMAD (Ours)')
                    ax_v.set_title(title, fontweight='bold', fontsize=11)
                    ax_v.set_xlim(0, 60) # Enforce uniform 0~60s time window!
                    ax_v.set_ylim(0, 300)
                    ax_v.grid(True, linestyle='--', alpha=0.3)
                    if row_idx == 0 and col_idx == 0: ax_v.legend(loc='upper right', fontsize=8)
                    if col_idx == 0: ax_v.set_ylabel("StdDev (us)")
                    if row_idx == 3: ax_v.set_xlabel("Time (s)")
                    
                    # Scatter
                    if key in matrix:
                        r, _ = stats.pearsonr(data['rtt'], data['abs_offset']) if len(data['rtt']) > 2 else (0.0, 0.0)
                        ax_s.scatter(data['rtt'], data['abs_offset'], color='#ff00aa', alpha=0.7, s=15)
                        ax_s.set_title(f"{title}\nr = {r:.2f}", fontweight='bold', fontsize=11)
                    ax_s.set_xlim(0, 3.0)
                    ax_s.set_ylim(0, 2.5)
                    ax_s.grid(True, linestyle='--', alpha=0.3)
                    if col_idx == 0: ax_s.set_ylabel("|Raw Offset| (ms)")
                    if row_idx == 3: ax_s.set_xlabel("Half RTT (ms)")
                    
                    # Temp & Freq
                    ax_t.plot(t, data['temp'], color='#ff3333', linewidth=2)
                    ax_t_twin = ax_t.twinx()
                    ax_t_twin.plot(t, data['freq'], color='#ffaa00', linestyle='-', linewidth=2)
                    ax_t.set_title(title, fontweight='bold', fontsize=11)
                    ax_t.set_xlim(0, 60) # Enforce uniform 0~60s time window!
                    ax_t.set_ylim(30, 80)
                    ax_t_twin.set_ylim(400, 1500)
                    ax_t.grid(True, linestyle='--', alpha=0.3)
                    if col_idx == 0: ax_t.set_ylabel("Temp (°C)", color='#ff3333')
                    if col_idx == 3: ax_t_twin.set_ylabel("Freq (MHz)", color='#ffaa00')
                    
                    # Entropy
                    h_off = calc_discrete_shannon_entropy(data['abs_offset'], bin_width=0.01)
                    ax_e.hist(data['abs_offset'], bins=50, color='#aa00ff', alpha=0.8, density=True)
                    ax_e.set_title(f"{title}\nH={h_off:.2f} bits", fontweight='bold', fontsize=11)
                    ax_e.set_xlim(0, 1.5)
                    ax_e.set_ylim(0, 8.0)
                    ax_e.grid(True, linestyle='--', alpha=0.3)
                    if col_idx == 0: ax_e.set_ylabel("Probability Density")
                    if row_idx == 3: ax_e.set_xlabel("|Raw Offset| (ms)")
                else:
                    ax_v.set_title(f"{title}\n[NO DATA]")
                    ax_s.set_title(f"{title}\n[NO DATA]")
                    ax_t.set_title(f"{title}\n[NO DATA]")
                    ax_e.set_title(f"{title}\n[NO DATA]")
                    
            col_idx += 1
            
    # Save all 4 Consolidated Reports
    p_var = os.path.join(reports_dir, "dlmad_proof_variance_4x4.png")
    p_scat = os.path.join(reports_dir, "dlmad_proof_scatter_4x4.png")
    p_temp = os.path.join(reports_dir, "system_temp_freq_4x4.png")
    p_ent = os.path.join(reports_dir, "raw_data_entropy_4x4.png")
    
    fig_var.tight_layout(rect=[0, 0, 1, 0.95]); fig_var.savefig(p_var, dpi=200, bbox_inches='tight'); plt.close(fig_var)
    fig_scat.tight_layout(rect=[0, 0, 1, 0.95]); fig_scat.savefig(p_scat, dpi=200, bbox_inches='tight'); plt.close(fig_scat)
    fig_temp.tight_layout(rect=[0, 0, 1, 0.95]); fig_temp.savefig(p_temp, dpi=200, bbox_inches='tight'); plt.close(fig_temp)
    fig_ent.tight_layout(rect=[0, 0, 1, 0.95]); fig_ent.savefig(p_ent, dpi=200, bbox_inches='tight'); plt.close(fig_ent)
    
    print("\n[SUCCESS] Generated 4 Consolidated High-Resolution Reports in /reports:")
    print(f"  1. {p_var}")
    print(f"  2. {p_scat}")
    print(f"  3. {p_temp}")
    print(f"  4. {p_ent}")

if __name__ == '__main__':
    run_full_analysis()
