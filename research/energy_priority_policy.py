"""Current energy-first dispatch, anchored to least-loaded service estimates."""
import hashlib
import math
import statistics
from pathlib import Path
from scheduling_support import (ROOT,HORIZON,ManagedEnv,MeteredTask,Scenario,profiles,
    committed_schedule,transfer_seconds,candidates as lifecycle_candidates)
from proposal_benchmark import choose,FREQUENCIES

PREDICTED_RESPONSE_RATIO=1.0

def communication_dominated(env,row):
    fastest_cpu=row['data_bits']*row['cycles_per_bit']/(max(FREQUENCIES)*1e9)
    transfers=[transfer_seconds(env,row['source'],f'n{i}',row['data_bits'],row['rate_bps'])[0]
        for i in range(len(FREQUENCIES)) if f'n{i}'!=row['source']]
    return bool(transfers) and fastest_cpu<=min(transfers)


def select_energy(env,row):
    options=lifecycle_candidates(env,row)
    reference=choose(env,row,'least_loaded')
    by_name={o['name']:o for o in options}
    limit=min(row['deadline_s'],by_name[reference]['response_s']*PREDICTED_RESPONSE_RATIO)
    busy=any(t.released_at is None for t in env.observed_tasks.values())
    guarded=busy and communication_dominated(env,row)
    feasible=[];rejected_deadlines=0
    if guarded:
        selected=by_name[reference]
        mode='busy_communication_reference'
        fallback=False
    else:
        for candidate in options:
            if candidate['response_s']>limit+1e-12:
                continue
            before=committed_schedule(env,candidate['name'])
            after=committed_schedule(env,candidate['name'],row)
            safe=True
            for task_id,times in before.items():
                task=env.observed_tasks[task_id]
                deadline=task.arrival_s+task.ddl
                if task.ddl>=0 and after[task_id]['finish_s']>max(deadline,times['finish_s'])+1e-12:
                    safe=False;break
            if safe:feasible.append(candidate)
            else:rejected_deadlines+=1
        selected=min(feasible,key=lambda c:(c['marginal_j'],c['response_s'],c['name'])) if feasible else min(options,key=lambda c:(c['response_s'],c['marginal_j'],c['name']))
        fallback=not bool(feasible)
        mode='fastest_deadline_fallback' if fallback else 'minimum_energy'
    return selected['name'],dict(reference_destination=reference,predicted_response_s=selected['response_s'],
        reference_response_s=by_name[reference]['response_s'],response_limit_s=limit,
        predicted_marginal_j=selected['marginal_j'],reference_marginal_j=by_name[reference]['marginal_j'],
        feasible_count=len(feasible),queued_deadline_rejections=rejected_deadlines,
        fallback_fastest=fallback,busy_communication_guard=guarded,selection_mode=mode,
        changed=selected['name']!=reference)


def dispatch(env,row,policy):
    if policy=='energy_priority':return select_energy(env,row)
    if policy=='least_loaded':return choose(env,row,policy),None
    raise ValueError(policy)


def run_case(path,rows,policy):
    env=ManagedEnv(Scenario(str(path)),profiles())
    env.logger.enable_logging=False
    env.enable_idle_sleep()
    decisions={}
    def producer():
        for row in rows:
            yield env.controller.timeout(row['arrival_s']-env.now)
            destination,decision=dispatch(env,row,policy)
            decisions[row['id']]=decision
            task=MeteredTask(id=row['id'],task_size=row['data_bits'],cycles_per_bit=row['cycles_per_bit'],
                trans_bit_rate=row['rate_bps'],src_name=row['source'],ddl=row['deadline_s'],memory_gb=row['memory_gb'])
            task.arrival_s=env.now
            env.submit(task,destination)
    env.controller.process(producer())
    env.run(until=HORIZON)
    report=env.energy_report()
    for row in report['tasks']:
        task=env.observed_tasks[row['id']]
        row.update(arrival_s=task.arrival_s,destination=task.assigned_destination,deadline_s=task.ddl,decision=decisions[row['id']])
    env.close()
    return report


def summarize(report,policy):
    rows=report['tasks'];complete=[r for r in rows if r['finished_s'] is not None]
    times=sorted(r['response_s'] for r in complete)
    return dict(policy=policy,submitted=len(rows),completed=len(complete),on_time=sum(r['on_time'] is True for r in complete),
        mean_response_s=statistics.mean(times) if times else None,
        p95_response_s=times[math.ceil(.95*len(times))-1] if times else None,
        total_j=report['total_j'],network_j=report['network_j'],
        changes=sum(bool(r.get('decision') and r['decision']['changed']) for r in rows))


def source_hashes():
    """Fingerprint this distribution; old frozen experiment hashes stay historical."""
    names=('energy_priority_policy.py','scheduling_support.py','proposal_benchmark.py',
           'proposal_simulation.py','proposal_energy_model.py')
    return {'research/'+name:hashlib.sha256((ROOT/'research'/name).read_bytes()).hexdigest()
            for name in names}
