"""Variable-size RayCloudSim execution without changing frozen strategy files.

The parameter context is process-local: independent runs may use processes,
not threads. All patched module attributes are restored even on failure.
"""
import contextlib
import importlib
import json
from dataclasses import replace
from pathlib import Path

import energy_priority_policy as policy
import proposal_benchmark as benchmark
from proposal_energy_model import PowerParameters

ROOT=Path(__file__).resolve().parents[1]

def make_topology(kind,node_count):
    if isinstance(node_count,bool) or not isinstance(node_count,int) or node_count<4:
        raise ValueError('node_count must be an integer >= 4')
    cloud_count=max(1,(node_count-1)//3)
    cloud_start=node_count-cloud_count
    frequencies=[.5]+[1.]*(cloud_start-1)+[2.]*cloud_count
    if kind=='star':edges={(0,i) for i in range(1,node_count)}
    elif kind=='tree':edges={((i-1)//2,i) for i in range(1,node_count)}
    elif kind=='mesh':edges={(i,j) for i in range(node_count) for j in range(i+1,node_count)}
    elif kind=='hybrid':edges={(0,1),(0,2),(1,2)}|{(i-2,i) for i in range(3,node_count)}|{(node_count-2,node_count-1)}
    else:raise ValueError('unknown topology')
    nodes=[dict(NodeType='Node',NodeName=f'n{i}',NodeId=i,MaxCpuFreq=f*1e9,
                MaxBufferSize=1e12,IdleEnergyCoef=.01,ExeEnergyCoef=1) for i,f in enumerate(frequencies)]
    links=[dict(EdgeType='Link',SrcNodeID=i,DstNodeID=j,Bandwidth=10e9,
                BaseLatency=.2 if max(i,j)<cloud_start else 1.) for i,j in sorted(edges)]
    return dict(Nodes=nodes,Edges=links)

def configuration(config):
    nodes=sorted(config['Nodes'],key=lambda n:n['NodeId'])
    if [n['NodeId'] for n in nodes]!=list(range(len(nodes))) or [n['NodeName'] for n in nodes]!=[f'n{i}' for i in range(len(nodes))]:
        raise ValueError('nodes require contiguous IDs and n0..nN names')
    frequencies=tuple(n['MaxCpuFreq']/1e9 for n in nodes)
    profiles={f'n{i}':PowerParameters(4 if i==0 else 8 if f<2 else 15,2,1.75,.5,-.5,.5,.4,10,1)
              for i,f in enumerate(frequencies)}
    return frequencies,profiles

@contextlib.contextmanager
def parameter_context(frequencies,profiles,horizon):
    saved=[]
    try:
        for name in ('proposal_benchmark','scheduling_support','energy_priority_policy'):
            module=importlib.import_module(name)
            for attr,value in [('FREQUENCIES',frequencies)]:
                if hasattr(module,attr):saved.append((module,attr,getattr(module,attr)));setattr(module,attr,value)
        saved.extend([(policy,'profiles',policy.profiles),(policy,'HORIZON',policy.HORIZON)])
        policy.profiles=lambda:profiles
        policy.HORIZON=horizon
        yield
    finally:
        for module,attr,value in reversed(saved):setattr(module,attr,value)

def run(config_path,rows,method='energy_priority',horizon=1500.,power_overrides=None,env_factory=None,task_factory=None,selector=None):
    config=json.loads(Path(config_path).read_text(encoding='utf-8'))
    frequencies,profiles=configuration(config)
    if horizon<=0:raise ValueError('positive horizon required')
    if len({r['id'] for r in rows})!=len(rows):raise ValueError('duplicate task IDs')
    if any(r['arrival_s']<0 or r['arrival_s']>=horizon for r in rows):raise ValueError('arrival outside observation window')
    if any(b['arrival_s']<a['arrival_s'] for a,b in zip(rows,rows[1:])):raise ValueError('arrivals must be sorted')
    for name,changes in (power_overrides or {}).items():
        if name not in profiles:raise ValueError('unknown power profile node')
        profiles[name]=replace(profiles[name],**changes)
    if env_factory is None and task_factory is None and selector is None:
        with parameter_context(frequencies,profiles,horizon):return policy.run_case(config_path,rows,method)
    with parameter_context(frequencies,profiles,horizon):
        env=(env_factory or policy.ManagedEnv)(policy.Scenario(str(config_path)),profiles)
        env.logger.enable_logging=False;env.enable_idle_sleep();decisions={}
        def producer():
            for row in rows:
                yield env.controller.timeout(row['arrival_s']-env.now)
                destination,decision=(selector(env,row) if selector else policy.dispatch(env,row,method))
                decisions[row['id']]=decision
                cls=task_factory or policy.MeteredTask
                extra={k:row[k] for k in ('result_bits','result_rate_bps') if k in row} if task_factory else {}
                task=cls(id=row['id'],task_size=row['data_bits'],cycles_per_bit=row['cycles_per_bit'],
                    trans_bit_rate=row['rate_bps'],src_name=row['source'],ddl=row['deadline_s'],memory_gb=row['memory_gb'],**extra)
                task.arrival_s=env.now;env.submit(task,destination)
        try:
            env.controller.process(producer());env.run(until=horizon)
            if selector is not None and hasattr(selector,'finish_episode'):selector.finish_episode(env)
            report=env.energy_report()
            for row in report['tasks']:
                task=env.observed_tasks[row['id']]
                row.update(arrival_s=task.arrival_s,destination=task.assigned_destination,
                           deadline_s=task.ddl,decision=decisions[row['id']])
            return report
        finally:env.close()
