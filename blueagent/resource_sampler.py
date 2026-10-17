import argparse
import json
import os
import signal
import sys
import time

running = True

def handle_signal(sig, frame):
    global running
    running = False

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

def get_cpu_times():
    try:
        with open('/proc/stat', 'r') as f:
            for line in f:
                if line.startswith('cpu '):
                    parts = line.split()
                    # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
                    vals = [float(x) for x in parts[1:11]]
                    return vals
    except Exception:
        pass
    return [0.0] * 10

def get_ram_info():
    mem_total, mem_avail = 0, 0
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1]) // 1024
                elif line.startswith('MemAvailable:'):
                    mem_avail = int(line.split()[1]) // 1024
    except Exception:
        pass
    ram_used = max(0, mem_total - mem_avail)

    zfs_arc = 0
    try:
        with open('/proc/spl/kstat/zfs/arcstats', 'r') as f:
            for line in f:
                if line.startswith('size '):
                    parts = line.split()
                    zfs_arc = int(parts[2]) // (1024 * 1024)
                    break
    except Exception:
        pass

    net_ram = max(0, ram_used - zfs_arc)
    return ram_used, zfs_arc, net_ram

def main():
    parser = argparse.ArgumentParser(description='High-frequency resource sampler (100ms)')
    parser.add_argument('--output', type=str, required=True, help='Output JSONL path')
    parser.add_argument('--interval', type=float, default=0.1, help='Sampling interval in seconds')
    args = parser.parse_args()

    out_file = open(args.output, 'w', buffering=1)
    prev_cpu = get_cpu_times()
    prev_time = time.perf_counter()

    try:
        while running:
            time.sleep(args.interval)
            curr_time = time.perf_counter()
            curr_cpu = get_cpu_times()
            wall_ts = time.time()

            # CPU deltas
            idle_prev = prev_cpu[3] + prev_cpu[4]
            idle_curr = curr_cpu[3] + curr_cpu[4]
            idle_delta = idle_curr - idle_prev

            iowait_delta = curr_cpu[4] - prev_cpu[4]
            total_delta = sum(curr_cpu) - sum(prev_cpu)

            cpu_pct = 0.0
            iowait_pct = 0.0
            if total_delta > 0:
                cpu_pct = round(max(0.0, min(100.0, 100.0 * (1.0 - (idle_delta / total_delta)))), 2)
                iowait_pct = round(max(0.0, min(100.0, 100.0 * (iowait_delta / total_delta))), 2)

            prev_cpu = curr_cpu
            prev_time = curr_time

            ram_used, zfs_arc, net_ram = get_ram_info()

            record = {
                'mono': round(curr_time, 4),
                'ts': round(wall_ts, 4),
                'cpu_pct': cpu_pct,
                'iowait_pct': iowait_pct,
                'ram_used_mb': ram_used,
                'zfs_arc_mb': zfs_arc,
                'net_ram_mb': net_ram,
            }
            out_file.write(json.dumps(record) + '\n')
            out_file.flush()
    finally:
        out_file.close()

if __name__ == '__main__':
    main()
