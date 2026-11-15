"""Shared runtime and online estimates used by the current strategy.

Extracted unchanged from the development versions: this module contains no
abandoned policy entry points and never sees future task arrivals.
"""
import math
from proposal_simulation import MeteredEnv, MeteredTask
from proposal_benchmark import ROOT, HORIZON, FREQUENCIES, profiles
from examples.scenarios.scenario_1 import Scenario

IDLE_SECONDS = 2.0
POLL_SECONDS = .25
TAIL_SECONDS = IDLE_SECONDS + POLL_SECONDS / 2

class ManagedEnv(MeteredEnv):
    def enable_idle_sleep(self):
        self.idle_since = {}
        self.controller.process(self._idle_monitor())

    def _idle_monitor(self):
        while True:
            for name, ledger in self.ledgers.items():
                node = self.scenario.get_node(name)
                pending = any(t.assigned_destination == name and t.released_at is None
                              for t in self.observed_tasks.values())
                available = (ledger.state == 'idle' and not pending and
                             not node.active_tasks and name not in self.wake_events and
                             node.task_buffer.free_size == node.task_buffer.max_size)
                if not available:
                    self.idle_since.pop(name, None)
                elif self.now - self.idle_since.setdefault(name, self.now) >= IDLE_SECONDS:
                    self.sleep_idle_node(name)
                    self.idle_since.pop(name, None)
            yield self.controller.timeout(POLL_SECONDS)

def transfer_seconds(env, source, destination, bits, rate):
    links = env.scenario.infrastructure.get_shortest_links(source, destination)
    return sum(link.base_latency+bits/rate for link in links), len(links)


def projected_cost(env, name, candidate=None):
    """Predict node energy relative to sleep from now until all committed work drains.

    Full run occupancy includes the collector tick, matching the current ledger.
    Pending transmissions prevent automatic sleep even while the CPU is idle.
    A fixed expected half polling interval estimates the final idle tail.
    """
    now = env.now
    ledger = env.ledgers[name]
    p = ledger.parameters
    frequency = FREQUENCIES[int(name[1:])]
    running = None
    jobs = []
    for order, task in enumerate(env.observed_tasks.values()):
        if task.assigned_destination != name or task.released_at is not None:
            continue
        if task.started_at is not None:
            running = task
            continue
        tx, _ = transfer_seconds(env, task.src_name, name, task.task_size, task.trans_bit_rate)
        ready = max(now, task.arrival_s+tx)
        jobs.append((ready, order, task.task_size*task.cycles_per_bit/(frequency*1e9), task.memory_gb, False))
    if candidate is not None:
        tx, _ = transfer_seconds(env, candidate['source'], name, candidate['data_bits'], candidate['rate_bps'])
        jobs.append((now+tx, len(env.observed_tasks), candidate['data_bits']*candidate['cycles_per_bit']/(frequency*1e9), candidate['memory_gb'], True))
    jobs.sort()
    if running is None and not jobs:
        if ledger.state == 'sleep':
            return 0.0, None
        if ledger.state == 'idle':
            since = getattr(env, 'idle_since', {}).get(name, now)
            remaining = max(0, IDLE_SECONDS-(now-since))+POLL_SECONDS/2
            return (p.power_w('idle')-p.sleep_w)*remaining, None
    cost = 0.0
    cursor = now
    awake_start = now
    if ledger.state == 'sleep':
        # Wake starts on first actual CPU arrival, not at dispatch.
        awake_start = jobs[0][0]
        cursor = awake_start+p.wake_seconds
        cost += (p.wake_w-p.sleep_w)*p.wake_seconds+p.wake_extra_j
        awake_start = cursor
    elif ledger.state == 'waking':
        cursor = max(now, ledger.ready_at)
        cost += (p.wake_w-p.sleep_w)*(cursor-now)
        awake_start = cursor
    if running is not None:
        end = max(now, math.floor(running.started_at+running.exe_time)+1)
        cost += (p.power_w('running',1,frequency,running.memory_gb)-p.sleep_w)*(end-now)
        cursor = end
    response = None
    for ready, order, duration, memory, is_candidate in jobs:
        start = max(cursor, ready)
        cost += (p.power_w('idle')-p.sleep_w)*max(0,start-cursor)
        finished = start+duration
        released = math.floor(finished)+1
        cost += (p.power_w('running',1,frequency,memory)-p.sleep_w)*(released-start)
        cursor = released
        if is_candidate:
            response = finished-now
    cost += (p.power_w('idle')-p.sleep_w)*TAIL_SECONDS
    return cost, response


def candidates(env, row):
    options = []
    for name in env.ledgers:
        baseline, _ = projected_cost(env, name)
        augmented, response = projected_cost(env, name, row)
        _, hops = transfer_seconds(env, row['source'], name, row['data_bits'], row['rate_bps'])
        network = hops*row['data_bits']/row['rate_bps']*env.nic_w
        options.append(dict(name=name, response_s=response, marginal_j=augmented-baseline+network))
    return options

def committed_schedule(env,name,row=None):
    """Predict finish/release times; candidate may not delay earlier committed work.

    Deliberately mirrors the collector convention of frozen v2, not its dispatch
    rule. Tie ordering is approximate and explicitly tested as a limitation.
    """
    f=FREQUENCIES[int(name[1:])]
    now=env.now
    ledger=env.ledgers[name]
    jobs=[]
    cursor=now
    for order,t in enumerate(env.observed_tasks.values()):
        if t.assigned_destination!=name or t.released_at is not None:
            continue
        if t.started_at is not None:
            cursor=max(cursor,math.floor(t.started_at+t.exe_time)+1)
        else:
            tx,_=transfer_seconds(env,t.src_name,name,t.task_size,t.trans_bit_rate)
            jobs.append((max(now,t.arrival_s+tx),order,t.id,t.task_size*t.cycles_per_bit/(f*1e9)))
    if row is not None:
        tx,_=transfer_seconds(env,row['source'],name,row['data_bits'],row['rate_bps'])
        jobs.append((now+tx,len(env.observed_tasks),row['id'],row['data_bits']*row['cycles_per_bit']/(f*1e9)))
    jobs.sort()
    if ledger.state=='sleep' and jobs:
        cursor=max(cursor,jobs[0][0]+ledger.parameters.wake_seconds)
    elif ledger.state=='waking':
        cursor=max(cursor,ledger.ready_at)
    result={}
    for ready,order,task_id,duration in jobs:
        start=max(cursor,ready)
        finish=start+duration
        cursor=math.floor(finish)+1
        result[task_id]=dict(finish_s=finish,release_s=cursor)
    return result

