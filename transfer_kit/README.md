# 🧪 Klipper ClockSync 4x4 Matrix Research & Benchmark Toolkit

This repository contains the complete experimental suite, 48-file measurement dataset, mathematical evaluation suite, and high-resolution report generators for analyzing **Klipper Micro-Controller Clock Synchronization** under OS-level CPU governor scaling and system stress.

---

## 📂 Directory Structure

```text
transfer_kit/
├── scripts/
│   ├── run_analysis.py        # Master 4x4 evaluation CLI & plot generator
│   ├── ab_stress_tester_v2.py # Interactive SSH 4x4 stress testing automation
│   ├── syslogger.py           # Remote SBC CPU temperature/frequency logger daemon
│   └── test_abc_sim.py        # Quantile grid-search optimization simulator
├── reports/                   # High-resolution 4x4 PNG report visualizations
│   ├── dlmad_proof_variance_4x4.png # 3-Line ABC Algorithm comparison (0~300µs)
│   ├── dlmad_proof_scatter_4x4.png  # RTT vs Absolute Offset scatter (r = 0.92 ~ 0.99)
│   ├── system_temp_freq_4x4.png     # Real-time CPU Temp & Freq timeline
│   └── raw_data_entropy_4x4.png     # Raw offset probability density & Shannon Entropy
├── v2_dumps/                  # 48 raw CSV measurement datasets (16 Matrix cases)
├── RESEARCH_REPORT.md         # Comprehensive mathematical & empirical research report
└── README.md                  # Quick-start documentation
```

---

## 🚀 Quick Start Commands

### 1. Run Master Analysis Suite & Generate All 4 Reports
```bash
python transfer_kit/scripts/run_analysis.py
```

### 2. Run Interactive Remote SBC Stress Tester (A/B Test)
```bash
python transfer_kit/scripts/ab_stress_tester_v2.py --host 192.168.0.7 --user armbian
```

---

## 🔬 Key Empirical Discoveries
1. **CPU Governor Jitter ($r = 0.916 \rightarrow 0.989$)**: On default `ondemand` governor, idle states drop CPU frequency to 547.5 MHz. DVFS scaling latency introduces mild asymmetric jitter. Forcing `performance` (1296.0 MHz locked) eliminates clock scaling delays.
2. **Algorithm Evaluation**:
   - **Method A (Klipper Master EMA)**: Suffers from variance inflation under spike bursts (250µs+).
   - **Method B (Quantile 90% PR #7299)**: Caps maximum variance to ~139µs.
   - **Method C (DLMAD Ours)**: Achieves zero tracking lag and caps maximum variance to 50~100µs with a 50% breakdown point.
3. **Quantile Optimization**: Grid search across 50%~98% shows **85%~90%** is the optimal sweet spot.
