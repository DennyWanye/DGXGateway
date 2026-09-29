"""Temporary, scoped RDMA IPv4 configuration. No netplan/routing/sysctl changes.
Default action is a dry plan. Review every interface before --apply.
"""
from __future__ import annotations
import argparse
import datetime
import ipaddress
import json
import os
import shlex
import subprocess
from pathlib import Path
from common import load_config

def run(cmd: list[str], capture=False):
    if capture:return subprocess.check_output(cmd,text=True)
    print('+ '+shlex.join(cmd),flush=True)
    subprocess.run(cmd,check=True)

def net_state(dev):
    return json.loads(run(['ip','-j','address','show','dev',dev],True))[0]

def ipv4(state):
    return [a for a in state.get('addr_info',[]) if a['family']=='inet']

def addrstr(a):return f"{a['local']}/{a['prefixlen']}"

def build_plan(c,n,states):
    desired={n['prev_iface']:n['prev_cidr'],n['next_iface']:n['next_cidr']}
    cmds=[]
    for dev,s in states.items():
        for a in ipv4(s):cmds.append(['ip','address','del',addrstr(a),'dev',dev])
        if dev in desired:
            cmds += [['ip','link','set','dev',dev,'mtu','9000'],
                     ['ip','link','set','dev',dev,'up'],
                     ['ip','address','add',desired[dev],'dev',dev]]
    return cmds

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',default='config/cluster.json')
    p.add_argument('--rank',type=int,choices=range(4),required=True)
    p.add_argument('action',choices=['plan','apply','restore','check'])
    p.add_argument('--maintenance-approved',action='store_true')
    a=p.parse_args();c=load_config(a.config);n=c['nodes'][a.rank]
    touched=[n['prev_iface'],n['next_iface']]+n.get('unused_rdma_ifaces',[])
    mg=net_state(n['management_iface'])
    if n['management_ip'] not in [x['local'] for x in ipv4(mg)]:
        raise SystemExit('Configured management IP is not local: wrong rank or config')
    states={d:net_state(d) for d in touched}
    statefile=Path(c['root'])/'state'/f'network-r{a.rank}.before.json'
    if a.action=='restore':
        if not a.maintenance_approved or os.geteuid()!=0:raise SystemExit('Restore requires root and maintenance approval')
        saved=json.loads(statefile.read_text())
        if saved['management_ip']!=n['management_ip'] or set(saved['interfaces'])!=set(touched):
            raise SystemExit('Snapshot/config mismatch; restore manually from the saved inventory')
        for dev,old in saved['interfaces'].items():
            for x in ipv4(states[dev]):run(['ip','address','del',addrstr(x),'dev',dev])
            run(['ip','link','set','dev',dev,'mtu',str(old['mtu'])])
            for x in ipv4(old):
                cmd=['ip','address','add',addrstr(x),'dev',dev]
                if x.get('broadcast'):cmd+=['broadcast',x['broadcast']]
                if x.get('scope'):cmd+=['scope',x['scope']]
                if x.get('label') and x['label']!=dev:cmd+=['label',x['label']]
                if x.get('noprefixroute'):cmd+=['noprefixroute']
                run(cmd)
            run(['ip','link','set','dev',dev,'up' if 'UP' in old.get('flags',[]) else 'down'])
        out=statefile.with_name(statefile.name+'.restored-'+datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
        statefile.rename(out)
        print('RESTORED named-interface IPv4/MTU/admin flags. Reconnect original cables and verify old services separately.')
        return
    for dev,s in states.items():
        if s.get('master'):
            raise SystemExit(f'{dev} is enslaved to a bridge/bond; no automatic change')
        if not (Path('/sys/class/net')/dev/'device'/'infiniband').exists():
            raise SystemExit(f'{dev} is not an inspected RDMA PCI interface')
        for x in ipv4(s):
            if x.get('dynamic') or x.get('valid_life_time') not in (None,4294967295,'forever'):
                raise SystemExit(f'{dev} has DHCP/dynamic IPv4; resolve the network-manager profile first')
        routes=json.loads(run(['ip','-j','-4','route','show','table','all','dev',dev],True))
        for r in routes:
            if r.get('gateway') or (r.get('type','unicast')=='unicast' and r.get('protocol')!='kernel'):
                raise SystemExit(f'{dev} carries non-connected routes; review manually before using this helper')
    if a.action=='check':
        for side in ('prev','next'):
            dev=n[f'{side}_iface'];want=n[f'{side}_cidr']
            got=[addrstr(x) for x in ipv4(states[dev])]
            if got!=[want] or states[dev]['mtu']!=9000:
                raise SystemExit(f'{dev}: expected only {want}, MTU9000; actual {got}, MTU={states[dev]["mtu"]}')
            peer=c['nodes'][(a.rank-1)%4]['next_cidr'] if side=='prev' else c['nodes'][(a.rank+1)%4]['prev_cidr']
            peer=str(ipaddress.IPv4Interface(peer).ip)
            run(['ping','-I',dev,'-c','3','-W','3',peer])
            run(['ping','-I',dev,'-M','do','-s','8972','-c','3','-W','3',peer])
        for dev in n.get('unused_rdma_ifaces',[]):
            if ipv4(states[dev]):raise SystemExit(f'Unused RDMA interface still has IPv4: {dev}')
        print('ADJACENT_IP_AND_JUMBO_PASS (not proof of RDMA/NCCL)')
        return
    # Refuse overlap with any IPv4 subnet on any untouched interface.
    all_states=json.loads(run(['ip','-j','-4','address','show'],True))
    targets=[ipaddress.ip_interface(n[f'{s}_cidr']).network for s in ('prev','next')]
    for other in all_states:
        if other['ifname'] in touched:continue
        for x in ipv4(other):
            net=ipaddress.ip_interface(addrstr(x)).network
            if any(t.overlaps(net) for t in targets):
                raise SystemExit(f'RDMA subnet overlaps {other["ifname"]}: {net}')
    commands=build_plan(c,n,states)
    if a.action=='plan':
        print(json.dumps({'snapshot_to':str(statefile),'interfaces_before':states},indent=2))
        for cmd in commands:print(shlex.join(['sudo']+cmd))
        print('PLAN ONLY. Review physical ports and stop old GPU/data-plane work through the Gateway before apply.')
        return
    if not a.maintenance_approved or os.geteuid()!=0:
        raise SystemExit('Apply requires root and --maintenance-approved')
    statefile.parent.mkdir(parents=True,exist_ok=True)
    payload={'rank':a.rank,'management_ip':n['management_ip'],'interfaces':states,'timestamp':datetime.datetime.now().isoformat()}
    fd=os.open(statefile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(payload,f,indent=2)
    for cmd in commands:run(cmd)
    print('TEMPORARY_LINK_CONFIG_APPLIED. No netplan/default-route/sysctl/firewall change. Reboot may undo this.')

if __name__=='__main__':main()
