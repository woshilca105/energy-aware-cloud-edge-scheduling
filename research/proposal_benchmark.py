"""Application benchmark v1: four topologies, three workloads and three loads.

Development seeds only. Fixed synthetic profiles; all three baseline policies
use identical inputs, an always-awake power policy and the same observation time.
"""
import csv
import hashlib
import json
import math
import random
import statistics
from pathlib import Path
from proposal_simulation import ROOT, MeteredEnv, MeteredTask
from proposal_energy_model import PowerParameters
from examples.scenarios.scenario_1 import Scenario

OUT = ROOT/'research_records/proposal_benchmark_v1'
TOPOLOGIES = ('star', 'tree', 'mesh', 'hybrid')
WORKLOADS = ('compute', 'io', 'latency')
LOADS = {'low': 6.0, 'medium': 2.0, 'high': .5}
SEEDS = (1101, 1102, 1103)
POLICIES = ('nearest', 'least_loaded', 'fastest')
COUNT = 100
HORIZON = 1500.0
FREQUENCIES = (.5, 1, 1, 1, 1, 2, 2)


def topology(name):
    if name == 'star':
        pairs = [(0, i) for i in range(1, 7)]
    elif name == 'tree':
        pairs = [(0, 1), (0, 2), (1, 3), (1, 4), (2, 5), (2, 6)]
    elif name == 'mesh':
        pairs = [(i, j) for i in range(7) for j in range(i+1, 7)]
    elif name == 'hybrid':
        pairs = [(0, 1), (0, 2), (1, 2), (1, 3), (2, 4), (3, 5), (4, 6), (5, 6)]
    else:
        raise ValueError(name)
    nodes = [dict(NodeType='Node', NodeName=f'n{i}', NodeId=i,
                  MaxCpuFreq=f*1e9, MaxBufferSize=1e12,
                  IdleEnergyCoef=.01, ExeEnergyCoef=1) for i, f in enumerate(FREQUENCIES)]
    edges = [dict(EdgeType='Link', SrcNodeID=i, DstNodeID=j,
                  Bandwidth=10e9, BaseLatency=.2 if max(i, j)<5 else 1.0)
             for i, j in pairs]
    return dict(Nodes=nodes, Edges=edges)


def profiles():
    # Illustrative CPU/memory domain, not a calibrated device inventory.
    return {f'n{i}': PowerParameters(4 if i==0 else 8 if i<5 else 15,
                                    2, 1.75, .5, -.5, .5, .4, 10, 1)
            for i in range(7)}


def workload(kind, load, seed):
    rng = random.Random(seed)
    rows = []
    arrival = 0.0
    for i in range(COUNT):
        if i:
            arrival += LOADS[load]*rng.uniform(.8, 1.2)
        if kind == 'compute':
            mb, cycles, memory, deadline = rng.uniform(.05, .15), rng.uniform(2000, 4000), 1.0, 20
        elif kind == 'io':
            mb, cycles, memory, deadline = rng.uniform(2, 5), rng.uniform(5, 15), 2.0, 20
        else:
            mb, cycles, memory, deadline = rng.uniform(.05, .2), rng.uniform(100, 300), .25, 3
        rows.append(dict(id=i, arrival_s=arrival, data_bits=mb*8e6,
                         cycles_per_bit=cycles, rate_bps=8e6,
                         memory_gb=memory, deadline_s=deadline, source='n0'))
    return rows


def choose(env, row, policy):
    choices = []
    for i, frequency in enumerate(FREQUENCIES):
        name = f'n{i}'
        path = env.scenario.infrastructure.get_shortest_links(row['source'], name)
        transfer = sum(x.base_latency+row['data_bits']/row['rate_bps'] for x in path)
        # Only work already dispatched is visible; no later jobs are consulted.
        pending = 0.0
        for task in env.observed_tasks.values():
            if task.assigned_destination != name or task.released_at is not None:
                continue
            if task.started_at is not None:
                pending += max(0, task.started_at+task.exe_time-env.now)
                # Upstream releases CPU on its 1-second collector tick.
                pending += max(0, math.floor(max(env.now, task.started_at+task.exe_time))+1
                               -max(env.now, task.started_at+task.exe_time))
            else:
                pending += task.task_size*task.cycles_per_bit/(frequency*1e9)+1
        execution = row['data_bits']*row['cycles_per_bit']/(frequency*1e9)
        score = len(path) if policy=='nearest' else pending if policy=='least_loaded' else transfer+pending+execution
        choices.append((score, i, name))
    return min(choices)[2]


def run_case(config_path, rows, policy):
    env = MeteredEnv(Scenario(str(config_path)), profiles())
    env.logger.enable_logging = False

    def producer():
        for row in rows:
            yield env.controller.timeout(row['arrival_s']-env.now)
            destination = choose(env, row, policy)
            task = MeteredTask(id=row['id'], task_size=row['data_bits'],
                               cycles_per_bit=row['cycles_per_bit'], trans_bit_rate=row['rate_bps'],
                               src_name=row['source'], ddl=row['deadline_s'], memory_gb=row['memory_gb'])
            task.arrival_s = env.now
            env.submit(task, destination)

    env.controller.process(producer())
    env.run(until=HORIZON)
    report = env.energy_report()
    assert len(report['tasks']) == len(rows)
    for row in report['tasks']:
        task = env.observed_tasks[row['id']]
        row.update(destination=task.assigned_destination, arrival_s=task.arrival_s,
                   deadline_s=task.ddl, memory_gb=task.memory_gb)
        assert row['finished_s'] is not None, ('unfinished', policy, row)
        assert row['released_s'] is not None, ('not released', policy, row)
        assert math.isclose(row['response_s'], row['finished_s']-row['arrival_s'], abs_tol=1e-7)
    for node in report['nodes'].values():
        assert math.isclose(sum(node['by_state_seconds'].values()), HORIZON, abs_tol=1e-7)
    responses = sorted(x['response_s'] for x in report['tasks'])
    report['summary'] = dict(policy=policy, tasks=len(rows), completed=len(responses),
                             on_time=sum(x['on_time'] for x in report['tasks']),
                             mean_response_s=statistics.mean(responses),
                             p95_response_s=responses[math.ceil(.95*len(responses))-1],
                             node_j=sum(n['total_j'] for n in report['nodes'].values()),
                             network_j=report['network_j'], total_j=report['total_j'],
                             throughput_per_s=len(responses)/HORIZON)
    env.close()
    return report


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


