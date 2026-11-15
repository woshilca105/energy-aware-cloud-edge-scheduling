import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))

"""Run fixed scale/parameter cases, inspect data and disclose effect separately."""
import json,hashlib,math
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor,as_completed
import networkx as nx
import scalable_execution as scalable
import energy_priority_policy as original
import proposal_benchmark as base
def close(a,b):assert math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-7),(a,b)


def audit(report,rows):
    inputs={r['id']:r for r in rows};assert len(report['tasks'])==len(inputs)==100
    assert {t['id'] for t in report['tasks']}==set(inputs)
    complete=0;ontime=0;responses=[]
    for t in report['tasks']:
        r=inputs[t['id']]
        if t['finished_s'] is None:continue
        response=t['finished_s']-r['arrival_s'];close(response,t['response_s'])
        on=r['deadline_s']<0 or response<=r['deadline_s'];assert on==t['on_time']
        complete+=1;ontime+=on;responses.append(response)
    for n in report['nodes'].values():
        close(sum(n['by_state_seconds'].values()),report['horizon_s'])
        close(sum(n['by_state_j'].values()),n['total_j'])
    close(sum(n['total_j'] for n in report['nodes'].values())+report['network_j'],report['total_j'])
    return dict(completed=complete,on_time=ontime,total_j=report['total_j'],response_sum_s=sum(responses))



ROOT=scalable.ROOT
OUT=ROOT/'research_records/function_scale_checks_v1'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def job(spec):
    n,topo,kind,load,seed=spec
    path=OUT/'inputs'/f'{topo}_{n}.json';rows=base.workload(kind,load,seed)
    reports={p:scalable.run(path,rows,p) for p in ('least_loaded','energy_priority')}
    for p,r in reports.items():
        audit(r,rows);assert len(r['nodes'])==n
        (OUT/f'n{n}_{topo}_{kind}_{load}_{seed}_{p}.json').write_text(json.dumps(r),encoding='utf-8')
    b=original.summarize(reports['least_loaded'],'least_loaded');e=original.summarize(reports['energy_priority'],'energy_priority')
    return dict(nodes=n,topology=topo,workload=kind,load=load,seed=seed,baseline=b,current=e,
        saving_pct=(1-e['total_j']/b['total_j'])*100,delay_change_pct=(e['mean_response_s']/b['mean_response_s']-1)*100)
def main():
    frozen=original.source_hashes();OUT.mkdir(parents=True,exist_ok=False);(OUT/'inputs').mkdir()
    for n in (4,7,13):
        for topo in base.TOPOLOGIES:
            config=scalable.make_topology(topo,n)
            g=nx.Graph();g.add_nodes_from(range(n));g.add_edges_from((x['SrcNodeID'],x['DstNodeID']) for x in config['Edges']);assert nx.is_connected(g)
            if n==7:assert config==base.topology(topo)
            (OUT/'inputs'/f'{topo}_{n}.json').write_text(json.dumps(config),encoding='utf-8')
    # Seven-node compatibility: exact report replay, not only matching aggregates.
    rows=base.workload('compute','medium',9101)
    for p in ('least_loaded','energy_priority'):
        assert scalable.run(OUT/'inputs/star_7.json',rows,p)==original.run_case(OUT/'inputs/star_7.json',rows,p)
    old=tuple(base.FREQUENCIES)
    try:
        with scalable.parameter_context((1.,),{},100):raise RuntimeError('intentional exception')
    except RuntimeError:pass
    assert tuple(base.FREQUENCIES)==old
    specs=[(n,t,k,l,s) for n in (4,7,13) for t in base.TOPOLOGIES for k in base.WORKLOADS for l in base.LOADS for s in (9101,9102,9103)]
    plan=dict(nodes=[4,7,13],seeds=[9101,9102,9103],cases=len(specs),policies=['least_loaded','energy_priority'],source_sha256=frozen,
              purpose='fixed functional scale validation and separate descriptive effect audit; no new claim of universal savings')
    (OUT/'plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    results=[]
    with ProcessPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(job,s) for s in specs]):
            results.append(future.result())
            if len(results)%36==0:print('scale cases',len(results),'/',len(specs),flush=True)
    # Parameter overrides must affect the actual ledger, not just presentation.
    normal=scalable.run(OUT/'inputs/star_7.json',rows,'least_loaded')
    changed=scalable.run(OUT/'inputs/star_7.json',rows,'least_loaded',power_overrides={f'n{i}':{'sleep_w':.75} for i in range(7)})
    expected=.25*sum(r['by_state_seconds']['sleep'] for r in normal['nodes'].values())
    assert math.isclose(changed['total_j']-normal['total_j'],expected,rel_tol=1e-9)
    totals={}
    for n in (4,7,13):
        group=[r for r in results if r['nodes']==n];b=sum(r['baseline']['total_j'] for r in group);e=sum(r['current']['total_j'] for r in group)
        totals[n]=dict(energy_reduction_pct=(1-e/b)*100,worst_delay_change_pct=max(r['delay_change_pct'] for r in group),
            service_failure_cases=sum(r['delay_change_pct']>=5 or r['current']['on_time']<r['baseline']['on_time'] or r['current']['completed']<r['baseline']['completed'] for r in group))
    assert original.source_hashes()==frozen
    result=dict(functional_checks_passed=True,cases=len(results),fresh_runs=2*len(results),task_policy_records=200*len(results),
        canonical_topologies_exact=True,canonical_policy_replay_exact=True,exception_context_restored=True,
        power_override_integral_checked=True,effects_by_scale=totals,results=sorted(results,key=lambda r:(r['nodes'],r['topology'],r['workload'],r['load'],r['seed'])))
    (OUT/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='results'}),flush=True)
if __name__=='__main__':main()
