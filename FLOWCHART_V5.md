# V5 Complete Workflow Flowchart

V5 = V3 philosophy (Agent A sole brain, Runtime B pure executor), with two changes:
- **Runtime B runs on a Lambda MicroVM** (not AgentCore). Runtime A manages its lifecycle.
- **Report rendering moved into Runtime A** (Runtime B is shell/python only).

## End-to-End Flow

```
用户请求: "分析各区域Q1销售达成率"
│
▼
┌─── Runtime A (AgentCore; Agent A = 唯一大脑) ───────────────────────────┐
│                                                                         │
│  @app.entrypoint (async generator → SSE)                                │
│  │                                                                      │
│  │  ① resolve tenant_id + session_id                                    │
│  │                                                                      │
│  │  ② ┌─ MicroVM 生命周期: 启动 ────────────────────────────┐          │
│  │    │  yield status "正在启动 MicroVM 工作站..."          │          │
│  │    │  run_microvm(image, exec_role, ingress, egress) ────┼──┐       │
│  │    │  poll get_microvm until state==RUNNING (~2.3s)      │  │       │
│  │    │  create_microvm_auth_token → X-aws-proxy-auth       │  │       │
│  │    │  yield status "MicroVM 就绪 (2.25s)"                 │  │       │
│  │    └──────────────────────────────────────────────────────┘  │     │
│  │                                                              │ 启动  │
│  │  ③ yield status "Agent 开始分析..."                          │ MicroVM│
│  │                                                              ▼       │
│  │  ④ ┌─ Phase 1: Agent A stream_async (Opus) ────────────┐  ┌────────────────────┐
│  │    │  LLM 思考: "下载数据"                               │  │ Runtime B          │
│  │    │   ▼ yield status "正在执行: runtime_b_shell"        │  │ (Lambda MicroVM)   │
│  │    │   ▼ HTTPS POST {action:shell} ──────────────────────┼─▶│ :8080              │
│  │    │   ◀──────── JSON {stdout,exit_code} ─────────────────┼──│ subprocess.run     │
│  │    │   ▼ yield status "runtime_b_shell 完成"             │  │ (aws s3 cp 下载)   │
│  │    │  LLM 思考: "pandas 分析"                            │  │                    │
│  │    │   ▼ yield status "正在执行: runtime_b_python"       │  │                    │
│  │    │   ▼ HTTPS POST {action:python} ─────────────────────┼─▶│ exec(code)         │
│  │    │   ◀──────── JSON {stdout,output_files} ──────────────┼──│ (pandas/matplotlib)│
│  │    │   ▼ yield status "runtime_b_python 完成"            │  │ → /tmp/workspace/  │
│  │    │  LLM 思考: "出图 + 上传 S3"                          │  │   output/ (持久)   │
│  │    │   ▼ HTTPS POST {action:shell: aws s3 cp ↑} ─────────┼─▶│ 上传产出到 S3       │
│  │    │  LLM: end_turn → 退出循环                           │  └────────────────────┘
│  │    │  analysis_result = 关键发现文本                      │            │
│  │    └──────────────────────────────────────────────────────┘            │
│  │                                                                         │
│  │  ⑤ yield status "数据准备完成"                                          │
│  │                                                                         │
│  │  ⑥ ┌─ Phase 2: Report (在 Runtime A 内渲染, 不再调 B) ──┐               │
│  │    │  yield status "正在生成分析报告 (Opus streaming)"   │               │
│  │    │  bedrock.converse_stream(Opus, analysis_result)     │               │
│  │    │   ▼ for chunk in stream:                            │               │
│  │    │       yield {"type":"chunk","content": chunk} ──────┼─▶ 前端逐 token │
│  │    │  s3.put_object(analysis_report.md)                  │               │
│  │    │  yield {"type":"done","s3_keys":[...]}              │               │
│  │    └──────────────────────────────────────────────────────┘             │
│  │                                                                         │
│  │  ⑦ finally: terminate_microvm(id) ──────────────────────────┐          │
│  │     yield 结束; MicroVM 回收, 状态/磁盘销毁                  │ 回收      │
│  └──────────────────────────────────────────────────────────────┼─────────┘
│                                                                  ▼
└──────────────────────────────────────────────────────── MicroVM TERMINATED
```

## SSE Event Stream (actual, verified on AWS us-west-2)

```
[status]  正在启动 MicroVM 工作站...              ← ② run_microvm
[status]  MicroVM 就绪 (2.25s)                     ← cold start 2.25s (PENDING→RUNNING)
[status]  Agent 开始分析...                        ← ③
[status]  正在执行: runtime_b_shell                ← ④ Phase 1, 每个 tool call 实时
[status]  正在执行: runtime_b_python
[status]  正在执行: runtime_b_shell
[status]  正在执行: runtime_b_python
[status]  正在执行: runtime_b_shell
[status]  数据准备完成                             ← ⑤
[status]  正在生成分析报告 (Opus streaming)...      ← ⑥ Phase 2, 报告在 A 渲染
[chunk]   # 2026 Q1 各区域销售达成率分析报告 ...    ← 745 chunks 流式
[chunk]   ## 概述 ...
   ...
[done]    s3_keys: [analysis_report.md]            ← put_object 完成
                                                     (MicroVM 在 finally 中 terminate)
= 10 status + 745 chunk + 1 done = 756 events, 184s, exit=0
```

## Lifecycle States (MicroVM, verified)

```
run_microvm → PENDING ──(~2.3s)──▶ RUNNING ──(Agent loop + report)──▶ 仍 RUNNING
                                                                          │
                                              entrypoint finally:         ▼
                                              terminate_microvm → TERMINATING → TERMINATED
                                                                  (磁盘+内存销毁, 不可恢复)
```

- Idle policy 配置了 auto-suspend (maxIdleDurationSeconds=900)，但本工作流是单请求秒级完成，
  通常在 idle 触发前就 terminate。suspend/resume 主要服务长 idle 的交互式场景。
- maximumDurationInSeconds=1800（30 min）安全上限；8h 硬上限相关见 [DESIGN_V5.md](DESIGN_V5.md)。

## Key Difference vs V3 (side by side)

```
V3:  Runtime A ──invoke_agent_runtime(session)──▶ Runtime B (AgentCore)
                                                    ├ shell  → JSON
                                                    ├ python → JSON
                                                    └ report → SSE (Opus 在 B)
     同 session 自动路由到同 microVM；平台管生命周期。

V5:  Runtime A ──run_microvm──▶ Runtime B (Lambda MicroVM)
        │                         ├ shell  → JSON
        │ HTTPS + X-aws-proxy-auth ├ python → JSON
        │                         └ (无 report — 已移除)
        └─ report 在 Runtime A 自己渲染 (converse_stream Opus)
        └─ finally terminate_microvm；Runtime A 显式管生命周期。
```
