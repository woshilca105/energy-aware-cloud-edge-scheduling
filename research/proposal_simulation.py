"""RayCloudSim adapter: seconds, bits, Hz, GHz, GB, watts and joules.

CPU/memory sleep domain only: the separately metered NIC stays available.
Fixed-frequency, single-job CPU; queued input memory and result traffic excluded.
No changes to upstream core or the frozen window-strategy baseline.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.env import Env
from core.task import Task
from proposal_energy_model import StateEnergyLedger, finite_nonnegative


class MeteredTask(Task):
    def __init__(self, *args, memory_gb=0, **kwargs):
        super().__init__(*args, **kwargs)
        finite_nonnegative(memory_gb, 'memory_gb')
        self.memory_gb = memory_gb
        self.wake_wait_s = 0.0
        self.started_at = self.finished_at = self.released_at = None

    def allocate(self, now, node=None, pre_allocate=False):
        super().allocate(now, node, pre_allocate)
        if not pre_allocate:
            self.wait_time += self.wake_wait_s
            self.started_at = now
            self.meter.ledgers[self.dst_name].set_running(
                now, 1.0, self.cpu_freq / 1e9, self.memory_gb)

    def deallocate(self):
        self.meter.ledgers[self.dst_name].set_idle(self.meter.now)
        self.released_at = self.meter.now
        super().deallocate()


class MeteredEnv(Env):
    def __init__(self, scenario, parameters, tx_w=2, rx_w=1):
        super().__init__(scenario, config_file=str(ROOT/'core/configs/env_config_null.json'))
        for value in (tx_w, rx_w):
            finite_nonnegative(value, 'NIC incremental watts')
        self.nic_w = tx_w + rx_w
        self.ledgers = {name: StateEnergyLedger(profile) for name, profile in parameters.items()}
        if set(self.ledgers) != set(scenario.node_id2name.values()):
            raise ValueError('one power profile is required per node')
        self.wake_events = {}
        self.transfers = []
        self.observed_tasks = {}

    def submit(self, task, destination):
        if not isinstance(task, MeteredTask):
            raise TypeError('submit requires MeteredTask')
        if task.id in self.observed_tasks:
            raise ValueError('duplicate task ID')
        task.meter = self
        task.assigned_destination = destination
        self.observed_tasks[task.id] = task
        self.process(task=task, dst_name=destination)

    def sleep_idle_node(self, name):
        # Explicit idle sleep only; automatic sleep scheduling is a later policy.
        node = self.scenario.get_node(name)
        pending = any(t.assigned_destination == name and t.released_at is None
                      for t in self.observed_tasks.values())
        if pending or node.active_tasks or node.task_buffer.free_size < node.task_buffer.max_size or name in self.wake_events:
            raise ValueError('cannot sleep a busy or waking node')
        self.ledgers[name].sleep(self.now)

    def _wake(self, name):
        ledger = self.ledgers[name]
        ledger.wake(self.now)
        yield self.controller.timeout(ledger.parameters.wake_seconds)
        ledger.advance(self.now)
        del self.wake_events[name]

    def _handle_task_execution(self, task, dst, flag_reactive):
        if not flag_reactive:
            arrival = self.now
            if self.ledgers[dst.name].state == 'sleep' and dst.name not in self.wake_events:
                self.wake_events[dst.name] = self.controller.process(self._wake(dst.name))
            if dst.name in self.wake_events:
                yield self.wake_events[dst.name]
            task.wake_wait_s = self.now - arrival
        yield from super()._handle_task_execution(task, dst, flag_reactive)
        if task.started_at is not None and task.finished_at is None:
            task.finished_at = self.now

    def _handle_task_transmission(self, task, dst_name):
        # Let upstream validate/allocate the route before recording any charge.
        process = super()._handle_task_transmission(task, dst_name)
        first_event = next(process)
        cursor = self.now
        for link in self.scenario.infrastructure.get_shortest_links(task.src_name, dst_name):
            cursor += link.base_latency
            duration = task.task_size / task.trans_bit_rate
            self.transfers.append(dict(task_id=task.id, start=cursor, end=cursor+duration))
            cursor += duration
        yield first_event
        yield from process

    def energy_report(self):
        horizon = self.now
        nodes = {name: ledger.summary(horizon) for name, ledger in self.ledgers.items()}
        network = sum(max(0, min(horizon, x['end'])-x['start'])*self.nic_w
                      for x in self.transfers)
        rows = []
        for task in self.observed_tasks.values():
            response = None if task.finished_at is None else task.trans_time+task.wait_time+task.exe_time
            rows.append(dict(id=task.id, started_s=task.started_at, finished_s=task.finished_at,
                             released_s=task.released_at, wake_wait_s=task.wake_wait_s,
                             response_s=response, on_time=None if response is None else
                             (task.ddl < 0 or response <= task.ddl)))
        return dict(horizon_s=horizon, nodes=nodes, network_j=network,
                    total_j=sum(x['total_j'] for x in nodes.values())+network, tasks=rows)
