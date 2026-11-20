import argparse, json, os, subprocess, time
from pathlib import Path

def run(cmd, timeout_s=1.0):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout_s)
        return (p.stdout or "").strip()
    except Exception:
        return ""

def systemctl_state(unit: str):
    out = run([
        "systemctl","show",unit,
        "-p","ActiveState","-p","SubState","-p","ActiveEnterTimestamp",
        "--no-pager",
    ], timeout_s=1.0)
    st = {"active":"?","sub":"?","since":""}
    for line in out.splitlines():
        if line.startswith("ActiveState="):
            st["active"] = line.split("=",1)[1].strip()
        elif line.startswith("SubState="):
            st["sub"] = line.split("=",1)[1].strip()
        elif line.startswith("ActiveEnterTimestamp="):
            st["since"] = line.split("=",1)[1].strip()
    return st

def read_json_line(p: Path):
    try:
        if not p.exists():
            return None
        txt = p.read_text(encoding="utf-8", errors="ignore").strip()
        if not txt:
            return None
        return json.loads(txt)
    except Exception:
        return None

def tail_jsonl(p: Path, n: int):
    try:
        if not p.exists():
            return []
        out = run(["tail","-n",str(int(n)),str(p)], timeout_s=1.0)
        recs = []
        for raw in out.splitlines()[-n:]:
            try:
                recs.append(json.loads(raw))
            except Exception:
                pass
        return recs
    except Exception:
        return []

ap = argparse.ArgumentParser()
ap.add_argument("--out-dir", required=True)
ap.add_argument("--refresh", type=float, default=1.0)
args = ap.parse_args()

out_path = Path(args.out_dir)

def get_status_path(filename):
    uid = os.getuid()
    cands = [
        Path("/run/kkfm-watchdog") / filename,
        Path(f"/run/user/{uid}/kkfm-watchdog") / filename,
        Path(f"/dev/shm/kkfm-watchdog-{uid}") / filename,
        out_path / filename,
    ]
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return out_path / filename

def get_anomalies_path():
    uid = os.getuid()
    cands = [
        Path("/run/kkfm-watchdog") / "anomalies.jsonl",
        Path(f"/run/user/{uid}/kkfm-watchdog") / "anomalies.jsonl",
        Path(f"/dev/shm/kkfm-watchdog-{uid}") / "anomalies.jsonl",
        out_path / "anomalies.jsonl",
    ]
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return out_path / "anomalies.jsonl"

units = [
    "acnh-aircheck.service",
    "acnh-aircheck-watchdog.service",
    "acnh-aircheck-tx-watchdog.service",
]

while True:
    snap = {
        "ts": time.time(),
        "services": {u: systemctl_state(u) for u in units},
        "tx": read_json_line(get_status_path("tx_status.json")),
        "log": read_json_line(get_status_path("log_status.json")),
        "anomalies": tail_jsonl(get_anomalies_path(), 50),
    }
    print(json.dumps(snap, ensure_ascii=False), flush=True)
    time.sleep(max(0.1, float(args.refresh)))

