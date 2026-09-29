#!/usr/bin/env python3
"""Foreground, opt-in host-memory guard. It can stop only this bundle's labelled model container."""
import argparse
import json
import time
from datetime import datetime, timezone
from ringctl import mem_available_gib, inspect_container, stop_owned, LABEL_KEY, LABEL_VALUE
from common import CONTAINER

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--floor-gib', type=float, default=6.0)
    p.add_argument('--interval', type=float, default=2.0)
    p.add_argument('--consecutive', type=int, default=3)
    p.add_argument('--stop-on-low-memory', action='store_true')
    a = p.parse_args()
    if not (2 <= a.floor_gib <= 32 and 1 <= a.interval <= 60 and 1 <= a.consecutive <= 30):
        p.error('Invalid guard parameters')
    lows = 0
    try:
        while True:
            m = mem_available_gib()
            lows = lows + 1 if m < a.floor_gib else 0
            print(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'MemAvailable_GiB': round(m, 3),
                              'low_samples': lows, 'stop_enabled': a.stop_on_low_memory}), flush=True)
            if lows >= a.consecutive:
                print('LOW_MEMORY: record all rank logs; this is not proof that CUDA allocations are fully bounded', flush=True)
                if a.stop_on_low_memory:
                    info = inspect_container(CONTAINER)
                    if info and info.get('Config', {}).get('Labels', {}).get(LABEL_KEY) == LABEL_VALUE:
                        stop_owned(CONTAINER)
                        print('Stopped ONLY this node’s labelled dsv41-ring container. Stop the other ranks explicitly.')
                    else:
                        print('No owned model container to stop; unrelated processes unchanged')
                    return
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
