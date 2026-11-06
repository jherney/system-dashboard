import psutil
import time

for _ in range(2):
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent']):
        procs.append(p.info)
    procs.sort(key=lambda x: x['cpu_percent'] or 0.0, reverse=True)
    print(procs[:3])
    time.sleep(1)
