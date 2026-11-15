import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))

"""Known-coefficient calibration, bad-data rejection, cost units and exports."""
import csv,json,math
from pathlib import Path
from dataclasses import replace
from proposal_energy_model import PowerParameters
from power_calibration import calibrate,read_samples
from cost_evaluation import Pricing,evaluate,radar_coordinates,export_accounted_report
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'research_records/function_calibration_cost_checks_v1'
def rejected(fn):
    try:fn()
    except ValueError:return
    raise AssertionError('invalid input accepted')
def main():
    OUT.mkdir(parents=True,exist_ok=False)
    truth=PowerParameters(9,3,1.75,.7,-.6,.4,.4,10,1)
    samples=[]
    for split in ('train','holdout'):
        for i in range(24):
            u=.2+.03*(i%7);f=.6+.2*(i%5);m=.5+.7*(i%3)
            if split=='holdout':u+=.015;f+=.05;m+=.12
            state='running'
            samples.append(dict(run_id=split,state=state,utilization=u,frequency_ghz=f,memory_gb=m,power_w=truth.power_w(state,u,f,m)))
        for state in ('idle','sleep'):
            samples.append(dict(run_id=split,state=state,utilization=0,frequency_ghz=0,memory_gb=0,power_w=truth.power_w(state)))
    sample_path=OUT/'synthetic_software_fixture.csv'
    with sample_path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(samples[0]));w.writeheader();w.writerows(samples)
    loaded=read_samples(sample_path);train=[r for r in loaded if r['run_id']=='train'];hold=[r for r in loaded if r['run_id']=='holdout']
    result=calibrate(train,hold,replace(truth,static_w=4,alpha_w_per_ghz3=1,memory_w_per_gb=.1),dict(source_kind='synthetic_fixture',instrument='software fixture, not physical'))
    for key in ('static_w','alpha_w_per_ghz3','memory_w_per_gb','idle_adjustment_w','sleep_w'):assert math.isclose(result['profile'][key],getattr(truth,key),abs_tol=1e-8)
    assert result['max_relative_error_pct']<1e-7 and not result['hardware_accuracy_verified'] and not result['hardware_measurements_supplied']
    rejected(lambda:calibrate(train,train,truth,dict(source_kind='synthetic_fixture')))
    rejected(lambda:calibrate([dict(train[0]) for _ in range(5)],hold,truth,dict(source_kind='synthetic_fixture')))
    rejected(lambda:calibrate(train,hold,truth,dict(source_kind='hardware_measurement')))
    report=json.loads((ROOT/'research_records/function_accounting_checks_v1/queue.json').read_text())
    pricing=Pricing(.8,.001,.2,source_note='explicit test assumptions; not a real tariff')
    values=evaluate(report,pricing,transferred_bits=8e9)
    assert math.isclose(values['cost_components']['network'],.2)
    assert math.isclose(values['cost_components']['electricity'],report['total_j']/3600000*.8)
    other=dict(values,energy_j=values['energy_j']*1.1,model_cost=values['model_cost']*1.1)
    radar=radar_coordinates({'current':values,'other':other})
    assert radar['coordinates']['current']['energy_j']==1 and radar['coordinates']['other']['energy_j']==0
    assert radar['aggregate_score'] is None and 'on_time_rate' in radar['flat_axes']
    rejected(lambda:Pricing(-1,source_note='bad input'))
    export_accounted_report(report,OUT/'export')
    with (OUT/'export/tasks.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    assert len(rows)==2 and all('direct_energy_j' in r and 'queued_memory_j' in r and 'return_network_j' in r for r in rows)
    assert json.loads((OUT/'export/tasks.json').read_text())==report['tasks']
    output=dict(passed=True,calibration=result,cost=values,radar=radar,
        checks=['known coefficients recovered from separate runs','overlap and singular design rejected','unproven hardware provenance not promoted to precision evidence','energy conversion J/kWh and GB costs','radar direction and equal-axis semantics','actual task/direct/network/memory CSV/JSON export'],
        actual_hardware_measurements=False)
    (OUT/'verification.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(passed=True,checks=output['checks'],actual_hardware_measurements=False)),flush=True)
if __name__=='__main__':main()
