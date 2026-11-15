"""Configured model costs and transparent radar coordinates, not real bills."""
import csv,json,math
from dataclasses import dataclass,asdict
from pathlib import Path

@dataclass(frozen=True)
class Pricing:
    electricity_per_kwh:float
    cpu_per_second:float=0.
    network_per_gb:float=0.
    currency:str='CNY'
    source_kind:str='scenario_assumption'
    source_note:str=''
    def __post_init__(self):
        for value in (self.electricity_per_kwh,self.cpu_per_second,self.network_per_gb):
            if not math.isfinite(value) or value<0:raise ValueError('finite nonnegative rates required')
        if not self.currency or not self.source_note:raise ValueError('currency and pricing provenance required')
        if self.source_kind not in ('scenario_assumption','supplied_tariff'):raise ValueError('explicit tariff type required')

def evaluate(report,pricing,transferred_bits=0.):
    if not math.isfinite(transferred_bits) or transferred_bits<0:raise ValueError('invalid transferred bits')
    tasks=report['tasks'];complete=[r for r in tasks if r['finished_s'] is not None]
    busy=sum(n['by_state_seconds']['running'] for n in report['nodes'].values())
    electricity=report['total_j']/3_600_000*pricing.electricity_per_kwh
    cpu=busy*pricing.cpu_per_second;network=transferred_bits/8/1e9*pricing.network_per_gb
    return dict(energy_j=report['total_j'],mean_response_s=sum(r['response_s'] for r in complete)/len(complete) if complete else None,
        throughput_tasks_per_s=len(complete)/report['horizon_s'],on_time_rate=sum(r['on_time'] is True for r in complete)/len(tasks) if tasks else 0.,
        mean_cpu_occupancy=busy/(len(report['nodes'])*report['horizon_s']),model_cost=electricity+cpu+network,
        cost_components=dict(electricity=electricity,cpu=cpu,network=network),pricing=asdict(pricing),
        cost_label='assumption-based model cost' if pricing.source_kind=='scenario_assumption' else 'calculated from supplied tariff, not measured bill')

def radar_coordinates(metrics):
    directions=dict(energy_j='lower',mean_response_s='lower',throughput_tasks_per_s='higher',on_time_rate='higher',model_cost='lower')
    result={name:{} for name in metrics};flat=[]
    for key,direction in directions.items():
        values=[m[key] for m in metrics.values() if m[key] is not None]
        if any(not math.isfinite(v) or v<0 for v in values):raise ValueError('invalid radar metric')
        if not values:raise ValueError('no defined values for metric')
        low,high=min(values),max(values)
        if high==low:flat.append(key)
        for name,m in metrics.items():
            value=m[key]
            score=0. if value is None else 1. if high==low else (high-value)/(high-low) if direction=='lower' else (value-low)/(high-low)
            result[name][key]=score
    return dict(coordinates=result,directions=directions,flat_axes=flat,aggregate_score=None,
        note='Within-this-comparison min-max display only; equal defined values score 1 and are labelled flat. Missing response scores 0. No arbitrary weighted winner.')

def radar_svg(metrics):
    from html import escape
    data=radar_coordinates(metrics);axes=list(data['directions']);cx,cy,r=320,220,145
    labels=['能耗','平均响应','吞吐量','按时率','模型成本']
    def point(i,scale):
        angle=-math.pi/2+2*math.pi*i/5
        return cx+r*scale*math.cos(angle),cy+r*scale*math.sin(angle)
    def points(values):return ' '.join(f'{point(i,v)[0]:.2f},{point(i,v)[1]:.2f}' for i,v in enumerate(values))
    height=max(470,430+20*len(metrics))
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 {height}" role="img" aria-label="五维归一化雷达图">',
           f'<rect width="640" height="{height}" fill="white"/>']
    for scale in (.25,.5,.75,1.):parts.append(f'<polygon points="{points([scale]*5)}" fill="none" stroke="#d1d5db"/>')
    for i,label in enumerate(labels):
        x,y=point(i,1);lx,ly=point(i,1.18)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x}" y2="{y}" stroke="#9ca3af"/>')
        parts.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-size="15" font-family="Microsoft YaHei,sans-serif">{label}</text>')
    for index,(name,scores) in enumerate(data['coordinates'].items()):
        color=('#2563eb','#0f766e','#c2410c','#7c3aed','#be185d','#374151')[index%6]
        parts.append(f'<polygon points="{points([scores[k] for k in axes])}" fill="{color}" fill-opacity=".12" stroke="{color}" stroke-width="2"/>')
        parts.append(f'<text x="20" y="{410+index*20}" fill="{color}" font-size="13">{escape(name)}</text>')
    parts.append('</svg>');return ''.join(parts)

def export_accounted_report(report,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    for name,rows in [('tasks',report['tasks']),('nodes',[dict(node=k,**v) for k,v in report['nodes'].items()])]:
        (directory/(name+'.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
        flat=[{k:v for k,v in row.items() if not isinstance(v,(dict,list,tuple))} for row in rows]
        fields=list(dict.fromkeys(k for row in flat for k in row))
        with (directory/(name+'.csv')).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(flat)
    summary={k:v for k,v in report.items() if k not in ('tasks','nodes')}
    (directory/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
