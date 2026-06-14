# Hermes CockroachDB + Cluster mode

This branch adds an opt-in CockroachDB/Postgres storage layer and a pull-based
cluster runtime.

## Goals

- Durable state in CockroachDB/Postgres instead of local sqlite/jsonl for new
  cluster deployments.
- Core machine accepts sub-tasks and stores them in DB.
- Node machines only need Hermes + Python deps; they pull queued tasks, spawn a
  Hermes one-shot worker, then write result back to the core.
- Nodes do not own source state. The task carries `workspace_root`,
  `git_remote`, and `branch`. Use a shared filesystem, same checkout path, or a
  git remote workflow. The node only spawns work.
- Multiple clusters are isolated by `cluster_name`.

## Database config

`config.yaml`:

```yaml
database:
  backend: cockroach
  url: postgresql://root@cockroach-host:26257/hermes?sslmode=disable
  cluster_name: default
  profile: default
  pool_min: 1
  pool_max: 8
  statement_timeout_ms: 0
```

Env override:

```bash
export HERMES_DATABASE_URL='postgresql://root@cockroach-host:26257/hermes?sslmode=disable'
export HERMES_CLUSTER=default
```

Initialize schema:

```bash
hermes db init
hermes db status
```

Tables created:

- `hermes_sessions`
- `hermes_messages`
- `hermes_memory`
- `hermes_kv`
- `hermes_cluster_nodes`
- `hermes_cluster_tasks`
- `hermes_schema_version`

## Core setup

On the central machine:

```bash
hermes cluster init --name default --public-host <core-ip-or-domain> --port 8787 --print-join
hermes cluster core --host 0.0.0.0 --port 8787
```

The `init` command writes only local bootstrap config to
`~/.hermes/cluster/config.yaml`. Runtime state stays in CockroachDB.

## Node setup

On another PC/VM:

```bash
hermes cluster join http://<core-ip-or-domain>:8787 --token <token-from-init> --name default
hermes cluster node
```

The node backs off and refuses to claim new work when CPU, memory, or swap is
>= 95%.

## Submit work

```bash
hermes cluster submit 'Refactor module X and run tests' \
  --workspace-root /shared/workspace/hermes-agent \
  --toolsets terminal,file
```

List state:

```bash
hermes cluster nodes
hermes cluster tasks
```

## Architecture

- Core exposes HTTP:
  - `GET /health`
  - `POST /v1/nodes/register`
  - `POST /v1/nodes/{id}/heartbeat`
  - `GET /v1/nodes`
  - `POST /v1/tasks`
  - `POST /v1/tasks/claim`
  - `POST /v1/tasks/{id}/finish`
  - `GET /v1/tasks`
- Auth uses `X-Hermes-Cluster-Token`.
- Nodes pull work, so NAT/home PCs work if they can reach the core.
- DB queue provides multi-core/multi-node coordination.

## Current integration boundary

The new CockroachDB layer is implemented as a parallel durable backend
(`hermes_db`) plus new CLI/runtime (`hermes cluster`). Legacy local-only paths
still exist until every old SQLite call-site is migrated. For cluster mode,
new session/message/memory/task records go through CockroachDB.
