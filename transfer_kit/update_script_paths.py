import os

scripts_dir = r'D:\transfer_kit\scripts'

for fname in os.listdir(scripts_dir):
    if fname.endswith('.py'):
        fpath = os.path.join(scripts_dir, fname)
        content = open(fpath, 'r', encoding='utf-8').read()
        
        # Replace BASE_DIR definition to resolve root folder D:\transfer_kit
        modified = False
        
        if 'BASE_DIR = os.path.dirname(os.path.abspath(__file__))' in content:
            content = content.replace(
                'BASE_DIR = os.path.dirname(os.path.abspath(__file__))',
                'SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))\nBASE_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))'
            )
            modified = True
            
        if 'BASE_DIR = r\'D:\\transfer_kit\'' in content:
            content = content.replace(
                'BASE_DIR = r\'D:\\transfer_kit\'',
                'SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))\nBASE_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))'
            )
            modified = True
            
        # Update output paths to point to reports folder if saving PNGs
        if 'output_var = os.path.join(BASE_DIR, "' in content:
            content = content.replace(
                'output_var = os.path.join(BASE_DIR, "',
                'reports_dir = os.path.join(BASE_DIR, "reports")\nos.makedirs(reports_dir, exist_ok=True)\noutput_var = os.path.join(reports_dir, "'
            )
            content = content.replace('output_scat = os.path.join(BASE_DIR, "', 'output_scat = os.path.join(reports_dir, "')
            modified = True
            
        if 'out_temp = os.path.join(BASE_DIR, "' in content:
            content = content.replace(
                'out_temp = os.path.join(BASE_DIR, "',
                'reports_dir = os.path.join(BASE_DIR, "reports")\nos.makedirs(reports_dir, exist_ok=True)\nout_temp = os.path.join(reports_dir, "'
            )
            content = content.replace('out_ent = os.path.join(BASE_DIR, "', 'out_ent = os.path.join(reports_dir, "')
            modified = True
            
        if 'plot_output = os.path.join(BASE_DIR, "' in content:
            content = content.replace(
                'plot_output = os.path.join(BASE_DIR, "',
                'reports_dir = os.path.join(BASE_DIR, "reports")\nos.makedirs(reports_dir, exist_ok=True)\nplot_output = os.path.join(reports_dir, "'
            )
            modified = True
            
        if 'output_png = os.path.join(BASE_DIR, "' in content:
            content = content.replace(
                'output_png = os.path.join(BASE_DIR, "',
                'reports_dir = os.path.join(BASE_DIR, "reports")\nos.makedirs(reports_dir, exist_ok=True)\noutput_png = os.path.join(reports_dir, "'
            )
            modified = True
            
        if modified:
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"Updated paths in: {fname}")

print("\nScript paths updated successfully!")
