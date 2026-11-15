"""Small, explicit tabular Q-learning comparator with delayed observed rewards.

Training and frozen evaluation are distinct. This optional learner does not
replace the validated greedy strategy or inherit its savings claims.
"""
import hashlib,json,math,random
from pathlib import Path
import energy_priority_policy as greedy

class QLearningOffloader:
    def __init__(self,seed=0,training=True,epsilon=.15,alpha=.2,gamma=.8):
        if not (0<=epsilon<=1 and 0<alpha<=1 and 0<=gamma<1):raise ValueError('invalid Q-learning parameters')
        self.rng=random.Random(seed);self.training=training;self.epsilon=epsilon;self.alpha=alpha;self.gamma=gamma
        self.q={};self.pending={};self.updates=0;self.explorations=0
    def state(self,env,row):
        counts=[sum(t.assigned_destination==n and t.released_at is None for t in env.observed_tasks.values()) for n in env.ledgers]
        local=min(counts[0],3);remote=min(sum(counts[1:]),5)
        cpu=row['data_bits']*row['cycles_per_bit']/(max(greedy.FREQUENCIES)*1e9)
        work=min(int(math.log2(1+cpu)),5)
        return f'{len(counts)}|{local}|{remote}|{work}|{int(row["deadline_s"]>10)}'
    def actions(self,env,row):
        options=greedy.lifecycle_candidates(env,row);reference=greedy.choose(env,row,'least_loaded')
        by_name={o['name']:o for o in options}
        if any(t.released_at is None for t in env.observed_tasks.values()) and greedy.communication_dominated(env,row):return [by_name[reference]]
        limit=min(row['deadline_s'],by_name[reference]['response_s'])
        eligible=[]
        for c in options:
            if c['response_s']>limit+1e-12:continue
            before=greedy.committed_schedule(env,c['name']);after=greedy.committed_schedule(env,c['name'],row)
            if all(env.observed_tasks[k].ddl<0 or after[k]['finish_s']<=max(env.observed_tasks[k].arrival_s+env.observed_tasks[k].ddl,t['finish_s'])+1e-12 for k,t in before.items()):eligible.append(c)
        eligible=eligible or [min(options,key=lambda c:(c['response_s'],c['name']))]
        # Q is a residual selector among choices no worse than the validated
        # greedy decision under BOTH predictions. Do not let exploration spend
        # extra predicted energy merely to obtain a larger learned Q value.
        anchor_name,_=greedy.select_energy(env,row)
        anchor=by_name[anchor_name]
        return [c for c in eligible if c['marginal_j']<=anchor['marginal_j']+1e-12 and c['response_s']<=anchor['response_s']+1e-12] or [anchor]
    def observe(self,env,next_state=None,next_actions=()):
        if not self.training or not self.pending:return
        ready=[k for k in self.pending if env.observed_tasks[k].released_at is not None and getattr(env.observed_tasks[k],'result_received_at',env.observed_tasks[k].finished_at) is not None]
        if not ready:return
        report={r['id']:r for r in env.energy_report()['tasks']}
        for task_id in ready:
            state,action=self.pending.pop(task_id);row=report[task_id]
            energy=row.get('direct_energy_j')
            if energy is None:raise ValueError('Q rewards require observed complete energy accounting')
            deadline=env.observed_tasks[task_id].ddl
            penalty=0 if deadline<=0 else 10*max(0,row['response_s']/deadline-1)
            reward=-energy/50-penalty
            future=max((self.q.get((next_state,a),0.) for a in next_actions),default=0.) if next_state is not None else 0.
            old=self.q.get((state,action),0.)
            self.q[state,action]=old+self.alpha*(reward+self.gamma*future-old);self.updates+=1
    def __call__(self,env,row):
        state=self.state(env,row);options=self.actions(env,row)
        self.observe(env,state,[c['name'] for c in options])
        explore=self.training and self.rng.random()<self.epsilon
        if explore:selected=self.rng.choice(options);self.explorations+=1
        else:selected=min(options,key=lambda c:(-self.q.get((state,c['name']),-c['marginal_j']/50),c['marginal_j'],c['response_s'],c['name']))
        if self.training:self.pending[row['id']]=(state,selected['name'])
        return selected['name'],dict(policy='q_learning',state=state,exploration=explore,training=self.training,known_q_entries=len(self.q))
    def finish_episode(self,env):
        self.observe(env)
        if self.pending:raise ValueError('training episode ended with unobserved rewards; do not silently discard tasks')
    def save(self,path):
        payload=dict(format_version=1,algorithm='greedy-shielded residual Q-learning with delayed observed task-energy reward',alpha=self.alpha,gamma=self.gamma,
            q=[dict(state=s,action=a,value=v) for (s,a),v in sorted(self.q.items())],updates=self.updates,explorations=self.explorations,
            reward='-direct_model_j/50 - 10*max(0,response/deadline-1)',features='node_count,local_backlog,remote_backlog,compute_time_bin,deadline_bin')
        Path(path).write_text(json.dumps(payload,indent=2),encoding='utf-8')
    @classmethod
    def load(cls,path):
        data=json.loads(Path(path).read_text());
        if data.get('format_version')!=1:raise ValueError('unsupported learner format')
        obj=cls(training=False,epsilon=0,alpha=data['alpha'],gamma=data['gamma'])
        for r in data['q']:
            if not math.isfinite(r['value']):raise ValueError('non-finite Q value')
            key=r['state'],r['action']
            if key in obj.q:raise ValueError('duplicate Q state/action')
            obj.q[key]=r['value']
        obj.updates=data['updates'];obj.explorations=data['explorations'];return obj
