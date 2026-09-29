#!/usr/bin/env python3
"""Authenticated local control client; prints no API key or session cookie."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import sys
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from DGXGateway.dgx_gateway.config import load_config


def client():
    config=load_config(Path.home()/'.config/dgx-cluster-gateway/gateway.toml')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    root=f'http://{config.bind}:{config.port}'
    def request(path,payload=None):
        data=None if payload is None else json.dumps(payload).encode()
        req=urllib.request.Request(root+path,data=data,headers={'Origin':config.allowed_origins[0],'Content-Type':'application/json'})
        with opener.open(req,timeout=60) as response:return json.load(response)
    if not config.lan_no_auth:request('/api/v1/session',{'token':config.admin_token_file.read_text().strip()})
    return request


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['snapshot','switch','operation'])
    parser.add_argument('--target');parser.add_argument('--operation');parser.add_argument('--key');parser.add_argument('--output',type=Path)
    args=parser.parse_args();request=client()
    if args.action=='snapshot':
        raw=request('/api/v1/snapshot');value={k:raw.get(k) for k in ['active_local_model','models','lock','current_operation','residency']}
    elif args.action=='switch':value=request('/api/v1/switches',{'target_model':args.target,'idempotency_key':args.key})
    else:value=request('/api/v1/operations/'+args.operation)
    if args.output:args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(value,ensure_ascii=False))
if __name__=='__main__':main()
