# AgentCore + Lambda MicroVMs Architecture Design (V5)

## What changed from V3

V5 keeps the V3 philosophy — **大脑与手脚分离** (Agent A is the sole brain,
Runtime B is a pure executor) — but makes two structural changes:

1. **Runtime B moves from AgentCore Runtime to a Lambda MicroVM.**
   AgentCore auto-routed same-session calls to the same microVM. With MicroVMs,
   Runtime A manages the lifecycle explicitly per request:
   `run_microvm → poll RUNNING → create_microvm_auth_token → call endpoint → terminate_microvm`.

2. **Report generation moves from Runtime B into Runtime A.**
   In V3 Runtime B had a `report` action that called `converse_stream`. In V5
   Runtime A renders the report itself (it already had Bedrock access and the
   analysis text in hand). Runtime B is now a pure code executor: `shell` + `python` only.

| | V3 | V5 |
|---|---|---|
| Runtime A | AgentCore, Agent A brain | AgentCore, Agent A brain **+ MicroVM lifecycle + report** |
| Runtime B | AgentCore Runtime, shell/python/**report** | **Lambda MicroVM**, shell/python only |
| B invocation | `invoke_agent_runtime` (same session → same microVM) | HTTPS to MicroVM endpoint (`X-aws-proxy-auth`) |
| Report rendered by | Runtime B (`converse_stream`) | **Runtime A** (`converse_stream`) |
| B lifecycle | AgentCore-managed (opaque) | **Runtime A-managed** (run/terminate per request) |

---

## Architecture

```
┌─── Runtime A（AgentCore；Agent A = 唯一大脑）──────────────────────────┐
│                                                                        │
│  @app.entrypoint (async generator → SSE):                              │
│    1. run_microvm  → poll RUNNING → create_microvm_auth_token          │
│    2. Phase 1: Agent A (Opus) stream_async                             │
│         tools: runtime_b_shell / runtime_b_python                      │
│         → HTTPS POST to MicroVM endpoint (X-aws-proxy-auth)            │
│    3. Phase 2: Runtime A converse_stream(Opus) → report SSE chunks     │
│         → upload analysis_report.md to S3                              │
│    4. finally: terminate_microvm                                       │
│                                                                        │
└──────────┬─────────────────────────────────────────────────────────────┘
           │ HTTPS (per request: one MicroVM, started then terminated)
           ▼
┌─── Runtime B（Lambda MicroVM；纯执行器，无决策）─────────────────────┐
│                                                                        │
│  HTTP server :8080  →  POST {action: shell|python}  →  JSON response   │
│    shell:  subprocess.run(command)  (aws cli preinstalled)            │
│    python: exec(code)               (pandas/numpy/matplotlib)         │
│                                                                        │
│  Hook server :9000  →  /ready /run /resume /suspend /terminate         │
│    /ready : signal snapshot-ready during image build                  │
│    /run   : reset /tmp/workspace for a clean per-MicroVM slate         │
│                                                                        │
│  文件系统: /tmp/workspace/ (同一 MicroVM 内跨 shell/python 调用持久)   │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Data flow (no Mountpoint — local disk + S3 via CLI)

Runtime B has a real local disk (`/tmp/workspace`, up to 32 GB). Files persist
across `shell`/`python` calls **within the same MicroVM**, so the flow is:

```
shell:  aws s3 cp s3://bucket/tenants/.../datasets/ /tmp/workspace/ --recursive  ← download to local disk
python: pd.read_csv("/tmp/workspace/...")                                         ← read local disk
        df.to_csv("/tmp/workspace/output/x.csv")                                  ← write local disk (call 1)
python: plt.savefig("/tmp/workspace/output/x.png")                                ← write local disk (call 2, reuses disk)
shell:  aws s3 cp /tmp/workspace/output/ s3://bucket/.../reports/ --recursive    ← upload outputs to S3
```

**Why S3 access uses the AWS CLI / boto3 PUT, not a mounted filesystem:**
Mounting S3 as a filesystem (Mountpoint-for-S3, FUSE) is possible inside a MicroVM
(install `mount-s3` + `additionalOsCapabilities: ["ALL"]`), but its write semantics
are constrained — sequential whole-file writes only, **no append, no rename** on
general-purpose buckets. Tools like `pandas.to_csv` / `matplotlib.savefig` often
write-then-rename, which would fail on a mounted bucket. So we **write to local
disk** (full POSIX, no limits, reused across calls) and **PUT to S3 at the end**.
A FUSE mount would only help the read path; for this workload plain `aws s3 cp`
is simpler and avoids the write pitfalls.

---

## ⚠️ 8-hour lifecycle limit & roadmap

> This section records a real constraint and the planned fix. **V5 does not
> implement long-running (>8h) handling** — the current analysis workload is
> minutes-long. Documented here so the relay design is ready when needed.

### The limit (verified against AWS docs, 2026-06)

- A MicroVM's `maximumDurationInSeconds` caps **running + suspended combined** at
  **28,800s (8h)**. Suspending does **not** extend the clock — idle time still
  counts toward the 8h.
- On reaching the limit the MicroVM goes to `TERMINATED`, a **terminal state**:
  "cannot be resumed or restarted." Disk and memory are destroyed.
- **AgentCore Runtime has the identical 8h cap** (`maxLifetime` default 8h), so
  this is not a MicroVM-specific regression — it is inherent to both.

### How to continue past 8h (today): the relay pattern

You cannot extend one VM. You start a **new** VM and restore state from S3:

```
maximumDurationInSeconds = 7.5h         # leave a buffer, don't race the 8h wall
task checkpoints to S3 periodically / per logical step
/suspend + /terminate hooks → final flush to S3
orchestrator (Runtime A / Step Functions / control-plane Lambda):
  on ~7.5h timer OR detecting TERMINATED-while-incomplete
  → run_microvm (new VM, fresh 8h clock)
  → /run hook restores checkpoint from S3
  → resume work
= a chain of ≤8h VMs stitched together by S3 state
```

This handles **checkpointable** tasks. A task whose progress is **pure in-memory
and not serializable** (e.g. a 10h in-memory computation with no save points)
cannot be relayed by any serverless option today — that needs EC2/ECS/Batch, or
the roadmap item below.

### Roadmap (AWS, expected)

- **Snapshot-to-S3 + restore** for MicroVMs: snapshot the full VM (memory + disk)
  to S3 and restore it into a new VM with a fresh lease. This is the platform-native
  answer — it carries **memory state**, which app-level S3 checkpointing cannot.
  When available, it supersedes the relay pattern for the >8h case.

### Optional UX: ask-before-suspend (interactive sessions only)

For **interactive** sessions (a human at the screen) or **high-cost** runs, a
"session expiring in N minutes — persist & continue?" prompt is reasonable as a
cost circuit-breaker. For **autonomous batch** tasks it is an anti-pattern: the
decision (checkpoint if incomplete) should be automatic, and a silent default
must never drop work — default to **persist + terminate**, never "do nothing".

### Storage comparison (relevant to long tasks)

| | Lambda MicroVMs | AgentCore Runtime |
|---|---|---|
| In-VM disk | 32 GB, persists across suspend/resume | session disk, persists across stop/resume |
| Cross-VM / durable | **none managed — must write S3** | **managed session storage** (`/mnt/workspace`, survives stop/resume, 14-day idle expiry); can also mount EFS / S3 Files |
| Mountable volumes | DIY Mountpoint-for-S3 only (read-friendly) | managed S3 Files / EFS |

For >8h checkpointable work, AgentCore's managed session storage auto-reattaches
disk to the next compute — MicroVMs require DIY S3 checkpointing until snapshot-to-S3 ships.
Neither preserves **memory** state across the 8h boundary today.

---

## Deployment notes

- **boto3 ≥ 1.43.36** is required for the `lambda-microvms` / `lambda-core` clients.
- MicroVMs available regions (2026-06): us-east-1, us-east-2, us-west-2, eu-west-1, ap-northeast-1. ARM64 only.
- Base image (e.g. us-west-2): `arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1`
- Network bandwidth is tied to MicroVM size (2 GB/1 vCPU ≈ 4 MB/s). For large S3
  datasets, size the MicroVM up to avoid a bandwidth bottleneck on `aws s3 cp`.
- See `deploy_v5.sh` for image build + Runtime A deploy.
