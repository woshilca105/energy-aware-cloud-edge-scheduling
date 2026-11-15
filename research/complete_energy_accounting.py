"""Observed-task energy, return-flow simulation, queued memory and NIC baseline.

Direct task joules and shared infrastructure joules are separate. The latter
are not arbitrarily allocated to individual tasks. Frozen reports are untouched.
"""
import math
from dataclasses import dataclass
from proposal_simulation import MeteredTask
from core.task import Task
from scheduling_support import ManagedEnv
from proposal_energy_model import finite_nonnegative

@dataclass(frozen=True)
class AccountingOptions:
    queued_memory: bool=True
    nic_idle_w_per_node: float=0.
    def __post_init__(self):finite_nonnegative(self.nic_idle_w_per_node,'NIC idle watts')

class AccountedTask(MeteredTask):
    def __init__(self,*args,result_bits=0.,result_rate_bps=None,**kwargs):
        super().__init__(*args,**kwargs)
        finite_nonnegative(result_bits,'result bits')
        self.result_bits=result_bits
        self.result_rate_bps=self.trans_bit_rate if result_rate_bps is None else result_rate_bps
        if not math.isfinite(self.result_rate_bps) or self.result_rate_bps<=0:raise ValueError('positive result transmission rate required')
        self.input_buffer_start=None;self.input_buffer_end=None
        self.return_started=False;self.result_received_at=None
    def allocate(self,now,node=None,pre_allocate=False):
        super().allocate(now,node,pre_allocate)
        if not pre_allocate:self.input_buffer_end=now

class AccountedEnv(ManagedEnv):
    def __init__(self,scenario,parameters,options=None):
        super().__init__(scenario,parameters)
        self.accounting_options=options or AccountingOptions()
    def _handle_task_execution(self,task,dst,flag_reactive):
        if not flag_reactive and task.input_buffer_start is None:task.input_buffer_start=self.now
        yield from super()._handle_task_execution(task,dst,flag_reactive)
        if task.finished_at is not None and not task.return_started:
            task.return_started=True
            if task.result_bits==0 or task.assigned_destination==task.src_name:task.result_received_at=self.now
            else:self.controller.process(self._return_result(task))
    def _return_result(self,task):
        # Use upstream network allocation and release rather than an unexecuted estimate.
        proxy=Task(id=('return',task.id),task_size=task.result_bits,cycles_per_bit=0,
                   trans_bit_rate=task.result_rate_bps,src_name=task.assigned_destination)
        yield from self._handle_task_transmission(proxy,task.src_name)
        task.result_received_at=self.now
    def energy_report(self):
        report=super().energy_report();horizon=report['horizon_s']
        extras={n:dict(queued_memory_j=0.,nic_baseline_j=self.accounting_options.nic_idle_w_per_node*horizon) for n in report['nodes']}
        transfer_j={task_id:dict(input_network_j=0.,return_network_j=0.) for task_id in self.observed_tasks}
        transmitted_bits=0.
        for transfer in self.transfers:
            raw_id=transfer['task_id'];is_return=isinstance(raw_id,tuple) and raw_id[0]=='return'
            owner=raw_id[1] if is_return else raw_id
            amount=max(0,min(horizon,transfer['end'])-transfer['start'])*self.nic_w
            task=self.observed_tasks[owner]
            rate=task.result_rate_bps if is_return else task.trans_bit_rate
            transmitted_bits+=max(0,min(horizon,transfer['end'])-transfer['start'])*rate
            transfer_j[owner]['return_network_j' if is_return else 'input_network_j']+=amount
        for row in report['tasks']:
            t=self.observed_tasks[row['id']];node=t.assigned_destination;p=self.ledgers[node].parameters
            end=min(horizon,t.released_at if t.released_at is not None else horizon)
            occupied=max(0,end-t.started_at) if t.started_at is not None else 0.
            executed=max(0,min(horizon,t.finished_at if t.finished_at is not None else horizon)-t.started_at) if t.started_at is not None else 0.
            frequency=self.scenario.get_node(node).max_cpu_freq/1e9
            power=p.power_w('running',1.,frequency,t.memory_gb)
            buffer_end=t.input_buffer_end if t.input_buffer_end is not None else horizon
            buffer_s=max(0,min(horizon,buffer_end)-t.input_buffer_start) if t.input_buffer_start is not None else 0.
            memory_j=buffer_s*t.memory_gb*p.memory_w_per_gb if self.accounting_options.queued_memory else 0.
            extras[node]['queued_memory_j']+=memory_j
            response=None if t.result_received_at is None else t.result_received_at-t.arrival_s
            row.update(cpu_finished_s=t.finished_at,finished_s=t.result_received_at,response_s=response,
                on_time=None if response is None else t.ddl<0 or response<=t.ddl,
                execution_j=power*executed,collector_holding_j=power*max(0,occupied-executed),queued_memory_j=memory_j,
                result_bits=t.result_bits,result_received_s=t.result_received_at,**transfer_j[t.id])
            row['direct_energy_j']=sum(row[k] for k in ('execution_j','collector_holding_j','queued_memory_j','input_network_j','return_network_j'))
        for name,node in report['nodes'].items():
            node['cpu_memory_state_j']=node['total_j']
            node.update(extras[name]);node['total_j']+=sum(extras[name].values())
        report['total_j']=sum(n['total_j'] for n in report['nodes'].values())+report['network_j']
        report['direct_task_energy_j']=sum(t['direct_energy_j'] for t in report['tasks'])
        report['network_transmitted_bits']=transmitted_bits
        shared=sum(sum(n['by_state_j'][s] for s in ('idle','sleep','waking'))+n['nic_baseline_j'] for n in report['nodes'].values())
        report['shared_infrastructure_j']=shared
        if not math.isclose(report['direct_task_energy_j']+shared,report['total_j'],rel_tol=1e-9,abs_tol=1e-7):raise AssertionError('task/shared energy does not close')
        report['accounting']=dict(queued_memory=self.accounting_options.queued_memory,
            nic_idle_w_per_node=self.accounting_options.nic_idle_w_per_node,
            response_definition='arrival to result received, including return transfer',
            shared_energy_allocation='unallocated idle/sleep/wake/NIC baseline; never described as direct task energy')
        return report
