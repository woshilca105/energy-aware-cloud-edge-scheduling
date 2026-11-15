"""Five placement policies under one full ledger, workload and configured tariff."""
import argparse
import csv
import functools
import json
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import scalable_execution as scalable
import proposal_benchmark as base
import energy_priority_policy as policy
from complete_energy_accounting import AccountedEnv, AccountedTask, AccountingOptions
from cost_evaluation import Pricing, evaluate, radar_coordinates, radar_svg
from parameter_sensitivity import sha

ROOT = scalable.ROOT
MODES = ('local', 'edge', 'cloud', 'least_loaded', 'energy_priority')

def tier_nodes(config, mode):
    nodes = config['Nodes']
    if mode == 'local':
        return [n for n in nodes if n['NodeId'] == 0]
    if mode == 'edge':
        return [n for n in nodes if n['NodeId'] != 0 and n['MaxCpuFreq'] < 2e9]
    if mode == 'cloud':
        return [n for n in nodes if n['MaxCpuFreq'] >= 2e9]
    raise ValueError('unknown fixed tier')

def select_tier(config, mode, env, row):
    options = []
    for node in tier_nodes(config, mode):
        name = node['NodeName']; pending = 0.
        for task in env.observed_tasks.values():
            if task.assigned_destination != name or task.released_at is not None:
                continue
            if task.started_at is None:
                pending += task.task_size*task.cycles_per_bit/node['MaxCpuFreq']+1
            else:
                end = max(env.now, task.started_at+task.exe_time)
                pending += max(0, task.started_at+task.exe_time-env.now)+max(0, math.floor(end)+1-end)
        options.append((pending, node['NodeId'], name))
    if not options:
        raise ValueError('no nodes in selected tier')
    return min(options)[2], dict(mode=mode, selection='least CPU backlog within fixed tier')

def job(spec):
    out_s, topo, kind, load, seed, pricing_data = spec
    out = Path(out_s); config_path = out/'inputs'/f'{topo}.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    rows = base.workload(kind, load, seed)
    for row in rows:
        row.update(result_bits=row['data_bits']*.1, result_rate_bps=row['rate_bps'])
    case = f'{topo}_{kind}_{load}_{seed}'
    input_path = out/'inputs'/f'{case}.json'
    input_path.write_text(json.dumps(rows), encoding='utf-8')
    summaries = []
    for mode in MODES:
        selector = functools.partial(select_tier, config, mode) if mode in ('local', 'edge', 'cloud') else None
        report = scalable.run(config_path, rows, mode,
            env_factory=functools.partial(AccountedEnv, options=AccountingOptions(True, .2)),
            task_factory=AccountedTask, selector=selector)
        assert len(report['tasks']) == len(rows)
        assert math.isclose(report['direct_task_energy_j']+report['shared_infrastructure_j'], report['total_j'], rel_tol=1e-9)
        if selector:
            allowed = {n['NodeName'] for n in tier_nodes(config, mode)}
            assert all(t['destination'] in allowed for t in report['tasks'])
        path = out/'raw'/f'{case}_{mode}.json'
        path.write_text(json.dumps(report), encoding='utf-8')
        metrics = evaluate(report, Pricing(**pricing_data), report['network_transmitted_bits'])
        summaries.append(dict(case=case, topology=topo, workload=kind, load=load, seed=seed, mode=mode,
            submitted=len(rows), completed=sum(t['finished_s'] is not None for t in report['tasks']),
            on_time=sum(t['on_time'] is True for t in report['tasks']), **metrics,
            input_sha256=sha(input_path), raw_sha256=sha(path)))
    return summaries

def aggregate(rows, pricing):
    result = {}
    for mode in MODES:
        group = [r for r in rows if r['mode'] == mode]
        submitted = sum(r['submitted'] for r in group); completed = sum(r['completed'] for r in group)
        result[mode] = dict(energy_j=sum(r['energy_j'] for r in group),
            mean_response_s=sum(r['mean_response_s']*r['completed'] for r in group if r['mean_response_s'] is not None)/completed if completed else None,
            throughput_tasks_per_s=completed/(len(group)*1500), on_time_rate=sum(r['on_time'] for r in group)/submitted,
            model_cost=sum(r['model_cost'] for r in group), submitted=submitted, completed=completed,
            on_time=sum(r['on_time'] for r in group),
            mean_cpu_occupancy=sum(r['mean_cpu_occupancy'] for r in group)/len(group),
            cost_components={key: sum(r['cost_components'][key] for r in group) for key in ('electricity', 'cpu', 'network')},
            pricing=pricing)
    return result

def run_study(out, seeds=(9801, 9802), workers=4):
    out = Path(out).resolve(); out.mkdir(parents=True, exist_ok=False)
    (out/'inputs').mkdir(); (out/'raw').mkdir()
    pricing = Pricing(.8, .00001, .02, source_note='Fixed synthetic scenario tariff: CNY/kWh .8, CNY/CPU-second .00001, CNY/per-hop GB .02; not a real bill.')
    frozen = policy.source_hashes()
    for topo in base.TOPOLOGIES:
        (out/'inputs'/f'{topo}.json').write_text(json.dumps(scalable.make_topology(topo, 7)), encoding='utf-8')
    plan = dict(seeds=list(seeds), topologies=list(base.TOPOLOGIES), workloads=list(base.WORKLOADS), loads=list(base.LOADS),
        modes=list(MODES), nodes=7, horizon_s=1500, result_ratio=.1, nic_idle_w=.2,
        pricing=pricing.__dict__, frozen_source_sha256=frozen,
        runner_sha256={n:sha(ROOT/'research'/n) for n in ('unified_mode_evaluation.py', 'scalable_execution.py', 'complete_energy_accounting.py', 'cost_evaluation.py')},
        scope='Same seven-node infrastructure kept in every mode, including idle/sleep. Fixed placement tiers, not separate hardware deployments. Nearest includes source and is identical to local for these n0-source workloads; not counted as an extra independent run.')
    (out/'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    specs = [(str(out), t, k, l, s, pricing.__dict__) for t in base.TOPOLOGIES for k in base.WORKLOADS for l in base.LOADS for s in seeds]
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(job, s) for s in specs]):
            rows.extend(future.result())
            if len(rows) % 60 == 0:
                print(f'unified modes {len(rows)}/{len(specs)*len(MODES)}', flush=True)
    rows.sort(key=lambda r:(r['case'], r['mode']))
    metrics = aggregate(rows, pricing.__dict__)
    pairs = {r['case']:{} for r in rows}
    for row in rows:
        pairs[row['case']][row['mode']] = row
    failures = []; adverse = []
    for case, values in pairs.items():
        b, e = values['least_loaded'], values['energy_priority']
        delay = (e['mean_response_s']/b['mean_response_s']-1)*100 if b['mean_response_s'] and e['mean_response_s'] is not None else None
        if delay is None or delay >= 5 or e['completed'] < b['completed'] or e['on_time'] < b['on_time']:
            failures.append(case)
        if e['energy_j'] > b['energy_j']+1e-7:
            adverse.append(case)
    assert policy.source_hashes() == frozen
    result = dict(functional_checks_passed=True, cases=len(specs), fresh_runs=len(rows), task_policy_records=sum(r['submitted'] for r in rows),
        plan_sha256=sha(out/'plan.json'), input_sha256={p.name:sha(p) for p in (out/'inputs').glob('*.json')},
        metrics=metrics, radar=radar_coordinates(metrics), service_failure_cases=failures, energy_adverse_cases=adverse,
        effect_gates_passed=metrics['energy_priority']['energy_j'] < metrics['least_loaded']['energy_j'] and not failures,
        frozen_sources_unchanged=True, rows=rows)
    (out/'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (out/'radar.svg').write_text(radar_svg(metrics), encoding='utf-8')
    fields = ['case', 'mode', 'submitted', 'completed', 'on_time', 'energy_j', 'mean_response_s', 'throughput_tasks_per_s', 'on_time_rate', 'mean_cpu_occupancy', 'model_cost']
    with (out/'summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        writer.writerows({key:r[key] for key in fields} for r in rows)
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows', 'input_sha256', 'radar')}, ensure_ascii=False), flush=True)
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    run_study(parser.parse_args().output)
