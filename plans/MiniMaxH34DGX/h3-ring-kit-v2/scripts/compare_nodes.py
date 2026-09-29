#!/usr/bin/env python3
"""Compare fresh offline node receipts. Not a live cluster health check."""
import argparse,json,hashlib
from assets_v2 import canonical
from pathlib import Path


def compare(rows):
    if len(rows)!=4 or {r["rank"] for r in rows}!={0,1,2,3}:raise ValueError("Require exactly four unique ranks")
    if any(r["world"]!=4 for r in rows):raise ValueError("Not a four-node check")
    if len({r["fingerprint"] for r in rows})!=1:raise ValueError("Mixed profile/image/model/config/deployment detected")
    if not all(r["full_hash_verified_now"] for r in rows):raise ValueError("Run check --full-hash on all four nodes after distribution")
    if any(hashlib.sha256(canonical(r['identity'])).hexdigest()!=r['fingerprint'] for r in rows):raise ValueError("Receipt fingerprint is internally inconsistent")
    head=next(r for r in rows if r["rank"]==0)
    return {"status":"PASS_OFFLINE_MATCH_ONLY","profile_id":head["profile_id"],"fingerprint":head["fingerprint"],
            "deployment_id":head["deployment_id"],"network_mode":head["network_mode"],
            "live_validation_required":True,"world":4}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipts",nargs=4,type=Path)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args();result=compare([json.loads(x.read_text()) for x in a.receipts])
    a.out.parent.mkdir(parents=True,exist_ok=True)
    with a.out.open("x") as f:json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))
if __name__=="__main__":main()
