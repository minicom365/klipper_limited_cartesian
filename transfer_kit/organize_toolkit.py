import os, shutil

BASE_DIR = r'D:\transfer_kit'
scripts_dir = os.path.join(BASE_DIR, 'scripts')
reports_dir = os.path.join(BASE_DIR, 'reports')

os.makedirs(scripts_dir, exist_ok=True)
os.makedirs(reports_dir, exist_ok=True)

# 1. Scripts to move into scripts/
scripts_files = [
    'ab_stress_tester_v2.py',
    'syslogger.py',
    'analyze_dlmad_abc.py',
    'advanced_analysis.py',
    'plot_4x4_temp_freq_entropy.py',
    'plot_realtime.py',
    'deep_research_entropy_temp.py'
]

for sf in scripts_files:
    src = os.path.join(BASE_DIR, sf)
    dst = os.path.join(scripts_dir, sf)
    if os.path.exists(src):
        shutil.move(src, dst)
        print(f"Moved script: {sf} -> scripts/")

# 2. Report PNGs to move into reports/
report_files = [
    'dlmad_abc_proof_variance_4x4.png',
    'dlmad_abc_proof_scatter_4x4.png',
    'dlmad_abc_proof_ondemand.png',
    'dlmad_abc_proof_performance.png',
    'deep_analysis_temp_freq_4x4.png',
    'deep_analysis_entropy_4x4.png',
    'deep_analysis_temp_freq_entropy.png',
    'realtime_leakage_visual_v6.png'
]

for rf in report_files:
    src = os.path.join(BASE_DIR, rf)
    dst = os.path.join(reports_dir, rf)
    if os.path.exists(src):
        shutil.move(src, dst)
        print(f"Moved report: {rf} -> reports/")

print("\nDirectory organization complete!")
