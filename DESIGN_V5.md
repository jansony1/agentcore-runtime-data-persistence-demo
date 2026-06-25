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

## Architecture — components

Five actors. Solid arrows = who calls whom; each is labelled with the protocol.

```
                          ┌───────────────┐
                          │   Frontend    │
                          └───────┬───────┘
                                  │  invoke_agent_runtime (SSE: status/chunk/done)
                                  ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Runtime A  —  AgentCore                                   │
   │  Agent A (Opus 4.6) = the only brain                       │
   │                                                            │
   │   • manages the MicroVM lifecycle (run / terminate)        │
   │   • Phase 1: drives Runtime B via 2 tools                  │
   │   • Phase 2: renders the report itself, uploads it         │
   └───┬───────────────┬──────────────────────┬────────────────┘
       │               │                      │
       │ control-plane │ data-plane           │ Bedrock
       │ boto3         │ HTTPS + token        │ converse_stream
       │ lambda-       │ runtime_b_shell      │ (Phase 2 report)
       │ microvms      │ runtime_b_python     │
       ▼               ▼                      ▼
 ┌───────────┐   ┌──────────────────────┐  ┌──────────────┐
 │ MicroVM   │   │ Runtime B            │  │  Bedrock     │
 │ control   │   │ Lambda MicroVM       │  │  (Opus 4.6)  │
 │ plane     │   │ pure executor        │  └──────────────┘
 │           │   │                      │
 │ RunMicrovm│   │ :8080  shell  → JSON │
 │ GetMicrovm│   │        python → JSON │
 │ AuthToken │   │ :9000  lifecycle     │        ┌──────────────┐
 │ Terminate │   │        hooks         │ ◀────▶ │  S3          │
 └───────────┘   │ /tmp/workspace (disk)│  aws   │ tenants/{id}/│
                 └──────────────────────┘  cli   │ datasets|... │
                                                 └──────────────┘
```

- **Runtime A → MicroVM control plane** (`boto3 lambda-microvms`): start/stop the VM.
- **Runtime A → Runtime B** (HTTPS + `X-aws-proxy-auth`): the two tools post `shell`/`python`.
- **Runtime A → Bedrock**: Phase 2 report (report no longer lives in Runtime B).
- **Runtime B ↔ S3** (`aws cli`): Runtime B downloads inputs / uploads chart+CSV outputs.
- **Runtime A → S3**: Runtime A uploads the final `analysis_report.md`.

---

## Data flow — sequence

One request, top to bottom. `A` = Runtime A, `B` = Runtime B (MicroVM).

```
Frontend          Runtime A                 MicroVM CP    Runtime B (MicroVM)      S3 / Bedrock
   │  invoke         │                          │              │                       │
   │ ───────────────▶│                          │              │                       │
   │                 │ ① run_microvm ──────────▶│              │                       │
   │                 │    poll until RUNNING ◀───│ (~2.4s)      │                       │
   │                 │    create auth token ────▶│              │                       │
   │ ◀── status ─────│   "MicroVM 就绪"          │              │                       │
   │                 │                                          │                       │
   │                 │ ② Phase 1: Agent A stream_async (Opus)   │                       │
   │ ◀── status ─────│   shell  ───────────────────────────────▶│ aws s3 cp datasets ─▶│ download
   │                 │                                          │   → /tmp/workspace    │
   │ ◀── status ─────│   python ───────────────────────────────▶│ pandas 读/算/出图     │
   │ ◀── status ─────│   python ───────────────────────────────▶│ → /tmp/workspace/out  │
   │ ◀── status ─────│   shell  ───────────────────────────────▶│ aws s3 cp output ───▶│ upload csv+png
   │ ◀── status ─────│   "数据准备完成"          (analysis_result 文本回到 A)            │
   │                 │                                          │                       │
   │                 │ ③ Phase 2: report rendered IN Runtime A  │                       │
   │ ◀── status ─────│   converse_stream(Opus, analysis_result) ───────────────────────▶│ Bedrock
   │ ◀── chunk ──────│   (报告逐 token 流式)                                             │
   │ ◀── chunk ──────│                                                                  │
   │                 │   put_object(analysis_report.md) ───────────────────────────────▶│ upload report
   │ ◀── done ───────│   s3_keys=[report.md]                    │                       │
   │                 │ ④ finally: terminate_microvm ───────────▶│ (VM 回收, 磁盘销毁)   │
```

Verified on AWS us-west-2: 10 status + 745 chunk + 1 done = 756 events, 184s, cold start 2.25s.

---

## Disk reuse & why outputs go to S3 via CLI (not a mount)

Runtime B has a real local disk (`/tmp/workspace`, up to 32 GB). Files persist
across `shell`/`python` calls **within the same MicroVM**:

```
shell : aws s3 cp s3://.../datasets/ /tmp/workspace/ --recursive   download → local disk
python: pd.read_csv("/tmp/workspace/...")                          read local disk
        df.to_csv("/tmp/workspace/output/x.csv")                   write local disk  (call 1)
python: plt.savefig("/tmp/workspace/output/x.png")                 write local disk  (call 2, same disk reused)
shell : aws s3 cp /tmp/workspace/output/ s3://.../reports/ -r      upload outputs → S3
```

**Why S3 outputs use `aws cli` / boto3 PUT, not a mounted filesystem:**
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
