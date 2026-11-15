"""Run a reproducible current experiment and render its offline results."""
import argparse,csv,functools,hashlib,json,math,uuid,webbrowser
from pathlib import Path
from cost_evaluation import Pricing,evaluate,radar_svg,export_accounted_report
ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'research_records/project_demo'

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def close(a,b):
    if not math.isclose(float(a),float(b),rel_tol=1e-9,abs_tol=1e-7):raise ValueError('current result inconsistent')


def run_current(args):
    import scalable_execution as scalable
    import proposal_benchmark as base
    from complete_energy_accounting import AccountedEnv,AccountedTask,AccountingOptions
    from energy_priority_policy import source_hashes
    directory=ROOT/'research_records/current_runs'/('run_'+uuid.uuid4().hex[:12]);directory.mkdir(parents=True)
    config=scalable.make_topology(args.topology,args.nodes);config_path=directory/'config.json'
    config_path.write_text(json.dumps(config,indent=2),encoding='utf-8')
    rows=base.workload(args.workload,args.load,args.seed)
    if not math.isfinite(args.result_ratio) or args.result_ratio<0:raise ValueError('nonnegative result ratio required')
    for r in rows:r.update(result_bits=r['data_bits']*args.result_ratio,result_rate_bps=r['rate_bps'])
    (directory/'inputs.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    profiles={}
    if args.calibration_training or args.calibration_validation or args.calibration_metadata:
        from power_calibration import calibrate,read_samples
        if not all((args.calibration_training,args.calibration_validation,args.calibration_metadata)):
            raise ValueError('training CSV, validation CSV and provenance JSON all required')
        if args.calibration_profile:raise ValueError('choose supplied profile or fit measurements, not both')
        _,defaults=scalable.configuration(config)
        if args.calibration_node not in defaults:raise ValueError('unknown calibration node')
        fitted=calibrate(read_samples(args.calibration_training),read_samples(args.calibration_validation),defaults[args.calibration_node],read(Path(args.calibration_metadata)))
        path=directory/'calibrated_profile.json';path.write_text(json.dumps(fitted,ensure_ascii=False,indent=2),encoding='utf-8')
        args.calibration_profile=str(path)
    if args.calibration_profile:
        calibrated=read(Path(args.calibration_profile))
        if args.calibration_node not in {n['NodeName'] for n in config['Nodes']}:raise ValueError('calibration node not in topology')
        profiles[args.calibration_node]=calibrated['profile']
    pricing=Pricing(args.electricity_rate,args.cpu_rate,args.network_rate,source_note='user-configurable scenario assumptions; not a measured bill')
    options=AccountingOptions(True,args.nic_idle_w);frozen=source_hashes()
    plan=dict(nodes=args.nodes,topology=args.topology,workload=args.workload,load=args.load,seed=args.seed,
        result_ratio=args.result_ratio,nic_idle_w=args.nic_idle_w,source_sha256=frozen,input_sha256=sha(directory/'inputs.json'),
        config_sha256=sha(config_path),pricing=pricing.__dict__,calibration_profile_supplied=bool(profiles),
        calibration_profile_sha256=sha(Path(args.calibration_profile)) if args.calibration_profile else None,
        current_policy=args.policy,learning_model_sha256=sha(Path(args.learning_model)) if args.policy=='q_learning' else None)
    (directory/'plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    reports={};metrics={}
    for method in ('least_loaded',args.policy):
        agent=None
        if method=='q_learning':
            from q_learning_offloader import QLearningOffloader
            agent=QLearningOffloader.load(args.learning_model)
        report=scalable.run(config_path,rows,method,power_overrides=profiles,
            env_factory=functools.partial(AccountedEnv,options=options),task_factory=AccountedTask,selector=agent)
        # Original input identity/deadline validation remains mandatory.
        if len(report['tasks'])!=len(rows):raise ValueError('task missing')
        for t,r in zip(report['tasks'],rows):
            if t['id']!=r['id'] or t['deadline_s']!=r['deadline_s']:raise ValueError('task or deadline changed')
            if t['finished_s'] is not None:close(t['response_s'],t['finished_s']-r['arrival_s'])
        close(report['direct_task_energy_j']+report['shared_infrastructure_j'],report['total_j'])
        (directory/(method+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
        export_accounted_report(report,directory/method)
        transferred=report['network_transmitted_bits']
        metrics[method]=evaluate(report,pricing,transferred);reports[method]=report
    if source_hashes()!=frozen:raise ValueError('frozen source mutated')
    case=f"n{args.nodes}_{args.topology}_{args.workload}_{args.load}_{args.seed}"
    values={p:dict(total_j=r['total_j'],mean_response_s=metrics[p]['mean_response_s'],submitted=len(r['tasks']),completed=sum(t['finished_s'] is not None for t in r['tasks']),on_time=sum(t['on_time'] is True for t in r['tasks'])) for p,r in reports.items()}
    b=values['least_loaded'];e=values[args.policy]
    saving=(1-e['total_j']/b['total_j'])*100
    delay=(e['mean_response_s']/b['mean_response_s']-1)*100 if e['mean_response_s'] is not None and b['mean_response_s'] else None
    data=dict(mode='new_extended_run',title='本次运行 · 完整计费扩展',source=str(directory.relative_to(ROOT)),
        cases=[dict(case=case,topology=args.topology,workload=args.workload,load=args.load,seed=args.seed,baseline=b,current=e,saving_pct=saving,delay_change_pct=delay)],
        tasks={case:{p:[[t['id'],t['destination'],t['arrival_s'],t['response_s'],t['deadline_s'],t['on_time'],t['direct_energy_j'],t['return_network_j'],t['queued_memory_j']] for t in r['tasks']] for p,r in reports.items()}},
        p_value=None,metrics=metrics,radar_svg=radar_svg(metrics),current_policy=args.policy,accounting_note='含实际结果回传、等待内存及独立NIC基础功耗；共享能耗不任意摊分。',
        gates=dict(energy=saving>0,service=delay is not None and delay<5 and e['on_time']>=b['on_time'] and e['completed']>=b['completed'],significance=None),
        pricing_note='费用为配置费率下的模型计算；默认电价等仅为情景假设。该次运行没有统计显著性结论。')
    (directory/'result.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return data


def feature_status():
    items=[]
    for key,label,path,field in [
        ('scale','可变规模与参数覆盖','function_scale_checks_v1/verification.json','functional_checks_passed'),
        ('accounting','回传、等待内存与任务能耗','function_accounting_checks_v1/verification.json','passed'),
        ('calibration','标定工具、成本与数据导出','function_calibration_cost_checks_v1/verification.json','passed'),
        ('sensitivity','功耗参数敏感性扫描','function_sensitivity_checks_v1/independent_verification.json','passed'),
        ('modes','完整账本云边端统一评价','unified_mode_evaluation_v1/independent_verification.json','passed'),
        ('learning','受保护学习对照','function_learning_checks_v2/verification.json','effect_gates_passed')]:
        p=ROOT/'research_records'/path;value=read(p) if p.exists() else {}
        items.append(dict(key=key,label=label,passed=value.get(field) is True,source=path))
    return items


def render(data):
    OUTPUT.mkdir(exist_ok=True);data=dict(data,features=feature_status())
    modes=ROOT/'research_records/unified_mode_evaluation_v1'
    if (modes/'independent_verification.json').exists():
        checked=read(modes/'independent_verification.json')
        if checked.get('passed') is not True or sha(modes/'verification.json')!=checked['verification_sha256'] or sha(modes/'radar.svg')!=checked['radar_sha256']:
            raise ValueError('unified modes evidence changed')
        evaluation=read(modes/'verification.json')
        data['mode_evaluation']=dict(source='unified_mode_evaluation_v1',cases=evaluation['cases'],
            metrics=evaluation['metrics'],radar_svg=(modes/'radar.svg').read_text(encoding='utf-8'))
    if 'radar_svg' not in data:
        # Primary cost is an explicit energy-only assumption; no task energy invented.
        metrics={}
        for p,key in [('least_loaded','baseline'),('energy_priority','current')]:
            total=sum(c[key]['total_j'] for c in data['cases']);complete=sum(c[key]['completed'] for c in data['cases'])
            metrics[p]=dict(energy_j=total,mean_response_s=sum(c[key]['mean_response_s']*c[key]['completed'] for c in data['cases'])/complete,
                throughput_tasks_per_s=complete/(len(data['cases'])*1500),on_time_rate=sum(c[key]['on_time'] for c in data['cases'])/(len(data['cases'])*100),model_cost=total/3600000*.8)
        data['metrics']=metrics;data['radar_svg']=radar_svg(metrics)
    template=(ROOT/'research/current_dashboard.html').read_text(encoding='utf-8')
    if template.count('__CURRENT_DATA__')!=1:raise ValueError('dashboard placeholder invalid')
    payload=json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<',chr(92)+'u003c')
    (OUTPUT/'index.html').write_text(template.replace('__CURRENT_DATA__',payload),encoding='utf-8')
    (OUTPUT/'current_data.json').write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return OUTPUT/'index.html'


def main():
    parser=argparse.ArgumentParser(description='当前节能项目：校验已有主批次或运行完整计费的新配置')
    parser.add_argument('--no-open',action='store_true');parser.add_argument('--run-current',action='store_true')
    parser.add_argument('--nodes',type=int,default=7);parser.add_argument('--topology',choices=('star','tree','mesh','hybrid'),default='star')
    parser.add_argument('--workload',choices=('compute','io','latency'),default='compute');parser.add_argument('--load',choices=('low','medium','high'),default='medium');parser.add_argument('--seed',type=int,default=9601)
    parser.add_argument('--result-ratio',type=float,default=.1);parser.add_argument('--nic-idle-w',type=float,default=.2)
    parser.add_argument('--electricity-rate',type=float,default=.8);parser.add_argument('--cpu-rate',type=float,default=0.);parser.add_argument('--network-rate',type=float,default=0.)
    parser.add_argument('--calibration-profile');parser.add_argument('--calibration-node',default='n0')
    parser.add_argument('--calibration-training');parser.add_argument('--calibration-validation');parser.add_argument('--calibration-metadata')
    parser.add_argument('--policy',choices=('energy_priority','q_learning'),default='energy_priority')
    parser.add_argument('--learning-model',default=str(ROOT/'research_records/function_learning_checks_v2/q_model.json') if (ROOT/'research_records/function_learning_checks_v2/q_model.json').exists() else str(ROOT/'research/assets/q_model.json'))
    args=parser.parse_args()
    args.run_current=True
    if not args.run_current and any((args.calibration_profile,args.calibration_training,args.calibration_validation,args.calibration_metadata)):
        parser.error('calibration options require --run-current')
    data=run_current(args);path=render(data)
    print(json.dumps(dict(mode=data['mode'],source=data['source'],cases=len(data['cases']),gates=data['gates'],page=str(path)),ensure_ascii=False),flush=True)
    if not args.no_open:webbrowser.open(path.resolve().as_uri())


if __name__=='__main__':main()
