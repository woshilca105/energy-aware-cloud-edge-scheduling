"""Fit explicit power coefficients from measured CSV rows with a held-out run.

No measurements are fabricated or collected by this module. Synthetic test
fixtures are labelled synthetic and never become hardware precision evidence.
"""
import csv,math
from dataclasses import asdict,replace
from proposal_energy_model import PowerParameters

REQUIRED=('run_id','state','utilization','frequency_ghz','memory_gb','power_w')

def read_samples(path):
    with open(path,encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not set(REQUIRED)<=set(reader.fieldnames or []):raise ValueError('measurement CSV columns missing')
        rows=[]
        for row in reader:
            item={key:row[key] for key in REQUIRED}
            if not item['run_id']:raise ValueError('measurement run ID required')
            if item['state'] not in ('running','idle','sleep'):raise ValueError('unsupported measurement state')
            for key in REQUIRED[2:]:
                item[key]=float(item[key])
                if not math.isfinite(item[key]) or item[key]<0:raise ValueError('invalid measurement')
            if item['utilization']>1:raise ValueError('utilization outside [0,1]')
            rows.append(item)
    if not rows:raise ValueError('empty measurement set')
    return rows

def solve(matrix,target):
    a=[list(map(float,row))+[float(y)] for row,y in zip(matrix,target)];size=len(a)
    for column in range(size):
        pivot=max(range(column,size),key=lambda row:abs(a[row][column]))
        if abs(a[pivot][column])<1e-10:raise ValueError('measurements cannot identify coefficients; vary frequency/utilization and memory independently')
        a[pivot],a[column]=a[column],a[pivot]
        divisor=a[column][column];a[column]=[v/divisor for v in a[column]]
        for row in range(size):
            if row!=column:
                factor=a[row][column];a[row]=[x-factor*y for x,y in zip(a[row],a[column])]
    return [row[-1] for row in a]

def calibrate(training,validation,base_profile,metadata):
    if not training or not validation:raise ValueError('separate training and validation samples required')
    train_runs={r['run_id'] for r in training};valid_runs={r['run_id'] for r in validation}
    if train_runs&valid_runs:raise ValueError('calibration and validation runs overlap')
    source=metadata.get('source_kind')
    if source not in ('hardware_measurement','synthetic_fixture'):raise ValueError('explicit measurement provenance required')
    if source=='hardware_measurement' and not metadata.get('instrument'):raise ValueError('measurement instrument metadata required')
    rows=[r for r in training if r['state']=='running']
    if len(rows)<4:raise ValueError('insufficient running measurements')
    x=[[1.,r['utilization']**base_profile.beta*r['frequency_ghz']**3,r['memory_gb']] for r in rows]
    xtx=[[sum(row[i]*row[j] for row in x) for j in range(3)] for i in range(3)]
    xty=[sum(row[i]*r['power_w'] for row,r in zip(x,rows)) for i in range(3)]
    static,alpha,memory=solve(xtx,xty)
    if min(static,alpha,memory)< -1e-8:raise ValueError('unphysical negative fitted coefficient; check measurement domain')
    updates=dict(static_w=max(0,static),alpha_w_per_ghz3=max(0,alpha),memory_w_per_gb=max(0,memory))
    idle=[r for r in training if r['state']=='idle']
    if idle:updates['idle_adjustment_w']=sum(r['power_w']-static-memory*r['memory_gb'] for r in idle)/len(idle)
    sleep=[r['power_w'] for r in training if r['state']=='sleep']
    if sleep:updates['sleep_w']=sum(sleep)/len(sleep)
    fitted=replace(base_profile,**updates)
    errors=[]
    for row in validation:
        if row['state'] in ('idle','sleep') and not any(r['state']==row['state'] for r in training):raise ValueError('holdout state was not calibrated')
        pred=fitted.power_w(row['state'],row['utilization'],row['frequency_ghz'],row['memory_gb'])
        absolute=abs(pred-row['power_w']);relative=None if row['power_w']==0 else absolute/row['power_w']*100
        errors.append(dict(run_id=row['run_id'],measured_w=row['power_w'],predicted_w=pred,absolute_error_w=absolute,relative_error_pct=relative))
    relative=[r['relative_error_pct'] for r in errors if r['relative_error_pct'] is not None]
    return dict(profile=asdict(fitted),fixed_beta=base_profile.beta,mae_w=sum(r['absolute_error_w'] for r in errors)/len(errors),
        mape_pct=sum(relative)/len(relative) if relative else None,max_relative_error_pct=max(relative) if relative else None,
        training_runs=sorted(train_runs),validation_runs=sorted(valid_runs),validation_rows=len(validation),errors=errors,
        measurement_source=source,metadata=dict(metadata),hardware_measurements_supplied=source=='hardware_measurement',
        hardware_accuracy_verified=False,provenance_note='Source metadata is supplied by the caller, not independently authenticated; synthetic fixtures prove software only.',
        uncalibrated_parameters=['beta','wake_seconds','wake_w','wake_extra_j'])
