"""Paired, full-accounting power-parameter sweep; preserves every adverse case."""
import argparse
import csv
import functools
import hashlib
import json
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import scalable_execution as scalable
import proposal_benchmark as benchmark
import energy_priority_policy as policy
from complete_energy_accounting import AccountedEnv, AccountedTask, AccountingOptions

ROOT = scalable.ROOT

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def default_plan():
    variants = [dict(name='reference', field=None, factor=1.)]
    for field in ('alpha_w_per_ghz3', 'memory_w_per_gb', 'sleep_w'):
        for factor in (.5, 2.):
            variants.append(dict(name=f'{field}_{factor:g}x', field=field, factor=factor))
    return dict(nodes=7, topologies=list(benchmark.TOPOLOGIES), workloads=list(benchmark.WORKLOADS),
                loads=list(benchmark.LOADS), seeds=[9701, 9702], variants=variants,
                horizon_s=1500., result_ratio=.1, nic_idle_w=.2,
                purpose='Fixed paired descriptive sensitivity study, not hardware calibration or significance evidence.')

def validate(plan):
    scalable.make_topology('star', plan['nodes'])
    for key, allowed in [('topologies', benchmark.TOPOLOGIES), ('workloads', benchmark.WORKLOADS), ('loads', benchmark.LOADS)]:
        if not plan[key] or len(set(plan[key])) != len(plan[key]) or not set(plan[key]) <= set(allowed):
            raise ValueError('invalid '+key)
    if not plan['seeds'] or len(set(plan['seeds'])) != len(plan['seeds']) or any(type(s) is not int for s in plan['seeds']):
        raise ValueError('distinct integer seeds required')
    names = set()
    for variant in plan['variants']:
        name = variant['name']
        if not name or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.' for c in name) or name in names:
            raise ValueError('unique safe variant name required')
        names.add(name)
        if variant['field'] not in (None, 'alpha_w_per_ghz3', 'memory_w_per_gb', 'sleep_w'):
            raise ValueError('unsupported sensitivity field')
        if not math.isfinite(variant['factor']) or variant['factor'] <= 0:
            raise ValueError('positive finite multiplier required')
    if not names or not any(v['field'] is None and v['factor'] == 1 for v in plan['variants']):
        raise ValueError('unchanged reference variant required')
    for key in ('horizon_s', 'result_ratio', 'nic_idle_w'):
        if not math.isfinite(plan[key]) or plan[key] < 0 or (key == 'horizon_s' and plan[key] == 0):
            raise ValueError('invalid '+key)

def job(spec):
    out_s, plan, variant, topo, kind, load, seed = spec
    out = Path(out_s)
    config_path = out/'inputs'/f'{topo}.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    _, profiles = scalable.configuration(config)
    overrides = {node: {variant['field']: getattr(profile, variant['field'])*variant['factor']}
                 for node, profile in profiles.items()} if variant['field'] else None
    rows = benchmark.workload(kind, load, seed)
    for row in rows:
        row.update(result_bits=row['data_bits']*plan['result_ratio'], result_rate_bps=row['rate_bps'])
    case = f"{variant['name']}_{topo}_{kind}_{load}_{seed}"
    values = {}
    hashes = {}
    for method in ('least_loaded', 'energy_priority'):
        report = scalable.run(config_path, rows, method, horizon=plan['horizon_s'], power_overrides=overrides,
            env_factory=functools.partial(AccountedEnv, options=AccountingOptions(True, plan['nic_idle_w'])), task_factory=AccountedTask)
        assert len(report['tasks']) == len(rows)
        assert math.isclose(report['direct_task_energy_j']+report['shared_infrastructure_j'], report['total_j'], rel_tol=1e-9, abs_tol=1e-7)
        assert math.isfinite(report['total_j']) and report['total_j'] > 0
        completed = [t for t in report['tasks'] if t['finished_s'] is not None]
        values[method] = dict(total_j=report['total_j'], completed=len(completed),
            on_time=sum(bool(t['on_time']) for t in report['tasks']),
            mean_response_s=sum(t['response_s'] for t in completed)/len(completed) if completed else None)
        file = out/'raw'/f'{case}_{method}.json'
        file.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        hashes[file.name] = sha(file)
    b, e = values['least_loaded'], values['energy_priority']
    delay = (e['mean_response_s']/b['mean_response_s']-1)*100 if b['mean_response_s'] and e['mean_response_s'] is not None else None
    return dict(case=case, variant=variant['name'], topology=topo, workload=kind, load=load, seed=seed,
        baseline=b, current=e, saving_pct=(1-e['total_j']/b['total_j'])*100, delay_change_pct=delay,
        service_passed=delay is not None and delay < 5 and e['completed'] >= b['completed'] and e['on_time'] >= b['on_time'],
        raw_sha256=hashes)

def run_study(plan, out, workers=4):
    validate(plan)
    if type(workers) is not int or workers < 1:
        raise ValueError('positive worker count required')
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out/'inputs').mkdir(); (out/'raw').mkdir()
    frozen = policy.source_hashes()
    for topo in plan['topologies']:
        (out/'inputs'/f'{topo}.json').write_text(json.dumps(scalable.make_topology(topo, plan['nodes'])), encoding='utf-8')
    sources = {name: sha(ROOT/'research'/name) for name in ('parameter_sensitivity.py', 'scalable_execution.py', 'complete_energy_accounting.py')}
    stored = dict(plan=plan, frozen_source_sha256=frozen, runner_sha256=sources,
                  input_sha256={p.name: sha(p) for p in (out/'inputs').glob('*.json')})
    (out/'plan.json').write_text(json.dumps(stored, indent=2), encoding='utf-8')
    specs = [(str(out), plan, v, t, k, l, s) for v in plan['variants'] for t in plan['topologies']
             for k in plan['workloads'] for l in plan['loads'] for s in plan['seeds']]
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(job, spec) for spec in specs]):
            results.append(future.result())
            if len(results) % 36 == 0:
                print(f'sensitivity cases {len(results)}/{len(specs)}', flush=True)
    results.sort(key=lambda r: r['case'])
    groups = []
    for variant in plan['variants']:
        group = [r for r in results if r['variant'] == variant['name']]
        b = sum(r['baseline']['total_j'] for r in group)
        e = sum(r['current']['total_j'] for r in group)
        failures = [r['case'] for r in group if not r['service_passed']]
        delays = [r['delay_change_pct'] for r in group if r['delay_change_pct'] is not None]
        groups.append(dict(variant=variant['name'], cases=len(group), baseline_j=b, current_j=e,
            energy_reduction_pct=(1-e/b)*100, worst_delay_change_pct=max(delays) if delays else None,
            energy_adverse_cases=[r['case'] for r in group if r['saving_pct'] < -1e-9],
            service_failure_cases=failures, effect_gates_passed=e < b and not failures))
    assert policy.source_hashes() == frozen
    result = dict(functional_checks_passed=True, cases=len(results), fresh_runs=2*len(results),
        task_policy_records=sum(r[p]['completed'] for r in results for p in ('baseline', 'current')),
        frozen_sources_unchanged=True, all_variants_effect_passed=all(g['effect_gates_passed'] for g in groups),
        plan_sha256=sha(out/'plan.json'), groups=groups, results=results)
    (out/'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    with (out/'summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['case', 'variant', 'topology', 'workload', 'load', 'seed', 'saving_pct', 'delay_change_pct', 'service_passed'])
        writer.writeheader(); writer.writerows({key: row[key] for key in writer.fieldnames} for row in results)
    print(json.dumps({k:v for k,v in result.items() if k != 'results'}, ensure_ascii=False), flush=True)
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8-sig')) if args.plan else default_plan()
    run_study(plan, args.output, args.workers)

if __name__ == '__main__':
    main()
