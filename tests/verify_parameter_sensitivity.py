import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))

"""Independently rebuild sensitivity summaries from all saved task ledgers."""
import argparse
import copy
import csv
import itertools
import json
import math
from pathlib import Path
import parameter_sensitivity as study
import energy_priority_policy as policy

def near(a, b):
    assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7), (a, b)

def verify(out):
    stored = json.loads((out/'plan.json').read_text(encoding='utf-8'))
    plan = stored['plan']; study.validate(plan)
    result = json.loads((out/'verification.json').read_text(encoding='utf-8'))
    assert result['plan_sha256'] == study.sha(out/'plan.json')
    assert policy.source_hashes() == stored['frozen_source_sha256']
    for name, digest in stored['runner_sha256'].items():
        assert study.sha(study.ROOT/'research'/name) == digest
    for name, digest in stored['input_sha256'].items():
        assert study.sha(out/'inputs'/name) == digest
    expected = set(itertools.product([v['name'] for v in plan['variants']], plan['topologies'], plan['workloads'], plan['loads'], plan['seeds']))
    observed = {(r['variant'], r['topology'], r['workload'], r['load'], r['seed']) for r in result['results']}
    assert expected == observed and len(expected) == len(result['results'])
    records = 0
    for row in result['results']:
        for method, key in [('least_loaded', 'baseline'), ('energy_priority', 'current')]:
            name = row['case']+'_'+method+'.json'; path = out/'raw'/name
            assert study.sha(path) == row['raw_sha256'][name]
            raw = json.loads(path.read_text(encoding='utf-8'))
            near(raw['total_j'], sum(n['total_j'] for n in raw['nodes'].values())+raw['network_j'])
            near(raw['total_j'], raw['direct_task_energy_j']+raw['shared_infrastructure_j'])
            near(raw['total_j'], row[key]['total_j'])
            tasks = raw['tasks']; records += len(tasks)
            assert len({t['id'] for t in tasks}) == len(tasks) == 100
            finished = [t for t in tasks if t['finished_s'] is not None]
            assert len(finished) == row[key]['completed']
            for task in finished:
                near(task['response_s'], task['finished_s']-task['arrival_s'])
                assert bool(task['on_time']) == (task['response_s'] <= task['deadline_s'])
            assert sum(bool(t['on_time']) for t in tasks) == row[key]['on_time']
            if finished:
                near(sum(t['response_s'] for t in finished)/len(finished), row[key]['mean_response_s'])
        b, e = row['baseline'], row['current']
        near(row['saving_pct'], (1-e['total_j']/b['total_j'])*100)
        if b['mean_response_s'] and e['mean_response_s'] is not None:
            near(row['delay_change_pct'], (e['mean_response_s']/b['mean_response_s']-1)*100)
        assert row['service_passed'] == (row['delay_change_pct'] is not None and row['delay_change_pct'] < 5 and e['completed'] >= b['completed'] and e['on_time'] >= b['on_time'])
    for group in result['groups']:
        rows = [r for r in result['results'] if r['variant'] == group['variant']]
        b = sum(r['baseline']['total_j'] for r in rows); e = sum(r['current']['total_j'] for r in rows)
        near(group['baseline_j'], b); near(group['current_j'], e)
        near(group['energy_reduction_pct'], (1-e/b)*100)
        assert group['service_failure_cases'] == [r['case'] for r in rows if not r['service_passed']]
        assert group['energy_adverse_cases'] == [r['case'] for r in rows if r['saving_pct'] < -1e-9]
        assert group['effect_gates_passed'] == (e < b and not group['service_failure_cases'])
    with (out/'summary.csv').open(encoding='utf-8-sig', newline='') as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == len(expected)
    assert {r['case'] for r in exported} == {r['case'] for r in result['results']}
    assert records == result['task_policy_records']
    for mutate in (lambda p: p['variants'][0].update(name='../escape'),
                   lambda p: p['variants'][0].update(factor=float('nan')),
                   lambda p: p.update(seeds=[1, 1]),
                   lambda p: p['variants'][0].update(field='unknown')):
        bad = copy.deepcopy(plan); mutate(bad)
        try: study.validate(bad)
        except ValueError: pass
        else: raise AssertionError('invalid plan accepted')
    audit = dict(passed=True, cases=len(expected), raw_reports=2*len(expected), task_policy_records=records,
        raw_hashes_checked=True, energy_closure_checked=True, response_and_deadlines_recomputed=True,
        all_adverse_cases_retained=True, invalid_plans_rejected=True, frozen_sources_unchanged=True,
        all_variants_effect_passed=result['all_variants_effect_passed'])
    (out/'independent_verification.json').write_text(json.dumps(audit, indent=2), encoding='utf-8')
    print(json.dumps(audit), flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    verify(parser.parse_args().output)
