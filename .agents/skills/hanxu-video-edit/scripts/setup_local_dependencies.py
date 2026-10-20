#!/usr/bin/env python3
"""Preview or create only a workspace-local Python environment for optional checks."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',type=Path,required=True);p.add_argument('--install',action='store_true');a=p.parse_args();root=a.workspace.resolve(strict=True);runtime=(root/'.video-agent/runtime').resolve()
    if not runtime.is_relative_to(root):raise ValueError('Runtime escapes workspace')
    target=runtime/'python';plan={'target':str(target),'packages':['Pillow>=10,<13'],'global_install':False,'installed':False}
    if a.install:
        if target.exists():raise ValueError('Local runtime already exists; inspect it instead of overwriting')
        cache=runtime/'cache';temp=runtime/'tmp';cache.mkdir(parents=True,exist_ok=True);temp.mkdir(exist_ok=True)
        env=dict(os.environ,PIP_CACHE_DIR=str(cache),TMPDIR=str(temp),TEMP=str(temp),TMP=str(temp),PYTHONDONTWRITEBYTECODE='1',PIP_DISABLE_PIP_VERSION_CHECK='1')
        # venv leaves the invoking interpreter and its site-packages untouched.
        subprocess.run([sys.executable,'-m','venv',str(target)],check=True,env=env)
        py=target/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        subprocess.run([str(py),'-m','pip','--isolated','--require-virtualenv','--disable-pip-version-check','--cache-dir',str(cache),'install','--no-user','--no-compile','Pillow>=10,<13'],check=True,env=env)
        subprocess.run([str(py),'-c','from PIL import ImageFont; print("glyph dependency import passed")'],check=True,env=env)
        plan.update(installed=True,python=str(py))
    print(json.dumps(plan,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
