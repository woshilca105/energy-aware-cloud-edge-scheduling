"""Application-aligned energy primitives. Seconds, watts, GHz, GB and joules.

This module records explicit states; it does not yet schedule RayCloudSim jobs,
choose CPU frequencies, or claim that any parameter profile is calibrated.
"""
from dataclasses import dataclass
import math


def finite_nonnegative(value, name):
    if not math.isfinite(value) or value < 0:
        raise ValueError(f'{name} must be finite and nonnegative')


@dataclass(frozen=True)
class PowerParameters:
    static_w: float
    alpha_w_per_ghz3: float
    beta: float
    memory_w_per_gb: float
    idle_adjustment_w: float
    sleep_w: float
    wake_seconds: float
    wake_w: float
    wake_extra_j: float = 0.0

    def __post_init__(self):
        for name in ('static_w','alpha_w_per_ghz3','memory_w_per_gb','sleep_w','wake_seconds','wake_w','wake_extra_j'):
            finite_nonnegative(getattr(self,name),name)
        if not math.isfinite(self.beta) or self.beta<=0:
            raise ValueError('beta must be finite and positive')
        if not math.isfinite(self.idle_adjustment_w) or self.static_w+self.idle_adjustment_w<0:
            raise ValueError('idle correction must not make idle power negative')

    def power_w(self, state, utilization=0.0, frequency_ghz=0.0, memory_gb=0.0):
        finite_nonnegative(frequency_ghz,'frequency_ghz')
        finite_nonnegative(memory_gb,'memory_gb')
        if not math.isfinite(utilization) or not 0<=utilization<=1:
            raise ValueError('utilization must be in [0,1]')
        if state=='running':
            if frequency_ghz<=0:
                raise ValueError('running requires a positive frequency')
            return self.static_w+self.alpha_w_per_ghz3*utilization**self.beta*frequency_ghz**3+self.memory_w_per_gb*memory_gb
        if state=='idle':
            if utilization!=0:
                raise ValueError('idle utilization must be zero')
            return self.static_w+self.memory_w_per_gb*memory_gb+self.idle_adjustment_w
        if state=='sleep':
            if utilization!=0 or frequency_ghz!=0 or memory_gb!=0:
                raise ValueError('sleep power includes the entire low-power state; do not add active terms')
            return self.sleep_w
        if state=='waking':
            if utilization!=0 or frequency_ghz!=0 or memory_gb!=0:
                raise ValueError('waking cannot execute work')
            return self.wake_w
        raise ValueError('unknown power state')


class StateEnergyLedger:
    """Integrate explicit transitions; prevent work before wake-up completes."""
    def __init__(self, parameters):
        self.parameters=parameters
        self.now=0.0
        self.state='idle'
        self.utilization=0.0
        self.frequency_ghz=0.0
        self.memory_gb=0.0
        self.ready_at=None
        self.energy_j={name:0.0 for name in ('running','idle','sleep','waking')}
        self.duration_s={name:0.0 for name in self.energy_j}
        self.wake_count=0

    def advance(self, until):
        finite_nonnegative(until,'time')
        if until<self.now:
            raise ValueError('time cannot go backwards')
        if self.state=='waking':
            stop=min(until,self.ready_at)
            duration=stop-self.now
            self.energy_j['waking']+=duration*self.parameters.wake_w
            self.duration_s['waking']+=duration
            self.now=stop
            if self.now>=self.ready_at:
                self.state='idle'
                self.ready_at=None
            else:
                return
        duration=until-self.now
        power=self.parameters.power_w(self.state,self.utilization,self.frequency_ghz,self.memory_gb)
        self.energy_j[self.state]+=duration*power
        self.duration_s[self.state]+=duration
        self.now=until

    def _check_available(self, now):
        finite_nonnegative(now,'time')
        if now<self.now:
            raise ValueError('time cannot go backwards')
        if self.state=='sleep' or (self.state=='waking' and now<self.ready_at):
            raise ValueError('wake-up must finish before the node becomes available')

    def set_running(self, now, utilization, frequency_ghz, memory_gb):
        self.parameters.power_w('running',utilization,frequency_ghz,memory_gb)
        self._check_available(now)
        self.advance(now)
        self.state='running'
        self.utilization,self.frequency_ghz,self.memory_gb=utilization,frequency_ghz,memory_gb

    def set_idle(self, now, memory_gb=0.0):
        self.parameters.power_w('idle',memory_gb=memory_gb)
        self._check_available(now)
        self.advance(now)
        self.state='idle'
        self.utilization=self.frequency_ghz=0.0
        self.memory_gb=memory_gb

    def sleep(self, now):
        if self.state not in ('idle','waking') or (self.state=='waking' and now<self.ready_at):
            raise ValueError('only an available idle node can sleep')
        self.advance(now)
        self.state='sleep'
        self.utilization=self.frequency_ghz=self.memory_gb=0.0

    def wake(self, now):
        if self.state!='sleep':
            raise ValueError('wake is only valid from sleep')
        self.advance(now)
        self.state='waking'
        self.ready_at=now+self.parameters.wake_seconds
        self.energy_j['waking']+=self.parameters.wake_extra_j
        self.wake_count+=1
        # A zero-duration transition still pays the explicit transition cost.
        self.advance(now)

    def summary(self, until):
        self.advance(until)
        return dict(total_j=sum(self.energy_j.values()),by_state_j=dict(self.energy_j),
                    by_state_seconds=dict(self.duration_s),wake_count=self.wake_count,state=self.state)


def link_energy_mb(data_mb, rate_mbps, tx_w, rx_w):
    """One hop, decimal MB and Mbit/s; transmission only, not propagation delay."""
    for value,name in ((data_mb,'data_mb'),(tx_w,'tx_w'),(rx_w,'rx_w')):
        finite_nonnegative(value,name)
    if not math.isfinite(rate_mbps) or rate_mbps<=0:
        raise ValueError('rate_mbps must be finite and positive')
    duration_s=8*data_mb/rate_mbps
    return dict(duration_s=duration_s,energy_j=duration_s*(tx_w+rx_w))


def total_system_energy(node_ledgers_j, link_transfers_j):
    """Already-integrated node joules plus already-integrated link joules."""
    values=list(node_ledgers_j)+list(link_transfers_j)
    for value in values:
        finite_nonnegative(value,'energy_j')
    return sum(values)
