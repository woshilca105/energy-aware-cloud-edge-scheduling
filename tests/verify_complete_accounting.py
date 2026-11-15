import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))

"""Independent timed-flow, memory, partial-window and energy-closure checks."""
import functools,json,math
from pathlib import Path
import scalable_execution as scalable
from complete_energy_accounting import AccountedEnv,AccountedTask,AccountingOptions
import proposal_benchmark as base
import energy_priority_policy as frozen
ROOT=scalable.ROOT;OUT=ROOT/'research_records/function_accounting_checks_v1'
def close(a,b):assert math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-7),(a,b)
def scenario(rows,dest='n1',options=None,horizon=30):
    return scalable.run(OUT/'config.json',rows,horizon=horizon,
        env_factory=functools.partial(AccountedEnv,options=options or AccountingOptions(True,.2)),
        task_factory=AccountedTask,selector=lambda env,row:(dest,None))
def row(i,arrival=0,result=0):return dict(id=i,arrival_s=arrival,data_bits=1e6,cycles_per_bit=1000,rate_bps=1e6,
    source='n0',deadline_s=30,memory_gb=2.,result_bits=result,result_rate_bps=2e6)
def check(report):
    close(sum(n['total_j'] for n in report['nodes'].values())+report['network_j'],report['total_j'])
    close(sum(t['direct_energy_j'] for t in report['tasks'])+report['shared_infrastructure_j'],report['total_j'])
    for n in report['nodes'].values():close(sum(n['by_state_j'].values())+n['queued_memory_j']+n['nic_baseline_j'],n['total_j'])
    for t in report['tasks']:
        if t['finished_s'] is not None:close(t['response_s'],t['finished_s']-t['arrival_s'])
def main():
    source=frozen.source_hashes();OUT.mkdir(parents=True,exist_ok=False)
    (OUT/'config.json').write_text(json.dumps(scalable.make_topology('star',7)),encoding='utf-8')
    checks=[];reports={}
    # Switches off: preserve actual CPU schedule, response and energy exactly.
    rows=[row(0),row(1,.1)]
    old=scalable.run(OUT/'config.json',rows,selector=lambda env,row:('n1',None),horizon=30)
    off=scenario(rows,options=AccountingOptions(False,0.));close(old['total_j'],off['total_j'])
    for a,b in zip(old['tasks'],off['tasks']):close(a['finished_s'],b['finished_s']);close(a['response_s'],b['response_s'])
    checks.append('disabled additions preserve frozen response and energy')
    returned=scenario([row(0,result=2e6)]);r=returned['tasks'][0]
    close(r['finished_s']-r['cpu_finished_s'],1.2);close(r['input_network_j'],3);close(r['return_network_j'],3)
    close(sum(n['nic_baseline_j'] for n in returned['nodes'].values()),7*.2*30)
    checks.append('actual input and return route events plus always-on NIC integral')
    queue=scenario(rows);queued=queue['tasks'][1]
    # n1 input arrives at 1.3, starts at collector release 3.0: 1.7s x 2GB x .5W/GB.
    close(queued['started_s'],3.);close(queued['queued_memory_j'],1.7)
    assert queued['collector_holding_j']>0
    checks.append('queued memory and collector holding independently hand-calculated')
    local=scenario([row(0,result=2e6)],dest='n0');close(local['tasks'][0]['return_network_j'],0.)
    short=scenario([row(0,result=20e6)],horizon=5)
    assert short['tasks'][0]['cpu_finished_s'] is not None and short['tasks'][0]['finished_s'] is None
    close(short['tasks'][0]['return_network_j'],3*(5-short['tasks'][0]['cpu_finished_s']-.2))
    checks.append('unfinished return is not reported completed; network clipped to horizon')
    for name,report in [('off',off),('returned',returned),('queue',queue),('local',local),('short',short)]:
        check(report);reports[name]=report
    # Actual workload run, not only analytic tiny cases.
    for topo in base.TOPOLOGIES:
        config=OUT/f'{topo}.json';config.write_text(json.dumps(scalable.make_topology(topo,7)),encoding='utf-8')
        for method in ('least_loaded','energy_priority'):
            inputs=base.workload('compute','medium',9201)
            for item in inputs:item.update(result_bits=item['data_bits']*.1,result_rate_bps=item['rate_bps'])
            report=scalable.run(config,inputs,method,env_factory=functools.partial(AccountedEnv,options=AccountingOptions(True,.2)),task_factory=AccountedTask)
            check(report);assert all(t['finished_s'] is not None for t in report['tasks'])
            reports[topo+'_'+method]=report
    checks.append('four topologies, both real strategies, 800 complete task executions with return traffic')
    for name,report in reports.items():(OUT/(name+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
    assert frozen.source_hashes()==source
    result=dict(passed=True,checks=checks,real_workload_runs=8,task_records=800,small_case_runs=6,
        frozen_model_unchanged=True,note='Extended energy model verified separately; do not transplant old saving/p-values onto changed accounting.')
    (OUT/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
if __name__=='__main__':main()
