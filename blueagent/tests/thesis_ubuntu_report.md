# BlueAgent Stress & Scalability Benchmark Report (ubuntu-heavy)

*Generated on:* `2026-10-16 19:53:06 UTC` on AMD Ryzen 7 7700X (8 Cores), 15 GiB RAM
*Image Alias:* `ubuntu-heavy` | *Memory Limit:* `256MB`

## 1. Synchronized Barrier Wall-Clock Timings

| Scenario | M | N | Peak Cts | Pass | Prov Wall (s) | Deploy Wall (s) | Verify Setup Wall (s) | Verify Clean Wall (s) | Teardown Wall (s) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Test 1 Baseline (ubuntu-heavy) | 1 | 1 | 2 | 10/10 | 0.1639 | 2.9245 | 2.1657 | 3.5656 | 2.3107 |
| Tier M=2 N=5 | 2 | 5 | 20 | 2/2 | 0.2205 | 19.0763 | 16.7112 | 4.294 | 8.3389 |
| Tier M=2 N=7 | 2 | 7 | 28 | 2/2 | 0.335 | 30.0958 | 25.1701 | 5.7327 | 11.6093 |
| Tier M=2 N=10 | 2 | 10 | 40 | 2/2 | 0.2545 | 40.8113 | 36.8476 | 5.7449 | 18.6194 |
| Tier M=5 N=5 | 5 | 5 | 50 | 5/5 | 0.4607 | 45.85 | 35.2931 | 9.6809 | 13.5521 |
| Tier M=5 N=7 | 5 | 7 | 70 | 5/5 | 0.4648 | 67.6186 | 54.7203 | 12.5518 | 17.5317 |
| Tier M=5 N=10 | 5 | 10 | 100 | 5/5 | 0.5899 | 101.5395 | 78.8606 | 17.6765 | 25.3019 |
| Tier M=10 N=2 | 10 | 2 | 40 | 10/10 | 1.3898 | 36.442 | 28.8976 | 7.1999 | 9.6482 |
| Tier M=10 N=5 | 10 | 5 | 100 | 10/10 | 1.3258 | 103.1003 | 75.1486 | 18.0063 | 24.6532 |

## 2. Micro-Phase Breakdown & ZFS CoW Speedup

| Scenario | Dep Create Avg (s) | CoW Clone Avg (s) | **CoW Speedup** | OVN Create Avg (s) | Dep Start Avg (s) | Clone Start Avg (s) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Test 1 Baseline (ubuntu-heavy) | 1.2262 | 0.922 | **1.33x** | 0.2212 | 1.2982 | 0.4162 |
| Tier M=2 N=5 | 9.5127 | 8.6155 | **1.1x** | 0.3121 | 5.4626 | 2.5174 |
| Tier M=2 N=7 | 16.5008 | 15.3298 | **1.08x** | 0.3057 | 7.7879 | 3.2546 |
| Tier M=2 N=10 | 19.712 | 21.5027 | **0.92x** | 0.3175 | 15.2188 | 5.4072 |
| Tier M=5 N=5 | 30.1029 | 13.6168 | **2.21x** | 0.8888 | 7.7517 | 6.0963 |
| Tier M=5 N=7 | 42.816 | 21.75 | **1.97x** | 1.7505 | 12.2207 | 8.7305 |
| Tier M=5 N=10 | 63.2825 | 34.3464 | **1.84x** | 0.7969 | 19.1287 | 11.923 |
| Tier M=10 N=2 | 22.717 | 7.8123 | **2.91x** | 3.4078 | 3.2285 | 3.6046 |
| Tier M=10 N=5 | 76.1884 | 22.1704 | **3.44x** | 2.5958 | 9.2566 | 11.8493 |

## 3. Step-by-Step Subprocess CPU & RAM Consumption

| Scenario | Deploy CPU Avg (%) | Deploy Peak RAM (MB) | Verify Setup CPU Avg (%) | Verify Peak RAM (MB) | Steady Net RAM Delta (+MB) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Test 1 Baseline (ubuntu-heavy) | 10.04% | 3808 MB | 6.88% | 3881 MB | +98 MB |
| Tier M=2 N=5 | 16.3774% | 4281 MB | 14.0131% | 4860 MB | +973 MB |
| Tier M=2 N=7 | 15.4202% | 4506 MB | 13.1668% | 5309 MB | +1333 MB |
| Tier M=2 N=10 | 20.2883% | 4863 MB | 13.4101% | 6070 MB | +1976 MB |
| Tier M=5 N=5 | 22.0072% | 5163 MB | 18.1939% | 6751 MB | +2531 MB |
| Tier M=5 N=7 | 21.7191% | 5814 MB | 17.6309% | 7973 MB | +3568 MB |
| Tier M=5 N=10 | 23.102% | 6782 MB | 18.7158% | 9145 MB | +5156 MB |
| Tier M=10 N=2 | 23.1362% | 4584 MB | 18.4687% | 5686 MB | +1931 MB |
| Tier M=10 N=5 | 20.8592% | 6278 MB | 19.1284% | 9233 MB | +5247 MB |
