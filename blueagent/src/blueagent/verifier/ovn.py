"""OVN dark sandbox verification orchestrator template.

Acts as an Asynchronous Context Manager (RAII) coordinating:
1. Creation of dark OVN logical switch.
2. Fast CoW cloning of production target containers.
3. Rebinding container eth0 NIC from live bridge to OVN switch.
4. Optional provisioning of ephemeral verifier runner.
5. In-situ and black-box probe execution.
6. Guaranteed reverse-order teardown.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional
from ..config import settings
from ..engine.base import ContainerEngine
from ..incus import devices
from ..models import ChallengeConfig, VerifyResultResp, ContainerSpec
from ..network.commands import ip
from ..network.templates import render_interfaces_conf, render_networkd_conf



class OVNVerifier:
    """Async context manager managing OVN sandbox lifecycle and verifier runners.

    NOTE: This is an architectural template to be populated in Phase 2.
    """

    def __init__(self, engine: ContainerEngine, config: ChallengeConfig, session_id: Optional[str] = None, team_k: Optional[int] = None):
        self.engine = engine
        self.config = config
        if team_k is not None:
            self.team_k = team_k
        elif hasattr(config, "team_k") and getattr(config, "team_k") is not None:
            self.team_k = getattr(config, "team_k")
        else:
            raise ValueError("team_k must be provided to OVNVerifier")
        self.net_name = f"verify-{self.team_k}"
        self.clones: List[str] = []
        self.verifier_container_name: Optional[str] = None
        self.step_timings: Dict[str, Any] = {}
        self._t_start = time.perf_counter()

    async def __aenter__(self) -> "OVNVerifier":
        """Provisions the dark OVN network and CoW clones the target instances."""
        try:
            t_enter = time.perf_counter()
            allow_inet = bool(getattr(self.config, "verify_internet_access", True))
            net_config = {
                "ipv4.address": f"10.{self.team_k}.1.1/24",
                "ipv4.nat": "true" if allow_inet else "false",
            }
            if allow_inet:
                net_config["network"] = settings.incus_uplink_network

            t0 = time.perf_counter()
            await self.engine.create_network(
                name=self.net_name,
                config=net_config,
            )
            ovn_net_sec = time.perf_counter() - t0

            clone_timings: List[dict] = []

            async def _prepare_clone(c):
                c_t0 = time.perf_counter()
                source_name = f"c-{self.team_k}-{c.name}"
                clone_name = f"verify-{self.team_k}-{c.name}"
                ip_addr = f"10.{self.team_k}.1.{c.ip_suffix}"
                gw_ip = f"10.{self.team_k}.1.1"

                t_cl = time.perf_counter()
                await self.engine.clone_instance(source_name, clone_name)
                self.clones.append(clone_name)
                clone_sec = time.perf_counter() - t_cl

                t_conn = time.perf_counter()
                await self.engine.connect_network(clone_name, self.net_name)
                conn_sec = time.perf_counter() - t_conn

                t_st = time.perf_counter()
                await self.engine.start_instance(clone_name)
                start_sec = time.perf_counter() - t_st

                # Ensure runtime IP and route are active
                t_net = time.perf_counter()
                try:
                    await self.engine.exec_command(clone_name, ip.addr_replace(f"{ip_addr}/24", "eth0"), capture_output=False)
                    await self.engine.exec_command(clone_name, ip.link_up("eth0"), capture_output=False)
                    await self.engine.exec_command(clone_name, ip.route_replace_default(gw_ip, "eth0"), capture_output=False)
                except Exception:
                    pass
                runtime_net_sec = time.perf_counter() - t_net

                return {
                    "clone": clone_name,
                    "clone_sec": round(clone_sec, 4),
                    "connect_sec": round(conn_sec, 4),
                    "start_sec": round(start_sec, 4),
                    "runtime_net_sec": round(runtime_net_sec, 4),
                    "total_clone_sec": round(time.perf_counter() - c_t0, 4),
                }

            if self.config.containers:
                clone_timings = list(await asyncio.gather(*[_prepare_clone(c) for c in self.config.containers]))

            runner_sec = 0.0
            if self.config.verifier:
                t_v = time.perf_counter()
                v_name = f"verify-runner-{self.team_k}"
                self.verifier_container_name = v_name
                v_ip = f"10.{self.team_k}.1.{self.config.verifier.ip_suffix}"
                gw_ip = f"10.{self.team_k}.1.1"

                v_spec = ContainerSpec(
                    name=v_name,
                    image=self.config.verifier.image,
                    ip_suffix=self.config.verifier.ip_suffix,
                    limits_cpu=self.config.verifier.limits_cpu,
                    limits_memory=self.config.verifier.limits_memory,
                )
                dev_map = devices.nic_ovn(self.net_name, dev_name="eth0")
                await self.engine.create_instance(v_spec, devices=dev_map)

                # Inject static network configuration
                networkd_conf = render_networkd_conf(ip=v_ip, gateway=gw_ip)
                try:
                    await self.engine.push_file(v_name, "/etc/systemd/network/10-eth0.network", networkd_conf, mode="0644")
                except Exception:
                    pass

                interfaces_conf = render_interfaces_conf(ip=v_ip, gateway=gw_ip)
                try:
                    await self.engine.push_file(v_name, "/etc/network/interfaces", interfaces_conf, mode="0644")
                except Exception:
                    pass

                await self.engine.start_instance(v_name)

                # Ensure runtime IP and route are active
                try:
                    await self.engine.exec_command(v_name, ip.addr_replace(f"{v_ip}/24", "eth0"), capture_output=False)
                    await self.engine.exec_command(v_name, ip.link_up("eth0"), capture_output=False)
                    await self.engine.exec_command(v_name, ip.route_replace_default(gw_ip, "eth0"), capture_output=False)
                except Exception:
                    pass
                runner_sec = time.perf_counter() - t_v

            self.step_timings["sandbox_setup"] = {
                "ovn_network_create_sec": round(ovn_net_sec, 4),
                "total_clone_time_sec": round(sum(ct["clone_sec"] for ct in clone_timings), 4),
                "total_connect_time_sec": round(sum(ct["connect_sec"] for ct in clone_timings), 4),
                "total_start_time_sec": round(sum(ct["start_sec"] for ct in clone_timings), 4),
                "total_runtime_net_sec": round(sum(ct["runtime_net_sec"] for ct in clone_timings), 4),
                "verifier_runner_sec": round(runner_sec, 4),
                "total_setup_sec": round(time.perf_counter() - t_enter, 4),
                "clone_details": clone_timings,
            }

            return self
        except Exception:
            await self._cleanup()
            raise

    async def _cleanup(self) -> None:
        """Teardown verifier runner, clones, and dark OVN network."""
        t_clean = time.perf_counter()
        t0 = time.perf_counter()
        if self.verifier_container_name:
            try:
                await self.engine.stop_instance(self.verifier_container_name, force=True)
                await self.engine.delete_instance(self.verifier_container_name)
            except Exception:
                pass
            self.verifier_container_name = None

        async def _destroy_inst(inst_name):
            try:
                await self.engine.stop_instance(inst_name, force=True)
                await self.engine.delete_instance(inst_name)
            except Exception:
                pass

        if self.clones:
            await asyncio.gather(*[_destroy_inst(clone) for clone in self.clones])
            self.clones.clear()
        clones_clean_sec = time.perf_counter() - t0

        t1 = time.perf_counter()
        try:
            await self.engine.delete_network(self.net_name)
        except Exception:
            pass
        ovn_net_clean_sec = time.perf_counter() - t1

        self.step_timings["cleanup"] = {
            "clones_teardown_sec": round(clones_clean_sec, 4),
            "ovn_network_delete_sec": round(ovn_net_clean_sec, 4),
            "total_cleanup_sec": round(time.perf_counter() - t_clean, 4),
        }
        self.step_timings["total_verify_lifecycle_sec"] = round(time.perf_counter() - self._t_start, 4)

    async def run_checks(self) -> VerifyResultResp:
        """Executes readiness checks followed by verification commands against clones or via runner."""
        t_readiness_start = time.perf_counter()
        # 1. Run readiness checks with retry loop
        for cmd in self.config.readiness_commands:
            target_name = None
            if cmd.target == "verifier":
                if not self.verifier_container_name:
                    return VerifyResultResp(success=False, passed=False, message="Verifier target required for readiness check but no runner configured")
                target_name = self.verifier_container_name
            elif cmd.target == "container":
                if cmd.container_name:
                    target_name = f"verify-{self.team_k}-{cmd.container_name}"
                elif self.clones:
                    target_name = self.clones[0]
                else:
                    return VerifyResultResp(success=False, passed=False, message="No container clone available for readiness check")
            else:
                return VerifyResultResp(success=False, passed=False, message=f"Unknown target {cmd.target}")

            start_t = asyncio.get_event_loop().time()
            passed = False
            last_err = ""
            while (asyncio.get_event_loop().time() - start_t) < cmd.retry_timeout:
                try:
                    res = await self.engine.exec_command(
                        name=target_name,
                        cmd=cmd.cmd,
                        env=cmd.env,
                        timeout=cmd.timeout,
                    )
                    if res.exit_code == 0:
                        passed = True
                        break
                    last_err = f"exit {res.exit_code}: {res.stderr or res.stdout}"
                except Exception as ex:
                    last_err = str(ex)
                await asyncio.sleep(cmd.interval)

            if not passed:
                self.step_timings["readiness_sec"] = round(time.perf_counter() - t_readiness_start, 4)
                return VerifyResultResp(
                    success=True,
                    passed=False,
                    message=f"Readiness check failed on '{target_name}': {last_err}",
                    details={"readiness_failed": True, "error": last_err, "step_timings": self.step_timings},
                )

        self.step_timings["readiness_sec"] = round(time.perf_counter() - t_readiness_start, 4)

        # 2. Only if readiness passed, run verify commands
        t_verify_start = time.perf_counter()
        for cmd in self.config.verify_commands:
            try:
                target_name = None
                if cmd.target == "verifier":
                    if not self.verifier_container_name:
                        return VerifyResultResp(success=False, passed=False, message="Verifier target required but no verifier runner configured")
                    target_name = self.verifier_container_name
                elif cmd.target == "container":
                    if not cmd.container_name:
                        return VerifyResultResp(success=False, passed=False, message="Command missing container_name for target 'container'")
                    target_name = f"verify-{self.team_k}-{cmd.container_name}"
                else:
                    return VerifyResultResp(success=False, passed=False, message=f"Unknown target {cmd.target}")

                retries = getattr(cmd, "retries", 1)
                retry_delay = getattr(cmd, "retry_delay", 1.0)
                res = None
                for attempt in range(retries):
                    try:
                        res = await self.engine.exec_command(
                            name=target_name,
                            cmd=cmd.cmd,
                            env=cmd.env,
                            timeout=cmd.timeout,
                        )
                        if res.exit_code == 0:
                            break
                        if attempt < retries - 1 and retry_delay > 0:
                            await asyncio.sleep(retry_delay)
                    except Exception as e:
                        if attempt < retries - 1 and retry_delay > 0:
                            await asyncio.sleep(retry_delay)
                        else:
                            raise e

                if res and res.exit_code != 0:
                    self.step_timings["verify_commands_sec"] = round(time.perf_counter() - t_verify_start, 4)
                    return VerifyResultResp(
                        success=True,
                        passed=False,
                        message=f"Command failed (exit {res.exit_code}): {' '.join(cmd.cmd)}",
                        details={"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.exit_code, "step_timings": self.step_timings},
                    )
            except TimeoutError:
                self.step_timings["verify_commands_sec"] = round(time.perf_counter() - t_verify_start, 4)
                return VerifyResultResp(success=False, passed=False, message=f"Timeout executing command: {' '.join(cmd.cmd)}", details={"step_timings": self.step_timings})
            except Exception as e:
                self.step_timings["verify_commands_sec"] = round(time.perf_counter() - t_verify_start, 4)
                err_str = str(e).lower()
                if "deadline exceeded" in err_str or "timed out" in err_str:
                    return VerifyResultResp(success=False, passed=False, message=f"Timeout executing command: {' '.join(cmd.cmd)}", details={"step_timings": self.step_timings})
                return VerifyResultResp(success=False, passed=False, message=f"Error executing command: {e}", details={"step_timings": self.step_timings})

        self.step_timings["verify_commands_sec"] = round(time.perf_counter() - t_verify_start, 4)
        return VerifyResultResp(
            success=True,
            passed=True,
            message="All verification commands passed.",
            details={"step_timings": self.step_timings},
        )

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Guarantees ordered teardown of verifier runner, cloned instances, and OVN network."""
        await self._cleanup()
