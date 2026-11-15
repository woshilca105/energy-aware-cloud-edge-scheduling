import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))

"""Train only on fixed training seeds, save, reload and freeze on new seeds."""
import functools,json
from pathlib import Path
import scalable_execution as scalable
from complete_energy_accounting import AccountedEnv,AccountedTask,AccountingOptions
from q_learning_offloader import QLearningOffloader
import proposal_benchmark as base
import energy_priority_policy as original
ROOT=scalable.ROOT;OUT=ROOT/'research_records/function_learning_checks_v2'
def execute(config,inputs,method='least_loaded',agent=None):
    return scalable.run(config,inputs,method,env_factory=functools.partial(AccountedEnv,options=AccountingOptions(True,.2)),task_factory=AccountedTask,selector=agent)
def main():
    frozen=original.source_hashes();OUT.mkdir(parents=True,exist_ok=False)
    configs={}
    for topo in base.TOPOLOGIES:
        path=OUT/(topo+'.json');path.write_text(json.dumps(scalable.make_topology(topo,7)),encoding='utf-8');configs[topo]=path
    plan=dict(training_seeds=[9301,9302,9303],evaluation_seeds=[9501,9502],training_topology='star',kinds=list(base.WORKLOADS),loads=list(base.LOADS),
        primary_strategy_unchanged=True,note='optional comparator; service and energy outcomes disclosed separately, no borrowed p-value')
    (OUT/'plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    agent=QLearningOffloader(seed=9300);train_count=0
    for seed in plan['training_seeds']:
        for kind in base.WORKLOADS:
            for load in base.LOADS:
                report=execute(configs['star'],base.workload(kind,load,seed),agent=agent);train_count+=1
    assert agent.updates==train_count*100 and agent.q and agent.explorations>0
    path=OUT/'q_model.json';agent.save(path);frozen_agent=QLearningOffloader.load(path)
    assert frozen_agent.q==agent.q and not frozen_agent.training
    snapshot=dict(frozen_agent.q);updates=frozen_agent.updates;results=[]
    for topo in base.TOPOLOGIES:
        for seed in plan['evaluation_seeds']:
            for kind in base.WORKLOADS:
                for load in base.LOADS:
                    rows=base.workload(kind,load,seed)
                    reports={p:execute(configs[topo],rows,agent=frozen_agent if p=='q_learning' else None) for p in ('least_loaded','q_learning')}
                    values={}
                    for p,r in reports.items():
                        assert len(r['tasks'])==100 and all(t['finished_s'] is not None for t in r['tasks'])
                        values[p]=dict(total_j=r['total_j'],mean_response_s=sum(t['response_s'] for t in r['tasks'])/100,on_time=sum(t['on_time'] is True for t in r['tasks']))
                        (OUT/f'{topo}_{kind}_{load}_{seed}_{p}.json').write_text(json.dumps(r),encoding='utf-8')
                    b=values['least_loaded'];e=values['q_learning']
                    results.append(dict(topology=topo,seed=seed,workload=kind,load=load,policies=values,
                        delay_change_pct=(e['mean_response_s']/b['mean_response_s']-1)*100,energy_reduction_pct=(1-e['total_j']/b['total_j'])*100))
        print('learning evaluation topology',topo,'complete',flush=True)
    assert frozen_agent.q==snapshot and frozen_agent.updates==updates and not frozen_agent.pending
    assert original.source_hashes()==frozen
    b=sum(r['policies']['least_loaded']['total_j'] for r in results);e=sum(r['policies']['q_learning']['total_j'] for r in results)
    result=dict(functional_checks_passed=True,training_runs=train_count,training_task_updates=agent.updates,
        q_entries=len(agent.q),exploratory_selections=agent.explorations,evaluation_cases=len(results),evaluation_runs=2*len(results),
        evaluation_model_frozen=True,training_evaluation_seeds_disjoint=True,
        evaluation_energy_reduction_pct=(1-e/b)*100,worst_delay_change_pct=max(r['delay_change_pct'] for r in results),
        service_failure_cases=sum(r['delay_change_pct']>=5 or r['policies']['q_learning']['on_time']<r['policies']['least_loaded']['on_time'] for r in results),
        results=results,scope='Optional learning comparator. Functional pass is not superiority or replacement of the main validated greedy algorithm.')
    result['effect_gates_passed']=result['evaluation_energy_reduction_pct']>0 and result['service_failure_cases']==0
    (OUT/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='results'}),flush=True)
    assert result['effect_gates_passed'],'learning comparator energy/service acceptance failed'
if __name__=='__main__':main()
