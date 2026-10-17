# BlueAgent Stress & Scalability Benchmark Report (alpine)

*Generated on:* `2026-10-16 19:34:06 UTC` on AMD Ryzen 7 7700X (8 Cores), 15 GiB RAM
*Image Alias:* `alpine` | *Memory Limit:* `128MB`

## 1. Synchronized Barrier Wall-Clock Timings

| Scenario | M | N | Peak Cts | Pass | Prov Wall (s) | Deploy Wall (s) | Verify Setup Wall (s) | Verify Clean Wall (s) | Teardown Wall (s) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Test 1 Baseline (alpine) | 1 | 1 | 2 | 10/10 | 0.1413 | 2.622 | 2.315 | 1.8401 | 1.8691 |
| Tier M=2 N=5 | 2 | 5 | 20 | 2/2 | 0.2546 | 18.2527 | 18.5869 | 4.1736 | 8.6869 |
| Tier M=2 N=7 | 2 | 7 | 28 | 2/2 | 0.2612 | 28.275 | 25.32 | 4.0116 | 11.2912 |
| Tier M=2 N=10 | 2 | 10 | 40 | 2/2 | 0.2206 | 33.5996 | 29.9447 | 5.3089 | 17.0197 |
| Tier M=5 N=5 | 5 | 5 | 50 | 5/5 | 0.5187 | 37.3675 | 28.4438 | 7.3411 | 14.802 |
| Tier M=5 N=7 | 5 | 7 | 70 | 5/5 | 0.6782 | 49.7489 | 39.6619 | 11.3286 | 17.1022 |
| Tier M=5 N=10 | 5 | 10 | 100 | 5/5 | 0.5276 | 74.042 | 54.7594 | 15.362 | 23.0426 |
| Tier M=10 N=2 | 10 | 2 | 40 | 10/10 | 1.2811 | 28.5911 | 22.2108 | 6.5902 | 10.3621 |
| Tier M=10 N=5 | 10 | 5 | 100 | 10/10 | 1.2405 | 72.2104 | 55.7169 | 16.7073 | 22.6892 |

## 2. Micro-Phase Breakdown & ZFS CoW Speedup

| Scenario | Dep Create Avg (s) | CoW Clone Avg (s) | **CoW Speedup** | OVN Create Avg (s) | Dep Start Avg (s) | Clone Start Avg (s) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Test 1 Baseline (alpine) | 0.9877 | 0.7508 | **1.32x** | 0.3039 | 1.2655 | 0.7186 |
| Tier M=2 N=5 | 8.1604 | 8.6456 | **0.94x** | 0.4325 | 5.7075 | 3.9597 |
| Tier M=2 N=7 | 13.4497 | 10.1289 | **1.33x** | 0.3143 | 8.8376 | 7.6555 |
| Tier M=2 N=10 | 15.6757 | 13.4926 | **1.16x** | 0.235 | 12.1784 | 7.3592 |
| Tier M=5 N=5 | 23.5718 | 8.4421 | **2.79x** | 1.5739 | 6.2823 | 5.6126 |
| Tier M=5 N=7 | 32.1078 | 14.1331 | **2.27x** | 1.0804 | 9.5204 | 8.1498 |
| Tier M=5 N=10 | 45.3599 | 19.9525 | **2.27x** | 1.3325 | 14.5475 | 11.6802 |
| Tier M=10 N=2 | 19.3224 | 5.2567 | **3.68x** | 3.0624 | 2.4255 | 3.3788 |
| Tier M=10 N=5 | 50.6115 | 14.0556 | **3.6x** | 3.2392 | 6.5424 | 7.9066 |

## 3. Step-by-Step Subprocess CPU & RAM Consumption

| Scenario | Deploy CPU Avg (%) | Deploy Peak RAM (MB) | Verify Setup CPU Avg (%) | Verify Peak RAM (MB) | Steady Net RAM Delta (+MB) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Test 1 Baseline (alpine) | 11.36% | 3477 MB | 5.95% | 3506 MB | +38 MB |
| Tier M=2 N=5 | 13.3593% | 3634 MB | 7.3367% | 3819 MB | +309 MB |
| Tier M=2 N=7 | 13.6898% | 3742 MB | 7.3612% | 3960 MB | +370 MB |
| Tier M=2 N=10 | 16.0714% | 3886 MB | 9.0472% | 4153 MB | +563 MB |
| Tier M=5 N=5 | 16.497% | 3941 MB | 12.3428% | 4375 MB | +729 MB |
| Tier M=5 N=7 | 19.403% | 4206 MB | 12.4092% | 4757 MB | +1028 MB |
| Tier M=5 N=10 | 19.7062% | 4432 MB | 13.2512% | 5282 MB | +1445 MB |
| Tier M=10 N=2 | 18.1407% | 4013 MB | 13.7414% | 4384 MB | +611 MB |
| Tier M=10 N=5 | 17.2067% | 4556 MB | 13.7901% | 5417 MB | +1533 MB |
