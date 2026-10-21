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
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Choose a separate editing workspace, outside the installed skill')
    if not runtime.is_relative_to(root):raise ValueError('Runtime escapes workspace')
    target=runtime/'python';py=target/('Scripts/python.exe' if os.name=='nt' else 'bin/python');plan={'target':str(target),'python':str(py),'packages':['Pillow>=10,<13'],'global_install':False,'installed':False,'exists':target.exists()}
    if a.install:
        if target.exists():raise ValueError('Local runtime already exists; inspect it instead of overwriting')
        cache=runtime/'cache';temp=runtime/'tmp';cache.mkdir(parents=True,exist_ok=True);temp.mkdir(exist_ok=True)
        env=dict(os.environ,PIP_CACHE_DIR=str(cache),TMPDIR=str(temp),TEMP=str(temp),TMP=str(temp),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',PIP_DISABLE_PIP_VERSION_CHECK='1')
        # venv leaves the invoking interpreter and its site-packages untouched.
        log=runtime/'setup.log'
        try:
            with log.open('w',encoding='utf-8') as output:
                subprocess.run([sys.executable,'-m','venv',str(target)],check=True,env=env,stdout=output,stderr=subprocess.STDOUT)
                py=target/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
                subprocess.run([str(py),'-m','pip','--isolated','--require-virtualenv','--disable-pip-version-check','--cache-dir',str(cache),'install','--no-user','--no-compile','Pillow>=10,<13'],check=True,env=env,stdout=output,stderr=subprocess.STDOUT)
                subprocess.run([str(py),'-c','from PIL import ImageFont; print("glyph dependency import passed")'],check=True,env=env,stdout=output,stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as exc:
            raise ValueError('Local dependency setup failed; partial files retained, inspect '+str(log)) from exc
        plan.update(installed=True,python=str(py),log=str(log))
    print(json.dumps(plan,ensure_ascii=False,indent=2))

# Keep Unicode paths/JSON readable in Windows terminals and redirected output.
for _stream in (sys.stdout, sys.stderr):
    if __name__ == "__main__" and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

if __name__=='__main__':main()
