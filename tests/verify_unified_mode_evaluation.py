import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))

"""Audit each full ledger, placement restriction, configured bill and radar."""
import argparse
import itertools
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
import unified_mode_evaluation as study
from cost_evaluation import radar_svg

def near(a,b):
    assert math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-7),(a,b)

def verify(out):
    plan=json.loads((out/'plan.json').read_text(encoding='utf-8'))
    result=json.loads((out/'verification.json').read_text(encoding='utf-8'))
    assert result['plan_sha256']==study.sha(out/'plan.json')
    assert study.policy.source_hashes()==plan['frozen_source_sha256']
    for name,digest in plan['runner_sha256'].items():
        assert study.sha(study.ROOT/'research'/name)==digest
    for name,digest in result['input_sha256'].items():
        assert study.sha(out/'inputs'/name)==digest
    expected=set(itertools.product(plan['topologies'],plan['workloads'],plan['loads'],plan['seeds'],study.MODES))
    assert len(result['rows'])==len(expected)
    assert {(r['topology'],r['workload'],r['load'],r['seed'],r['mode']) for r in result['rows']}==expected
    records=0
    for row in result['rows']:
        file=out/'raw'/f"{row['case']}_{row['mode']}.json"
        assert study.sha(file)==row['raw_sha256']
        raw=json.loads(file.read_text(encoding='utf-8'))
        inputs=json.loads((out/'inputs'/f"{row['case']}.json").read_text(encoding='utf-8'))
        assert study.sha(out/'inputs'/f"{row['case']}.json")==row['input_sha256']
        near(raw['total_j'],sum(n['total_j'] for n in raw['nodes'].values())+raw['network_j'])
        near(raw['total_j'],raw['direct_task_energy_j']+raw['shared_infrastructure_j'])
        near(raw['total_j'],row['energy_j'])
        assert len(raw['tasks'])==len(inputs)==row['submitted']==100
        records+=len(inputs)
        completed=[]; on_time=0
        for task,source in zip(raw['tasks'],inputs):
            assert task['id']==source['id'] and task['deadline_s']==source['deadline_s']
            if task['finished_s'] is not None:
                near(task['response_s'],task['finished_s']-source['arrival_s'])
                assert task['on_time']==(task['response_s']<=source['deadline_s'])
                completed.append(task);on_time+=task['on_time']
        assert len(completed)==row['completed'] and on_time==row['on_time']
        if completed:near(sum(t['response_s'] for t in completed)/len(completed),row['mean_response_s'])
        config=json.loads((out/'inputs'/f"{row['topology']}.json").read_text(encoding='utf-8'))
        if row['mode'] in ('local','edge','cloud'):
            allowed={n['NodeName'] for n in study.tier_nodes(config,row['mode'])}
            assert all(t['destination'] in allowed for t in raw['tasks'])
        busy=sum(n['by_state_seconds']['running'] for n in raw['nodes'].values())
        rates=plan['pricing']
        components=dict(electricity=raw['total_j']/3600000*rates['electricity_per_kwh'],
            cpu=busy*rates['cpu_per_second'],network=raw['network_transmitted_bits']/8/1e9*rates['network_per_gb'])
        for key,value in components.items():near(value,row['cost_components'][key])
        near(sum(components.values()),row['model_cost'])
    rebuilt=study.aggregate(result['rows'],plan['pricing'])
    assert rebuilt==result['metrics']
    assert records==result['task_policy_records']
    failures=[];adverse=[]
    for case in sorted({r['case'] for r in result['rows']}):
        values={r['mode']:r for r in result['rows'] if r['case']==case}
        b,e=values['least_loaded'],values['energy_priority']
        delay=(e['mean_response_s']/b['mean_response_s']-1)*100 if b['mean_response_s'] and e['mean_response_s'] is not None else None
        if delay is None or delay>=5 or e['completed']<b['completed'] or e['on_time']<b['on_time']:failures.append(case)
        if e['energy_j']>b['energy_j']+1e-7:adverse.append(case)
    assert failures==result['service_failure_cases'] and adverse==result['energy_adverse_cases']
    assert result['effect_gates_passed']==(rebuilt['energy_priority']['energy_j']<rebuilt['least_loaded']['energy_j'] and not failures)
    for axis,direction in result['radar']['directions'].items():
        values=[m[axis] for m in rebuilt.values() if m[axis] is not None];low,high=min(values),max(values)
        for mode,metrics in rebuilt.items():
            expected_score=0 if metrics[axis] is None else 1 if high==low else (high-metrics[axis])/(high-low) if direction=='lower' else (metrics[axis]-low)/(high-low)
            near(result['radar']['coordinates'][mode][axis],expected_score)
    # Five and eight series must have distinct visible legend entries inside SVG.
    ns={'s':'http://www.w3.org/2000/svg'}
    for count in (2,5,8):
        metrics={f'mode{i}':next(iter(rebuilt.values())) for i in range(count)}
        svg=ET.fromstring(radar_svg(metrics));height=float(svg.attrib['viewBox'].split()[3])
        legends=[e for e in svg.findall('s:text',ns) if e.text and e.text.startswith('mode')]
        assert len(legends)==count and all(float(e.attrib['y'])<height for e in legends)
    saved_svg=ET.parse(out/'radar.svg').getroot()
    assert len([e for e in saved_svg.findall('s:text',ns) if e.text in study.MODES])==5
    audit=dict(passed=True,cases=result['cases'],fresh_runs=result['fresh_runs'],task_policy_records=records,
        placement_restrictions_checked=True,task_timing_checked=True,energy_closure_checked=True,
        configured_cost_units_checked=True,radar_coordinates_and_legend_bounds_checked=True,
        effect_gates_passed=result['effect_gates_passed'],frozen_sources_unchanged=True,
        verification_sha256=study.sha(out/'verification.json'),radar_sha256=study.sha(out/'radar.svg'))
    (out/'independent_verification.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
    print(json.dumps(audit),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    verify(parser.parse_args().output)
