import argparse
import asyncio
import csv
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

# Ensure blueagent and tests are importable
sys.path.insert(0, '/home/debian/bluectfd/blueagent/src')
sys.path.insert(0, '/home/debian/bluectfd/blueagent')
os.environ['BLUEAGENT_CTFD_API_TOKEN'] = 'secret-token'

from blueagent.engine.incus import IncusEngine
from tests.usability.client import BlueAgentTestClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('benchmark')


def percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(sorted_data[int(k)], 4)
    d0 = sorted_data[int(f)] * (c - k)
    d1 = sorted_data[int(c)] * (k - f)
    return round(d0 + d1, 4)


def calculate_stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {'avg': 0.0, 'p50': 0.0, 'p95': 0.0, 'min': 0.0, 'max': 0.0, 'std': 0.0}
    avg = sum(values) / len(values)
    variance = sum((x - avg) ** 2 for x in values) / len(values)
    return {
        'avg': round(avg, 4),
        'p50': percentile(values, 50),
        'p95': percentile(values, 95),
        'min': round(min(values), 4),
        'max': round(max(values), 4),
        'std': round(math.sqrt(variance), 4),
    }


def get_current_ram_and_arc() -> Dict[str, int]:
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
    return {'ram_used_mb': ram_used, 'zfs_arc_mb': zfs_arc, 'net_ram_mb': net_ram}


def build_challenge_payload(
    team_k: int,
    num_containers: int,
    image_alias: str = 'alpine',
    memory_limit: str = '128MB'
) -> Dict[str, Any]:
    containers = []
    verify_commands = []
    for i in range(num_containers):
        c_name = f'app{i+1}'
        containers.append({
            'name': c_name,
            'image': image_alias,
            'ip_suffix': i + 2,
            'limits_cpu': '1',
            'limits_memory': memory_limit
        })
        verify_commands.append({
            'target': 'container',
            'container_name': c_name,
            'cmd': ['echo', f'verified_{c_name}'],
            'timeout': 15,
            'interval': 1,
            'retry_timeout': 20
        })

    return {
        'challenge_id': f'ch-bench-{team_k}',
        'team_k': team_k,
        'containers': containers,
        'readiness_commands': [],
        'verify_commands': verify_commands,
        'verify_internet_access': False
    }


class BenchmarkRunner:
    def __init__(
        self,
        base_url: str = 'http://127.0.0.1:8000',
        token: str = 'secret-token',
        image_alias: str = 'alpine',
        memory_limit: str = '128MB',
        output_prefix: str = 'thesis_alpine',
    ):
        self.client = BlueAgentTestClient(base_url=base_url, token=token, timeout=900.0)
        self.image_alias = image_alias
        self.memory_limit = memory_limit
        self.output_prefix = output_prefix

        base_dir = '/home/debian/bluectfd/blueagent/tests'
        self.state_json_path = f'{base_dir}/{output_prefix}_state.json'
        self.csv_summary_path = f'{base_dir}/{output_prefix}_results.csv'
        self.csv_step_resources_path = f'{base_dir}/{output_prefix}_step_resources.csv'
        self.csv_timeseries_path = f'{base_dir}/{output_prefix}_timeseries.csv'
        self.csv_raw_samples_path = f'{base_dir}/{output_prefix}_raw_samples.csv'
        self.report_output_path = f'{base_dir}/{output_prefix}_report.md'

        self.results: List[Dict[str, Any]] = []
        self.step_resources: List[Dict[str, Any]] = []
        self.timeseries_samples: List[Dict[str, Any]] = []
        self.raw_samples: List[Dict[str, Any]] = []

    def load_existing_state(self):
        path = Path(self.state_json_path)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self.results = data.get('results', [])
                self.step_resources = data.get('step_resources', [])
                self.timeseries_samples = data.get('timeseries_samples', [])
                self.raw_samples = data.get('raw_samples', [])
                logger.info('Loaded existing state: %d results, %d step resource rows from %s',
                            len(self.results), len(self.step_resources), self.state_json_path)
            except Exception as e:
                logger.warning('Could not load state file %s: %s', self.state_json_path, e)

    def upsert_result(
        self,
        summary: Dict[str, Any],
        step_res_rows: List[Dict[str, Any]],
        timeseries_rows: List[Dict[str, Any]],
        raw_rows: List[Dict[str, Any]]
    ):
        test_name = summary['test_name']
        self.results = [r for r in self.results if r.get('test_name') != test_name]
        self.results.append(summary)

        self.step_resources = [r for r in self.step_resources if r.get('test_name') != test_name]
        self.step_resources.extend(step_res_rows)

        self.timeseries_samples = [r for r in self.timeseries_samples if r.get('test_name') != test_name]
        self.timeseries_samples.extend(timeseries_rows)

        self.raw_samples = [s for s in self.raw_samples if s.get('test_name') != test_name]
        self.raw_samples.extend(raw_rows)

        # Immediately flush all outputs
        self.save_state_json()
        self.export_summary_csv()
        self.export_step_resources_csv()
        self.export_timeseries_csv()
        self.export_raw_samples_csv()
        self.export_markdown_report()

    def save_state_json(self):
        Path(self.state_json_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_json_path, 'w') as f:
            json.dump({
                'results': self.results,
                'step_resources': self.step_resources,
                'raw_samples': self.raw_samples,
                # Store sample count to keep JSON manageable if needed
                'timeseries_sample_count': len(self.timeseries_samples),
            }, f, indent=2)

    async def ensure_clean_environment(self, team_ids: List[int]):
        """Parallel cleanup of BlueAgent teams + direct Incus sweep of any orphaned verify-* resources."""
        logger.info('Performing parallel pre-flight cleanup for teams: %s', team_ids)

        async def _clean_team(k: int):
            try:
                await self.client.cleanup_verify(team_k=k)
            except Exception:
                pass
            try:
                await self.client.teardown_challenge(team_k=k)
            except Exception:
                pass
            try:
                await self.client.delete_team(team_k=k)
            except Exception:
                pass

        await asyncio.gather(*[_clean_team(k) for k in team_ids])

        # Sweep any orphaned verify-* containers or OVN networks directly via Incus UDS
        engine = IncusEngine(socket_path='/var/lib/incus/unix.socket')
        try:
            instances = await engine.list_instances()
            orphan_insts = [
                name for name in instances
                if name.startswith('verify-') or name.startswith('c-')
            ]
            if orphan_insts:
                logger.warning('Sweeping %d orphaned Incus instances: %s', len(orphan_insts), orphan_insts)
                await asyncio.gather(*[
                    engine.delete_instance(name, force=True) for name in orphan_insts
                ], return_exceptions=True)

            networks = await engine.list_networks()
            orphan_nets = [
                name for name in networks
                if name.startswith('verify-')
            ]
            if orphan_nets:
                logger.warning('Sweeping %d orphaned OVN networks: %s', len(orphan_nets), orphan_nets)
                await asyncio.gather(*[
                    engine.delete_network(name) for name in orphan_nets
                ], return_exceptions=True)
        except Exception as e:
            logger.warning('Incus orphan sweep warning: %s', e)
        finally:
            await engine.aclose()

    async def _timed_create_team(self, k: int, name: str) -> Tuple[int, float, int]:
        t0 = time.perf_counter()
        r = await self.client.create_team(team_k=k, name=name)
        return k, round(time.perf_counter() - t0, 4), r.status_code

    async def _timed_deploy(self, k: int, payload: Dict[str, Any]) -> Tuple[int, float, httpx.Response]:
        t0 = time.perf_counter()
        r = await self.client.deploy_challenge(payload, team_k=k)
        return k, round(time.perf_counter() - t0, 4), r

    async def _timed_verify_setup(self, k: int, payload: Dict[str, Any]) -> Tuple[int, float, httpx.Response]:
        t0 = time.perf_counter()
        # skip_cleanup=True keeps clones running until Barrier 4
        r = await self.client.verify_challenge(team_k=k, config_payload=payload, skip_cleanup=True)
        return k, round(time.perf_counter() - t0, 4), r

    async def _timed_verify_cleanup(self, k: int, payload: Dict[str, Any]) -> Tuple[int, float, httpx.Response]:
        t0 = time.perf_counter()
        r = await self.client.cleanup_verify(team_k=k, config_payload=payload)
        return k, round(time.perf_counter() - t0, 4), r

    async def _timed_teardown_machines(self, k: int) -> Tuple[int, float, int]:
        t0 = time.perf_counter()
        r = await self.client.teardown_challenge(team_k=k)
        return k, round(time.perf_counter() - t0, 4), r.status_code

    async def _timed_delete_team(self, k: int) -> Tuple[int, float, int]:
        t0 = time.perf_counter()
        r = await self.client.delete_team(team_k=k)
        return k, round(time.perf_counter() - t0, 4), r.status_code

    # --------------------------------------------------------------------------
    # Core Execution of a Single (M, N) Scenario or Cycle with 6 Barriers
    # --------------------------------------------------------------------------
    async def execute_barrier_scenario(
        self,
        test_name: str,
        test_type: str,
        m: int,
        n: int,
        cycle_idx: Optional[int] = None,
        clean_before: bool = True,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        team_ids = list(range(1, m + 1))
        if clean_before:
            await self.ensure_clean_environment(team_ids)

        # Baseline RAM before starting scenario
        baseline_info = get_current_ram_and_arc()
        base_net_ram = baseline_info['net_ram_mb']
        base_total_ram = baseline_info['ram_used_mb']
        base_arc = baseline_info['zfs_arc_mb']

        # Spawn high-frequency 100ms resource sampler subprocess
        sampler_tmp = f'/tmp/sampler_{test_name.replace(" ", "_")}_{cycle_idx or 1}.jsonl'
        if os.path.exists(sampler_tmp):
            os.remove(sampler_tmp)

        sampler_proc = subprocess.Popen([
            sys.executable,
            '/home/debian/bluectfd/blueagent/resource_sampler.py',
            '--output', sampler_tmp,
            '--interval', '0.1'
        ])

        t_scenario_start = time.perf_counter()
        step_windows: Dict[str, Tuple[float, float]] = {}

        try:
            # ------------------------------------------------------------------
            # BARRIER 1: Provision M Teams in parallel & wait
            # ------------------------------------------------------------------
            t_prov_start = time.perf_counter()
            prov_results = await asyncio.gather(*[
                self._timed_create_team(k, f'{test_name} Team {k}')
                for k in team_ids
            ])
            t_prov_end = time.perf_counter()
            step_windows['provision_teams'] = (t_prov_start, t_prov_end)
            prov_wall_sec = round(t_prov_end - t_prov_start, 4)
            prov_times_map = {k: elapsed for k, elapsed, _ in prov_results}

            # ------------------------------------------------------------------
            # BARRIER 2: Deploy M x N Containers in parallel & wait
            # ------------------------------------------------------------------
            payloads = {
                k: build_challenge_payload(
                    team_k=k,
                    num_containers=n,
                    image_alias=self.image_alias,
                    memory_limit=self.memory_limit,
                ) for k in team_ids
            }
            t_dep_start = time.perf_counter()
            dep_results = await asyncio.gather(*[
                self._timed_deploy(k, payloads[k])
                for k in team_ids
            ])
            t_dep_end = time.perf_counter()
            step_windows['deploy_challenges'] = (t_dep_start, t_dep_end)
            dep_wall_sec = round(t_dep_end - t_dep_start, 4)

            # Settle pause to capture steady-state live containers RAM
            await asyncio.sleep(1.0)
            steady_live_info = get_current_ram_and_arc()

            # ------------------------------------------------------------------
            # BARRIER 3: Verify Setup (Start Verify) in parallel with skip_cleanup=True
            # ------------------------------------------------------------------
            t_ver_setup_start = time.perf_counter()
            ver_setup_results = await asyncio.gather(*[
                self._timed_verify_setup(k, payloads[k])
                for k in team_ids
            ])
            t_ver_setup_end = time.perf_counter()
            step_windows['verify_setup'] = (t_ver_setup_start, t_ver_setup_end)
            ver_setup_wall_sec = round(t_ver_setup_end - t_ver_setup_start, 4)

            # Settle pause to capture steady-state PEAK RAM with ALL 2*M*N containers alive!
            await asyncio.sleep(1.0)
            steady_peak_info = get_current_ram_and_arc()

            # ------------------------------------------------------------------
            # BARRIER 4: Verify Cleanup (Destroy Clones + OVN Networks) in parallel
            # ------------------------------------------------------------------
            t_ver_clean_start = time.perf_counter()
            ver_clean_results = await asyncio.gather(*[
                self._timed_verify_cleanup(k, payloads[k])
                for k in team_ids
            ])
            t_ver_clean_end = time.perf_counter()
            step_windows['verify_cleanup'] = (t_ver_clean_start, t_ver_clean_end)
            ver_clean_wall_sec = round(t_ver_clean_end - t_ver_clean_start, 4)

            # ------------------------------------------------------------------
            # BARRIER 5: Teardown Machines in parallel
            # ------------------------------------------------------------------
            t_mach_start = time.perf_counter()
            tear_mach_results = await asyncio.gather(*[
                self._timed_teardown_machines(k)
                for k in team_ids
            ])
            t_mach_end = time.perf_counter()
            step_windows['teardown_machines'] = (t_mach_start, t_mach_end)
            tear_machines_wall_sec = round(t_mach_end - t_mach_start, 4)

            # ------------------------------------------------------------------
            # BARRIER 6: Teardown Teams in parallel
            # ------------------------------------------------------------------
            t_team_start = time.perf_counter()
            tear_team_results = await asyncio.gather(*[
                self._timed_delete_team(k)
                for k in team_ids
            ])
            t_team_end = time.perf_counter()
            step_windows['teardown_teams'] = (t_team_start, t_team_end)
            tear_teams_wall_sec = round(t_team_end - t_team_start, 4)

            teardown_wall_sec = round((t_mach_end - t_mach_start) + (t_team_end - t_team_start), 4)
            steady_teardown_info = get_current_ram_and_arc()

        finally:
            # Terminate sampler subprocess cleanly
            sampler_proc.terminate()
            try:
                sampler_proc.wait(timeout=2.0)
            except Exception:
                sampler_proc.kill()

        # Parse 100ms samples from sampler JSONL
        raw_samples_list: List[Dict[str, Any]] = []
        if os.path.exists(sampler_tmp):
            try:
                with open(sampler_tmp, 'r') as f:
                    for line in f:
                        if line.strip():
                            raw_samples_list.append(json.loads(line))
            except Exception as e:
                logger.warning('Failed reading sampler file %s: %s', sampler_tmp, e)
            try:
                os.remove(sampler_tmp)
            except Exception:
                pass

        # Compute per-step resource metrics from the 100ms samples
        step_metrics: Dict[str, Dict[str, float]] = {}
        timeseries_rows: List[Dict[str, Any]] = []

        step_order = ['provision_teams', 'deploy_challenges', 'verify_setup', 'verify_cleanup', 'teardown_machines', 'teardown_teams']
        for step_name in step_order:
            w_start, w_end = step_windows.get(step_name, (0.0, 0.0))
            duration = round(w_end - w_start, 4)
            step_samples = [s for s in raw_samples_list if w_start <= s['mono'] <= w_end]

            # Label samples for timeseries CSV
            for s in step_samples:
                timeseries_rows.append({
                    'test_name': test_name,
                    'image_alias': self.image_alias,
                    'teams_m': m,
                    'containers_n': n,
                    'cycle': cycle_idx or 1,
                    'step_name': step_name,
                    'elapsed_sec': round(s['mono'] - t_scenario_start, 4),
                    'cpu_pct': s['cpu_pct'],
                    'iowait_pct': s['iowait_pct'],
                    'ram_used_mb': s['ram_used_mb'],
                    'zfs_arc_mb': s['zfs_arc_mb'],
                    'net_ram_mb': s['net_ram_mb'],
                })

            cpu_vals = [s['cpu_pct'] for s in step_samples]
            iowait_vals = [s['iowait_pct'] for s in step_samples]
            ram_vals = [s['ram_used_mb'] for s in step_samples]
            arc_vals = [s['zfs_arc_mb'] for s in step_samples]
            net_ram_vals = [s['net_ram_mb'] for s in step_samples]

            cpu_stats = calculate_stats(cpu_vals)
            iowait_stats = calculate_stats(iowait_vals)
            ram_stats = calculate_stats(ram_vals)
            arc_stats = calculate_stats(arc_vals)
            net_ram_stats = calculate_stats(net_ram_vals)

            step_metrics[step_name] = {
                'duration_sec': duration,
                'cpu_avg_pct': cpu_stats['avg'],
                'cpu_p95_pct': cpu_stats['p95'],
                'cpu_max_pct': cpu_stats['max'],
                'iowait_avg_pct': iowait_stats['avg'],
                'iowait_max_pct': iowait_stats['max'],
                'ram_avg_mb': ram_stats['avg'],
                'ram_peak_mb': ram_stats['max'],
                'zfs_arc_avg_mb': arc_stats['avg'],
                'net_ram_avg_mb': net_ram_stats['avg'],
                'net_ram_peak_mb': net_ram_stats['max'],
                'net_ram_delta_mb': round(max(0.0, net_ram_stats['max'] - base_net_ram), 2),
            }

        # Build step resource rows for long-format CSV
        step_res_rows: List[Dict[str, Any]] = []
        for step_name in step_order:
            sm = step_metrics[step_name]
            step_res_rows.append({
                'test_name': test_name,
                'image_alias': self.image_alias,
                'teams_m': m,
                'containers_n': n,
                'cycle': cycle_idx or 1,
                'step_name': step_name,
                'duration_sec': sm['duration_sec'],
                'cpu_avg_pct': sm['cpu_avg_pct'],
                'cpu_p95_pct': sm['cpu_p95_pct'],
                'cpu_max_pct': sm['cpu_max_pct'],
                'iowait_avg_pct': sm['iowait_avg_pct'],
                'iowait_max_pct': sm['iowait_max_pct'],
                'ram_avg_mb': sm['ram_avg_mb'],
                'ram_peak_mb': sm['ram_peak_mb'],
                'zfs_arc_avg_mb': sm['zfs_arc_avg_mb'],
                'net_ram_avg_mb': sm['net_ram_avg_mb'],
                'net_ram_peak_mb': sm['net_ram_peak_mb'],
                'net_ram_delta_mb': sm['net_ram_delta_mb'],
            })

        # Parse per-team metrics
        dep_map = {k: (elapsed, r) for k, elapsed, r in dep_results}
        ver_setup_map = {k: (elapsed, r) for k, elapsed, r in ver_setup_results}
        ver_clean_map = {k: (elapsed, r) for k, elapsed, r in ver_clean_results}
        tear_mach_map = {k: elapsed for k, elapsed, _ in tear_mach_results}
        tear_team_map = {k: elapsed for k, elapsed, _ in tear_team_results}

        raw_rows: List[Dict[str, Any]] = []
        errors: List[str] = []
        pass_count = 0

        for k in team_ids:
            prov_sec = prov_times_map[k]
            dep_http_sec, r_dep = dep_map[k]
            ver_http_sec, r_ver = ver_setup_map[k]
            clean_http_sec, r_clean = ver_clean_map[k]
            tm_sec = tear_mach_map[k]
            tt_sec = tear_team_map[k]

            dep_st = {}
            if r_dep.status_code in (200, 201):
                dep_st = r_dep.json().get('step_timings', {})
            else:
                errors.append(f'Team {k} Deploy HTTP {r_dep.status_code}: {r_dep.text[:120]}')

            ver_st = {}
            sb = {}
            ver_passed = False
            if r_ver.status_code == 200:
                v_json = r_ver.json()
                ver_passed = bool(v_json.get('passed'))
                if ver_passed:
                    pass_count += 1
                ver_st = v_json.get('details', {}).get('step_timings', {})
                sb = ver_st.get('sandbox_setup', {})
            else:
                errors.append(f'Team {k} Verify HTTP {r_ver.status_code}: {r_ver.text[:150]}')
                logger.error('[%s] Team %d verify error: %s', test_name, k, r_ver.text[:200])

            cl = {}
            if r_clean.status_code == 200:
                cl = r_clean.json().get('step_timings', {})

            row = {
                'test_name': test_name,
                'image_alias': self.image_alias,
                'test_type': test_type,
                'teams_m': m,
                'containers_n': n,
                'cycle': cycle_idx if cycle_idx is not None else 1,
                'team_k': k,
                'http_deploy_status': r_dep.status_code,
                'http_verify_status': r_ver.status_code,
                'verify_passed': int(ver_passed),
                # Sub-stage 1: Provision
                'provision_team_sec': prov_sec,
                # Sub-stage 2: Deploy
                'deploy_http_sec': dep_http_sec,
                'deploy_total_sec': dep_st.get('total_deploy_sec', dep_http_sec),
                'deploy_validation_sec': dep_st.get('validation_sec', 0.0),
                'deploy_create_sec': dep_st.get('total_create_sec', 0.0),
                'deploy_push_sec': dep_st.get('total_push_sec', 0.0),
                'deploy_start_sec': dep_st.get('total_start_sec', 0.0),
                'deploy_runtime_net_sec': dep_st.get('total_runtime_net_sec', 0.0),
                'deploy_db_record_sec': dep_st.get('total_db_record_sec', 0.0),
                # Sub-stage 3: Verify Setup (Start Verify)
                'verify_setup_http_sec': ver_http_sec,
                'verify_setup_total_sec': sb.get('total_setup_sec', 0.0),
                'verify_ovn_net_create_sec': sb.get('ovn_network_create_sec', 0.0),
                'verify_cow_clone_sec': sb.get('total_clone_time_sec', 0.0),
                'verify_clone_connect_sec': sb.get('total_connect_time_sec', 0.0),
                'verify_clone_start_sec': sb.get('total_start_time_sec', 0.0),
                'verify_clone_runtime_net_sec': sb.get('total_runtime_net_sec', 0.0),
                'verify_runner_sec': sb.get('verifier_runner_sec', 0.0),
                'verify_readiness_sec': ver_st.get('readiness_sec', 0.0),
                'verify_commands_sec': ver_st.get('verify_commands_sec', 0.0),
                # Sub-stage 4: Verify Cleanup
                'verify_cleanup_http_sec': clean_http_sec,
                'verify_cleanup_clones_sec': cl.get('clones_teardown_sec', 0.0),
                'verify_cleanup_ovn_net_sec': cl.get('ovn_network_delete_sec', 0.0),
                'verify_cleanup_total_sec': cl.get('total_cleanup_sec', clean_http_sec),
                # Sub-stage 5 & 6: Teardown
                'teardown_machines_sec': tm_sec,
                'teardown_team_sec': tt_sec,
                'teardown_total_sec': round(tm_sec + tt_sec, 4),
            }
            raw_rows.append(row)

        summary = self._aggregate_rows_to_summary(
            test_name=test_name,
            test_type=test_type,
            m=m,
            n=n,
            cycles=1,
            raw_rows=raw_rows,
            prov_wall_sec=prov_wall_sec,
            dep_wall_sec=dep_wall_sec,
            ver_setup_wall_sec=ver_setup_wall_sec,
            ver_clean_wall_sec=ver_clean_wall_sec,
            tear_machines_wall_sec=tear_machines_wall_sec,
            tear_teams_wall_sec=tear_teams_wall_sec,
            teardown_wall_sec=teardown_wall_sec,
            step_metrics=step_metrics,
            base_total_ram=base_total_ram,
            base_arc=base_arc,
            base_net_ram=base_net_ram,
            steady_live_info=steady_live_info,
            steady_peak_info=steady_peak_info,
            steady_teardown_info=steady_teardown_info,
            errors=errors,
        )
        return summary, step_res_rows, timeseries_rows, raw_rows

    def _aggregate_rows_to_summary(
        self,
        test_name: str,
        test_type: str,
        m: int,
        n: int,
        cycles: int,
        raw_rows: List[Dict[str, Any]],
        prov_wall_sec: float,
        dep_wall_sec: float,
        ver_setup_wall_sec: float,
        ver_clean_wall_sec: float,
        tear_machines_wall_sec: float,
        tear_teams_wall_sec: float,
        teardown_wall_sec: float,
        step_metrics: Dict[str, Dict[str, float]],
        base_total_ram: int,
        base_arc: int,
        base_net_ram: int,
        steady_live_info: Dict[str, int],
        steady_peak_info: Dict[str, int],
        steady_teardown_info: Dict[str, int],
        errors: List[str],
    ) -> Dict[str, Any]:
        ok_dep = [r for r in raw_rows if r['http_deploy_status'] in (200, 201)]
        ok_ver = [r for r in raw_rows if r['http_verify_status'] == 200 and r['verify_passed'] == 1]
        total_attempts = len(raw_rows)
        passed_count = len(ok_ver)
        failed_count = total_attempts - passed_count

        def col_stats(rows: List[Dict[str, Any]], key: str) -> Dict[str, float]:
            return calculate_stats([float(r[key]) for r in rows])

        return {
            'test_name': test_name,
            'image_alias': self.image_alias,
            'test_type': test_type,
            'teams_count': m,
            'containers_per_team': n,
            'cycles': cycles,
            'total_containers_live': m * n,
            'peak_containers_verify': 2 * m * n,
            'passed_count': passed_count,
            'failed_count': failed_count,
            'passed_ratio': f'{passed_count}/{total_attempts}',
            'pass_rate_pct': round((passed_count / total_attempts) * 100.0, 2) if total_attempts else 0.0,
            # Wall-clock times across the 6 barriers
            'provision_wall_sec': prov_wall_sec,
            'deploy_wall_sec': dep_wall_sec,
            'verify_setup_wall_sec': ver_setup_wall_sec,
            'verify_cleanup_wall_sec': ver_clean_wall_sec,
            'teardown_machines_wall_sec': tear_machines_wall_sec,
            'teardown_teams_wall_sec': tear_teams_wall_sec,
            'teardown_wall_sec': teardown_wall_sec,
            # Per-team macro stats
            'provision_stats': col_stats(raw_rows, 'provision_team_sec'),
            'deploy_stats': col_stats(ok_dep, 'deploy_total_sec'),
            'verify_setup_stats': col_stats(ok_ver, 'verify_setup_total_sec'),
            'verify_cleanup_stats': col_stats(raw_rows, 'verify_cleanup_total_sec'),
            'teardown_machines_stats': col_stats(raw_rows, 'teardown_machines_sec'),
            'teardown_team_stats': col_stats(raw_rows, 'teardown_team_sec'),
            'teardown_total_stats': col_stats(raw_rows, 'teardown_total_sec'),
            # Micro-phase stats
            'phase_stats': {
                'deploy_validation_sec': col_stats(ok_dep, 'deploy_validation_sec'),
                'deploy_create_sec': col_stats(ok_dep, 'deploy_create_sec'),
                'deploy_push_sec': col_stats(ok_dep, 'deploy_push_sec'),
                'deploy_start_sec': col_stats(ok_dep, 'deploy_start_sec'),
                'deploy_runtime_net_sec': col_stats(ok_dep, 'deploy_runtime_net_sec'),
                'deploy_db_record_sec': col_stats(ok_dep, 'deploy_db_record_sec'),
                'ovn_create_sec': col_stats(ok_ver, 'verify_ovn_net_create_sec'),
                'cow_clone_sec': col_stats(ok_ver, 'verify_cow_clone_sec'),
                'clone_connect_sec': col_stats(ok_ver, 'verify_clone_connect_sec'),
                'clone_start_sec': col_stats(ok_ver, 'verify_clone_start_sec'),
                'clone_runtime_net_sec': col_stats(ok_ver, 'verify_clone_runtime_net_sec'),
                'verify_runner_sec': col_stats(ok_ver, 'verify_runner_sec'),
                'verify_readiness_sec': col_stats(ok_ver, 'verify_readiness_sec'),
                'verify_cmds_sec': col_stats(ok_ver, 'verify_commands_sec'),
                'cleanup_clones_sec': col_stats(raw_rows, 'verify_cleanup_clones_sec'),
                'cleanup_ovn_net_sec': col_stats(raw_rows, 'verify_cleanup_ovn_net_sec'),
            },
            # Step CPU & RAM Subprocess Metrics
            'step_metrics': step_metrics,
            # Baseline & Steady-State RAM Snapshots
            'base_total_ram_mb': base_total_ram,
            'base_arc_mb': base_arc,
            'base_net_ram_mb': base_net_ram,
            'steady_live_ram_mb': steady_live_info['ram_used_mb'],
            'steady_live_arc_mb': steady_live_info['zfs_arc_mb'],
            'steady_live_net_ram_mb': steady_live_info['net_ram_mb'],
            'steady_live_net_delta_mb': max(0, steady_live_info['net_ram_mb'] - base_net_ram),
            'steady_peak_ram_mb': steady_peak_info['ram_used_mb'],
            'steady_peak_arc_mb': steady_peak_info['zfs_arc_mb'],
            'steady_peak_net_ram_mb': steady_peak_info['net_ram_mb'],
            'steady_peak_net_delta_mb': max(0, steady_peak_info['net_ram_mb'] - base_net_ram),
            'steady_teardown_net_ram_mb': steady_teardown_info['net_ram_mb'],
            'errors': errors,
        }

    # --------------------------------------------------------------------------
    # TEST 1: Single-Team Baseline (M=1, N=1, 10 cycles)
    # --------------------------------------------------------------------------
    async def run_test_1(self, cycles: int = 10):
        logger.info('===============================================================')
        logger.info('TEST 1: SINGLE-TEAM BASELINE (Image=%s, M=1, N=1, %d cycles)', self.image_alias, cycles)
        logger.info('===============================================================')

        await self.ensure_clean_environment([1])
        all_cycle_rows: List[Dict[str, Any]] = []
        all_step_res: List[Dict[str, Any]] = []
        all_timeseries: List[Dict[str, Any]] = []
        all_errors: List[str] = []

        prov_walls, dep_walls, ver_setup_walls, ver_clean_walls, tm_walls, tt_walls, tear_walls = [], [], [], [], [], [], []

        for c in range(1, cycles + 1):
            logger.info('[Test 1] Cycle %d/%d starting...', c, cycles)
            summary_c, step_res_c, ts_c, rows_c = await self.execute_barrier_scenario(
                test_name=f'Test 1 Baseline ({self.image_alias})',
                test_type='test_1_baseline',
                m=1,
                n=1,
                cycle_idx=c,
                clean_before=False,
            )
            all_cycle_rows.extend(rows_c)
            all_step_res.extend(step_res_c)
            all_timeseries.extend(ts_c)
            all_errors.extend(summary_c['errors'])

            prov_walls.append(summary_c['provision_wall_sec'])
            dep_walls.append(summary_c['deploy_wall_sec'])
            ver_setup_walls.append(summary_c['verify_setup_wall_sec'])
            ver_clean_walls.append(summary_c['verify_cleanup_wall_sec'])
            tm_walls.append(summary_c['teardown_machines_wall_sec'])
            tt_walls.append(summary_c['teardown_teams_wall_sec'])
            tear_walls.append(summary_c['teardown_wall_sec'])

            logger.info(
                '[Test 1] Cycle %d/%d: Prov=%.3fs, Deploy=%.3fs (CPU=%.1f%%), VerifySetup=%.3fs (CPU=%.1f%%, NetRAM=+%dMB), Teardown=%.3fs',
                c, cycles, summary_c['provision_wall_sec'], summary_c['deploy_wall_sec'],
                summary_c['step_metrics']['deploy_challenges']['cpu_avg_pct'],
                summary_c['verify_setup_wall_sec'],
                summary_c['step_metrics']['verify_setup']['cpu_avg_pct'],
                summary_c['step_metrics']['verify_setup']['net_ram_delta_mb'],
                summary_c['teardown_wall_sec']
            )

        # Average step metrics across cycles for summary
        avg_step_metrics: Dict[str, Dict[str, float]] = {}
        for step_name in ['provision_teams', 'deploy_challenges', 'verify_setup', 'verify_cleanup', 'teardown_machines', 'teardown_teams']:
            step_rows = [r for r in all_step_res if r['step_name'] == step_name]
            avg_step_metrics[step_name] = {
                'duration_sec': round(sum(r['duration_sec'] for r in step_rows) / len(step_rows), 4),
                'cpu_avg_pct': round(sum(r['cpu_avg_pct'] for r in step_rows) / len(step_rows), 2),
                'cpu_p95_pct': round(sum(r['cpu_p95_pct'] for r in step_rows) / len(step_rows), 2),
                'cpu_max_pct': round(max(r['cpu_max_pct'] for r in step_rows), 2),
                'iowait_avg_pct': round(sum(r['iowait_avg_pct'] for r in step_rows) / len(step_rows), 2),
                'iowait_max_pct': round(max(r['iowait_max_pct'] for r in step_rows), 2),
                'ram_avg_mb': round(sum(r['ram_avg_mb'] for r in step_rows) / len(step_rows), 2),
                'ram_peak_mb': round(max(r['ram_peak_mb'] for r in step_rows), 2),
                'zfs_arc_avg_mb': round(sum(r['zfs_arc_avg_mb'] for r in step_rows) / len(step_rows), 2),
                'net_ram_avg_mb': round(sum(r['net_ram_avg_mb'] for r in step_rows) / len(step_rows), 2),
                'net_ram_peak_mb': round(max(r['net_ram_peak_mb'] for r in step_rows), 2),
                'net_ram_delta_mb': round(sum(r['net_ram_delta_mb'] for r in step_rows) / len(step_rows), 2),
            }

        last_s = summary_c
        final_summary = self._aggregate_rows_to_summary(
            test_name=f'Test 1 Baseline ({self.image_alias})',
            test_type='test_1_baseline',
            m=1,
            n=1,
            cycles=cycles,
            raw_rows=all_cycle_rows,
            prov_wall_sec=round(sum(prov_walls) / len(prov_walls), 4),
            dep_wall_sec=round(sum(dep_walls) / len(dep_walls), 4),
            ver_setup_wall_sec=round(sum(ver_setup_walls) / len(ver_setup_walls), 4),
            ver_clean_wall_sec=round(sum(ver_clean_walls) / len(ver_clean_walls), 4),
            tear_machines_wall_sec=round(sum(tm_walls) / len(tm_walls), 4),
            tear_teams_wall_sec=round(sum(tt_walls) / len(tt_walls), 4),
            teardown_wall_sec=round(sum(tear_walls) / len(tear_walls), 4),
            step_metrics=avg_step_metrics,
            base_total_ram=last_s['base_total_ram_mb'],
            base_arc=last_s['base_arc_mb'],
            base_net_ram=last_s['base_net_ram_mb'],
            steady_live_info={'ram_used_mb': last_s['steady_live_ram_mb'], 'zfs_arc_mb': last_s['steady_live_arc_mb'], 'net_ram_mb': last_s['steady_live_net_ram_mb']},
            steady_peak_info={'ram_used_mb': last_s['steady_peak_ram_mb'], 'zfs_arc_mb': last_s['steady_peak_arc_mb'], 'net_ram_mb': last_s['steady_peak_net_ram_mb']},
            steady_teardown_info={'ram_used_mb': 0, 'zfs_arc_mb': 0, 'net_ram_mb': last_s['steady_teardown_net_ram_mb']},
            errors=all_errors,
        )

        self.upsert_result(final_summary, all_step_res, all_timeseries, all_cycle_rows)
        logger.info('[+] Test 1 Completed and flushed to CSV/Markdown.')
        return final_summary

    # --------------------------------------------------------------------------
    # 8-Scenario Thesis Stress Matrix
    # --------------------------------------------------------------------------
    async def run_scenarios(self, scenarios: List[Tuple[int, int]]):
        logger.info('===============================================================')
        logger.info('RUNNING SCENARIO MATRIX (Image=%s, %d Scenarios)', self.image_alias, len(scenarios))
        logger.info('===============================================================')

        for m, n in scenarios:
            test_name = f'Tier M={m} N={n}'
            logger.info('>>> STARTING: M=%d teams, N=%d containers/team (Peak Instances: %d) <<<',
                        m, n, 2 * m * n)
            summary, step_res, ts, raw_rows = await self.execute_barrier_scenario(
                test_name=test_name,
                test_type='matrix_and_test3' if n == 2 else 'matrix',
                m=m,
                n=n,
                cycle_idx=1,
                clean_before=True,
            )
            self.upsert_result(summary, step_res, ts, raw_rows)
            sm_ver = summary['step_metrics']['verify_setup']
            sm_dep = summary['step_metrics']['deploy_challenges']
            logger.info(
                '[+] Completed %s: Prov=%.2fs, Deploy=%.2fs (CPU=%.1f%%), VerifySetup=%.2fs (CPU=%.1f%%, NetRAM=+%dMB), Teardown=%.2fs',
                test_name, summary['provision_wall_sec'], summary['deploy_wall_sec'],
                sm_dep['cpu_avg_pct'], summary['verify_setup_wall_sec'],
                sm_ver['cpu_avg_pct'], summary['steady_peak_net_delta_mb'],
                summary['teardown_wall_sec']
            )

        # Final cleanup
        max_m = max([m for m, n in scenarios]) if scenarios else 10
        await self.ensure_clean_environment(list(range(1, max_m + 1)))

    # --------------------------------------------------------------------------
    # CSV & Markdown Exporters
    # --------------------------------------------------------------------------
    def export_summary_csv(self):
        filename = self.csv_summary_path
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        headers = [
            'test_name', 'image_alias', 'test_type', 'teams_m', 'containers_per_team_n', 'cycles',
            'live_containers', 'peak_verify_containers', 'passed_count', 'failed_count', 'pass_rate_pct',
            # Wall-clock times
            'provision_wall_sec', 'deploy_wall_sec', 'verify_setup_wall_sec', 'verify_cleanup_wall_sec',
            'teardown_machines_wall_sec', 'teardown_teams_wall_sec', 'teardown_wall_sec',
            # Micro-phase timings (Deploy)
            'deploy_validation_avg_sec', 'deploy_create_avg_sec', 'deploy_create_p50_sec', 'deploy_create_p95_sec',
            'deploy_push_avg_sec', 'deploy_start_avg_sec', 'deploy_runtime_net_avg_sec', 'deploy_db_record_avg_sec',
            # Micro-phase timings (Verify)
            'ovn_create_avg_sec', 'cow_clone_avg_sec', 'cow_clone_p50_sec', 'cow_clone_p95_sec',
            'clone_connect_avg_sec', 'clone_start_avg_sec', 'clone_runtime_net_avg_sec',
            'cleanup_clones_avg_sec', 'cleanup_ovn_net_avg_sec',
            # Steady-State RAM Deltas (MB)
            'base_net_ram_mb', 'steady_live_net_delta_mb', 'steady_peak_net_delta_mb',
            # Subprocess CPU & RAM Metrics per Step
            'deploy_cpu_avg_pct', 'deploy_cpu_max_pct', 'deploy_iowait_avg_pct', 'deploy_ram_peak_mb',
            'verify_setup_cpu_avg_pct', 'verify_setup_cpu_max_pct', 'verify_setup_iowait_avg_pct', 'verify_setup_ram_peak_mb',
            'verify_cleanup_cpu_avg_pct', 'verify_cleanup_ram_peak_mb',
            'teardown_cpu_avg_pct', 'teardown_cpu_max_pct',
        ]

        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in self.results:
                ps = r.get('phase_stats', {})
                sm = r.get('step_metrics', {})
                sm_dep = sm.get('deploy_challenges', {})
                sm_ver_set = sm.get('verify_setup', {})
                sm_ver_cln = sm.get('verify_cleanup', {})
                sm_tear_m = sm.get('teardown_machines', {})

                writer.writerow([
                    r.get('test_name'),
                    r.get('image_alias', self.image_alias),
                    r.get('test_type'),
                    r.get('teams_count'),
                    r.get('containers_per_team'),
                    r.get('cycles', 1),
                    r.get('total_containers_live'),
                    r.get('peak_containers_verify'),
                    r.get('passed_count'),
                    r.get('failed_count'),
                    r.get('pass_rate_pct'),
                    # Wall-clock
                    r.get('provision_wall_sec'),
                    r.get('deploy_wall_sec'),
                    r.get('verify_setup_wall_sec'),
                    r.get('verify_cleanup_wall_sec'),
                    r.get('teardown_machines_wall_sec'),
                    r.get('teardown_teams_wall_sec'),
                    r.get('teardown_wall_sec'),
                    # Deploy micro
                    ps.get('deploy_validation_sec', {}).get('avg', 0),
                    ps.get('deploy_create_sec', {}).get('avg', 0),
                    ps.get('deploy_create_sec', {}).get('p50', 0),
                    ps.get('deploy_create_sec', {}).get('p95', 0),
                    ps.get('deploy_push_sec', {}).get('avg', 0),
                    ps.get('deploy_start_sec', {}).get('avg', 0),
                    ps.get('deploy_runtime_net_sec', {}).get('avg', 0),
                    ps.get('deploy_db_record_sec', {}).get('avg', 0),
                    # Verify micro
                    ps.get('ovn_create_sec', {}).get('avg', 0),
                    ps.get('cow_clone_sec', {}).get('avg', 0),
                    ps.get('cow_clone_sec', {}).get('p50', 0),
                    ps.get('cow_clone_sec', {}).get('p95', 0),
                    ps.get('clone_connect_sec', {}).get('avg', 0),
                    ps.get('clone_start_sec', {}).get('avg', 0),
                    ps.get('clone_runtime_net_sec', {}).get('avg', 0),
                    ps.get('cleanup_clones_sec', {}).get('avg', 0),
                    ps.get('cleanup_ovn_net_sec', {}).get('avg', 0),
                    # RAM Deltas
                    r.get('base_net_ram_mb', 0),
                    r.get('steady_live_net_delta_mb', 0),
                    r.get('steady_peak_net_delta_mb', 0),
                    # Subprocess step telemetry
                    sm_dep.get('cpu_avg_pct', 0), sm_dep.get('cpu_max_pct', 0), sm_dep.get('iowait_avg_pct', 0), sm_dep.get('ram_peak_mb', 0),
                    sm_ver_set.get('cpu_avg_pct', 0), sm_ver_set.get('cpu_max_pct', 0), sm_ver_set.get('iowait_avg_pct', 0), sm_ver_set.get('ram_peak_mb', 0),
                    sm_ver_cln.get('cpu_avg_pct', 0), sm_ver_cln.get('ram_peak_mb', 0),
                    sm_tear_m.get('cpu_avg_pct', 0), sm_tear_m.get('cpu_max_pct', 0),
                ])
        logger.info('[+] Exported Summary CSV (%d rows) to %s', len(self.results), filename)

    def export_step_resources_csv(self):
        filename = self.csv_step_resources_path
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        if not self.step_resources:
            return
        headers = list(self.step_resources[0].keys())
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(self.step_resources)
        logger.info('[+] Exported Step Resources CSV (%d rows) to %s', len(self.step_resources), filename)

    def export_timeseries_csv(self):
        filename = self.csv_timeseries_path
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        if not self.timeseries_samples:
            return
        headers = list(self.timeseries_samples[0].keys())
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(self.timeseries_samples)
        logger.info('[+] Exported Time-Series CSV (%d samples) to %s', len(self.timeseries_samples), filename)

    def export_raw_samples_csv(self):
        filename = self.csv_raw_samples_path
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        if not self.raw_samples:
            return
        headers = list(self.raw_samples[0].keys())
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(self.raw_samples)
        logger.info('[+] Exported Raw Samples CSV (%d rows) to %s', len(self.raw_samples), filename)

    def export_markdown_report(self):
        filename = self.report_output_path
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        with open(filename, 'w') as f:
            f.write(f'# BlueAgent Stress & Scalability Benchmark Report ({self.image_alias})\n\n')
            f.write(f'*Generated on:* `{now_str}` on AMD Ryzen 7 7700X (8 Cores), 15 GiB RAM\n')
            f.write(f'*Image Alias:* `{self.image_alias}` | *Memory Limit:* `{self.memory_limit}`\n\n')

            f.write('## 1. Synchronized Barrier Wall-Clock Timings\n\n')
            f.write('| Scenario | M | N | Peak Cts | Pass | Prov Wall (s) | Deploy Wall (s) | Verify Setup Wall (s) | Verify Clean Wall (s) | Teardown Wall (s) |\n')
            f.write('| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n')
            for r in self.results:
                f.write(
                    f"| {r.get('test_name')} | {r.get('teams_count')} | {r.get('containers_per_team')} "
                    f"| {r.get('peak_containers_verify')} | {r.get('passed_ratio')} "
                    f"| {r.get('provision_wall_sec')} | {r.get('deploy_wall_sec')} "
                    f"| {r.get('verify_setup_wall_sec')} | {r.get('verify_cleanup_wall_sec')} "
                    f"| {r.get('teardown_wall_sec')} |\n"
                )

            f.write('\n## 2. Micro-Phase Breakdown & ZFS CoW Speedup\n\n')
            f.write('| Scenario | Dep Create Avg (s) | CoW Clone Avg (s) | **CoW Speedup** | OVN Create Avg (s) | Dep Start Avg (s) | Clone Start Avg (s) |\n')
            f.write('| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n')
            for r in self.results:
                ps = r.get('phase_stats', {})
                c_create = ps.get('deploy_create_sec', {}).get('avg', 0)
                c_clone = ps.get('cow_clone_sec', {}).get('avg', 0)
                speedup = round(c_create / c_clone, 2) if c_clone > 0 else 0.0
                f.write(
                    f"| {r.get('test_name')} | {c_create} | {c_clone} | **{speedup}x** "
                    f"| {ps.get('ovn_create_sec', {}).get('avg', 0)} "
                    f"| {ps.get('deploy_start_sec', {}).get('avg', 0)} "
                    f"| {ps.get('clone_start_sec', {}).get('avg', 0)} |\n"
                )

            f.write('\n## 3. Step-by-Step Subprocess CPU & RAM Consumption\n\n')
            f.write('| Scenario | Deploy CPU Avg (%) | Deploy Peak RAM (MB) | Verify Setup CPU Avg (%) | Verify Peak RAM (MB) | Steady Net RAM Delta (+MB) |\n')
            f.write('| :--- | :--- | :--- | :--- | :--- | :--- |\n')
            for r in self.results:
                sm = r.get('step_metrics', {})
                sm_dep = sm.get('deploy_challenges', {})
                sm_ver = sm.get('verify_setup', {})
                f.write(
                    f"| {r.get('test_name')} "
                    f"| {sm_dep.get('cpu_avg_pct', 0)}% "
                    f"| {sm_dep.get('ram_peak_mb', 0)} MB "
                    f"| {sm_ver.get('cpu_avg_pct', 0)}% "
                    f"| {sm_ver.get('ram_peak_mb', 0)} MB "
                    f"| +{r.get('steady_peak_net_delta_mb', 0)} MB |\n"
                )

        logger.info('[+] Exported Markdown Report to %s', filename)


# The 8 matrix scenarios agreed with user
THESIS_MATRIX_SCENARIOS = [
    (2, 5),   # Bridge point 1 (10 live / 20 peak)
    (2, 7),   # Tier 3 (14 live / 28 peak)
    (2, 10),  # Tier 3 (20 live / 40 peak)
    (5, 5),   # Bridge point 2 (25 live / 50 peak)
    (5, 7),   # Tier 3 (35 live / 70 peak)
    (5, 10),  # Tier 3 (50 live / 100 peak)
    (10, 2),  # Tier 3 (20 live / 40 peak)
    (10, 5),  # Tier 3 (50 live / 100 peak)
]


async def main():
    parser = argparse.ArgumentParser(description='BlueAgent Redesigned Thesis Benchmark Suite')
    parser.add_argument('--image', type=str, default='alpine', help='Container image alias (e.g. alpine, ubuntu-heavy)')
    parser.add_argument('--memory-limit', type=str, default='128MB', help='Memory limit per container (e.g. 128MB, 256MB)')
    parser.add_argument('--output-prefix', type=str, default='thesis_alpine', help='Prefix for output files (CSV, JSON, MD)')
    parser.add_argument('--test', choices=['1', 'matrix', 'all'], default='all',
                        help='Which test to run: 1 (Baseline), matrix (8 Matrix scenarios), or all (Test 1 + Matrix)')
    parser.add_argument('--cycles', type=int, default=10, help='Cycles for Test 1')
    parser.add_argument('--single-m', type=int, default=None, help='Run only single scenario with M teams')
    parser.add_argument('--single-n', type=int, default=None, help='Run only single scenario with N containers')
    parser.add_argument('--reset', action='store_true', help='Clear previous saved benchmark state before running')
    args = parser.parse_args()

    runner = BenchmarkRunner(
        image_alias=args.image,
        memory_limit=args.memory_limit,
        output_prefix=args.output_prefix,
    )
    if not args.reset:
        runner.load_existing_state()

    ready = await runner.client.wait_server_ready(max_seconds=10.0)
    if not ready:
        logger.error('BlueAgent server is not responding on http://127.0.0.1:8000')
        sys.exit(1)

    t_suite_start = time.perf_counter()

    if args.test in ('1', 'all') and args.single_m is None:
        await runner.run_test_1(cycles=args.cycles)

    if args.test in ('matrix', 'all') or args.single_m is not None:
        if args.single_m is not None and args.single_n is not None:
            scenarios = [(args.single_m, args.single_n)]
        else:
            scenarios = THESIS_MATRIX_SCENARIOS
        await runner.run_scenarios(scenarios)

    total_elapsed = time.perf_counter() - t_suite_start
    logger.info('[*] Suite Finished in %.2f seconds (%.2f minutes)', total_elapsed, total_elapsed / 60)



if __name__ == '__main__':
    asyncio.run(main())
