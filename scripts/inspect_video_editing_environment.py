#!/usr/bin/env python3
"""Read-only capacity and process observations; never a GUI/quality health test.

Python standard library only. No app control, cleanup, authentication or media
access. A returned process PID does not establish that its window responds.
"""
import argparse
import sys
import csv
import io
import json
import math
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

GIB = 1024 ** 3


def nonnegative(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def capacity(paths, min_free_gib, reserve_gib):
    """Check every actual target volume; do not sum space across volumes."""
    required = math.ceil((min_free_gib + reserve_gib) * GIB)
    volumes = {}
    for raw in paths:
        path = Path(raw).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f"Expected existing directory: {path}")
        device = path.stat().st_dev
        if device not in volumes:
            usage = shutil.disk_usage(path)
            volumes[device] = {"device": device, "paths": [],
                               "freeBytes": usage.free, "requiredBytes": required,
                               "sufficient": usage.free >= required}
        volumes[device]["paths"].append(str(path))
    return list(volumes.values())


def observation(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True,encoding='utf-8',errors='replace',
                                timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    if result.returncode:
        return {"status": "unavailable", "returncode": result.returncode}
    return {"status": "observed", "value": result.stdout.strip()}


def app_observation():
    if platform.system() == 'Windows':
        result = observation(['tasklist','/FO','CSV','/NH'])
        if result['status'] != 'observed': return result
        apps=[]
        for row in csv.reader(io.StringIO(result['value'])):
            if len(row)<5 or row[0].lower() not in {'jianyingpro.exe','capcut.exe','chatcut.exe'}: continue
            try:
                apps.append({'pid':int(row[1]),'rssKiB':int(re.sub(r'\D','',row[4])),'name':row[0]})
            except ValueError: continue
        return {'status':'observed','apps':apps,'windowResponsiveness':'not_tested'}
    result = observation(["ps", "-axo", "pid=,rss=,comm="])
    if result["status"] != "observed":
        return result
    apps = []
    for line in result["value"].splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        name = Path(fields[2]).name
        if name not in {"JianyingPro", "VideoFusion-macOS", "CapCut", "ChatCut", "QuickTime Player"}:
            continue
        try:
            apps.append({"pid": int(fields[0]), "rssKiB": int(fields[1]),
                         "name": name})
        except ValueError:
            continue
    return {"status": "observed", "apps": apps,
            "windowResponsiveness": "not_tested"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", action="append", required=True,
                        help="Existing system/cache/export directory; repeat for volumes")
    parser.add_argument("--min-free-gb", type=nonnegative, default=5,
                        help="GiB floor, default 5; configurable capacity budget, not app guarantee")
    parser.add_argument("--reserve-gb", type=nonnegative, default=0,
                        help="Additional estimated remaining output/cache GiB per volume")
    parser.add_argument("--budget-plan", type=Path, help="Complete remaining per-volume allocation plan")
    args = parser.parse_args()
    try:
        if args.budget_plan:
            from manage_video_editing_run import budget
            if args.reserve_gb: raise ValueError('Use budget-plan or reserve-gb, not both')
            plan_path=args.budget_plan.resolve()
            complete=budget(json.loads(plan_path.read_text(encoding='utf-8-sig')),plan_path.parent,args.path,args.min_free_gb)
            volumes=complete['volumes']
        else:
            complete=None
            volumes = capacity(args.path, args.min_free_gb, args.reserve_gb)
    except (OSError, ValueError) as exc:
        print(json.dumps({"capacityStatus": "unknown", "error": str(exc)}))
        return 1
    sufficient = all(item["sufficient"] for item in volumes)
    report = {"observedAt": datetime.now(timezone.utc).isoformat(),
              "capacityStatus": "sufficient" if sufficient else "insufficient",
              "minFreeGiB": args.min_free_gb, "reserveGiB": args.reserve_gb,
              "remainingWorkEstimateProvided": bool(complete) or args.reserve_gb > 0,
              "completeBudget": complete,
              "volumes": volumes, "processes": app_observation(),
              "applicationHealth": "not_tested"}
    if platform.system() == "Darwin":
        report["swap"] = observation(["sysctl", "vm.swapusage"])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if sufficient else 2


# Keep Unicode paths/JSON readable in Windows terminals and redirected output.
for _stream in (sys.stdout, sys.stderr):
    if __name__ == "__main__" and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

if __name__ == "__main__":
    raise SystemExit(main())
