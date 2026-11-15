# 云边端能耗感知任务调度

基于 [RayCloudSim](https://github.com/ZhangRui111/RayCloudSim) 的仿真项目：在任务完成和响应时间约束下，通过选择执行节点减少整体模型能耗。不是实际云服务，不执行真实图片处理等业务计算。

## 安装和运行

在项目根目录打开终端，Windows使用Python 3.12（已验证3.12.10）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-project.txt
.\启动项目.cmd
```

默认真正执行一次最小负载与节能策略的配对仿真，并打开本地页面。结果保存到 `research_records/current_runs/run_编号/`，生成数据不上传Git。

```powershell
.\.venv\Scripts\python.exe research/project_runner.py --nodes 7 --topology star --workload compute --load medium --seed 9901 --no-open
```

拓扑可选 `star/tree/mesh/hybrid`，任务可选 `compute/io/latency`，负载可选 `low/medium/high`，节点至少4个。页面支持任务明细、CSV导出和五维指标。单场景可能节能、持平或增加能耗，程序如实展示。

Linux/macOS使用相应虚拟环境的 `python research/project_runner.py --no-open`，再打开生成的HTML。本次实测环境是Windows。

## 阅读顺序

先沿一次任务的运行过程阅读以下六个入口，不需要一开始理解全部文件：

| 顺序 | research目录中的文件 | 先理解什么 |
|---|---|---|
| 1 | project_runner.py | 程序从哪里启动 |
| 2 | current_project_view.py | 如何组织输入、运行两种策略并展示结果 |
| 3 | proposal_benchmark.py | 任务、拓扑和最小负载基线 |
| 4 | energy_priority_policy.py | 当前策略如何选节点、保护已提交任务的期限 |
| 5 | scheduling_support.py | 排队、响应、增量能耗预测和自动休眠 |
| 6 | complete_energy_accounting.py | 仿真结果怎样计量，预测为什么不等于最终结果 |

## 代码结构

- `core/`：原作者仿真底座，保留版权和来源。
- `examples/scenarios/`：运行必需的场景适配。
- `research/`：当前实现；能耗原语在 `proposal_energy_model.py`，事件适配在 `proposal_simulation.py`，可变规模执行在 `scalable_execution.py`，离线页面为 `current_dashboard.html`。
- `research/cost_evaluation.py`：假设费率成本、导出和雷达；`power_calibration.py`：标定CSV拟合及留出验证。
- `research/q_learning_offloader.py`、`parameter_sensitivity.py`、`unified_mode_evaluation.py`：可选学习、参数实验、云边端评价，独立于主线阅读。
- `research/assets/`：小型模型和运行示例。
- `tests/`：六个独立核验脚本，不是应用运行依赖。

旧策略v1—v5、旧工作台和依赖未发布历史数据的分析入口已移出展示版。仍在使用的休眠和预测函数提取到 `scheduling_support.py`，并非简单删除依赖。重构前后各228次运行、合计456次仿真，覆盖4/7/13节点、四拓扑、三任务、三级负载及冻结学习模型，逐份完整报告完全一致。这是行为回归检查，不是新的显著性或普遍节能证明。

## 验证与可选实验

以下检查可从新克隆直接运行。脚本拒绝覆盖同名旧结果；重跑请保留旧数据并使用新的干净工作副本。

```powershell
.\.venv\Scripts\python.exe tests/verify_complete_accounting.py
.\.venv\Scripts\python.exe tests/verify_calibration_cost.py
```

以下批量实验运行较久；输出目录必须尚不存在：

```powershell
.\.venv\Scripts\python.exe research/parameter_sensitivity.py --output research_records/sensitivity_run_1
.\.venv\Scripts\python.exe tests/verify_parameter_sensitivity.py research_records/sensitivity_run_1
.\.venv\Scripts\python.exe research/unified_mode_evaluation.py --output research_records/modes_run_1
.\.venv\Scripts\python.exe tests/verify_unified_mode_evaluation.py research_records/modes_run_1
```

学习对照运行：`.\启动项目.cmd --policy q_learning`。训练/冻结评估代码为 `tests/verify_learning_offloader.py`，模型来源见 `research/assets/README.md`。学习仅在贪心保护的候选中选动作，不能将全部收益归于学习。规模检查代码为 `tests/verify_scalable_execution.py`。

## 结果边界与来源

能耗是合成参数下的模型J，没有真实仪器测量，不能宣称2%/5%硬件误差达标。成本使用注明的假设费率，不是真实账单。任务直接能耗和共享基础设施能耗分开；模式对比是在同一套设施上改变执行位置；雷达只作内部归一化展示，不计算任意综合赢家。

本仓库不附个人日志、申请书及大量历史实验；小型示例不代替多种子统计。历史失败版本仍保留在原始研发记录及Git历史中，不冒称没有尝试过。

RayCloudSim原作者及贡献者的版权、MIT许可证保留在 `LICENSE`，本项目扩展主要在 `research/`，不将底座作为独立原创。LEAF用于此前本地组件核对，CloudSim/YAFS为资料参考，不宣称跨平台验证。
