# Kaspr Ecosystem — Workspace Guide for LLMs

> **Purpose:** This document provides an LLM with comprehensive context about five interrelated projects so it can quickly reason about code, implement features, and make changes across the stack.

---

## Table of Contents

1. [Ecosystem Overview](#ecosystem-overview)
2. [Development Workflow](#development-workflow)
3. [Project 1: Faust (Stream Processing Framework)](#1-faust--stream-processing-framework)
4. [Project 2: Kaspr (YAML-Driven Stream Processing)](#2-kaspr--yaml-driven-stream-processing)
5. [Project 3: Kaspr-Operator (Kubernetes Operator)](#3-kaspr-operator--kubernetes-operator)
6. [Project 4: Kaspr-Helm (Helm Charts)](#4-kaspr-helm--helm-charts)
7. [Project 5: Kaspr-Docs (Documentation Website)](#5-kaspr-docs--documentation-website)
8. [Cross-Project Patterns & Conventions](#cross-project-patterns--conventions)
9. [Feature Implementation Checklist](#feature-implementation-checklist)

---

## Ecosystem Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                         USER / DEVELOPER                            │
│  Writes YAML CRDs (KasprAgent, KasprTable, KasprWebView, etc.)     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ kubectl apply
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  kaspr-operator (Python, Kopf)                                      │
│  Watches CRDs → creates ConfigMaps, StatefulSets, Services, HPAs    │
│  Deployed via kaspr-helm                                            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ mounts YAML definitions into pods
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  kaspr (Python library)                                             │
│  Reads YAML definitions at startup → builds Faust agents/tables     │
│  AppBuilder parses YAML → creates AgentSpec → calls app.agent()     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ extends / wraps
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  faust (Python, forked as twm-faust)                                │
│  Core stream processing: agents, streams, tables, topics, transport │
│  Kafka consumer/producer via aiokafka, RocksDB state stores         │
└──────────────────────────────────────────────────────────────────────┘
```

| Project | Language | Version | Location |
|---------|----------|---------|----------|
| **faust** (twm-faust) | Python | 1.17.10 | `faust/` |
| **kaspr** | Python | 0.8.2 | `kaspr/` |
| **kaspr-operator** | Python | 0.16.2 | `kaspr-operator/` (TotalWineLabs) |
| **kaspr-helm** | Helm/YAML | Chart 0.2.20 / App 0.5.15 | `kaspr-helm/` |
| **kaspr-docs** | TypeScript/MDX | — | `kaspr-docs/` |

### Dependency Chain

```
faust ← kaspr ← kaspr-operator ← kaspr-helm
                                ← kaspr-docs
```

- **kaspr** depends on `twm-faust[rocksdict,prometheus]>=1.17.10,<1.18.0`
- **kaspr-operator** depends on `kopf`, `kubernetes-asyncio`, `marshmallow`, `mmh3`, `prometheus-client`
- **kaspr-docs** uses `nextra` (Next.js documentation framework)
- **kaspr-helm** deploys the operator image and CRDs

---

## Development Workflow

The typical flow when adding a new feature:

1. **Faust** — Implement the core stream processing capability (e.g., new table type, new stream operation, new transport feature). Bump `__version__` in `faust/__init__.py`.
2. **Kaspr** — Add YAML configuration model + schema + builder support for the new feature. This means adding types/models, types/schemas, and wiring it through the `AppBuilder`. Bump `__version__` in `kaspr/__init__.py`.
3. **Kaspr-Operator** — Update CRDs (OpenAPI schema in `crds/*.crd.yaml`), add corresponding types/models and types/schemas for the operator side, update resource classes and handlers. Bump `__version__` in `kaspr/__init__.py` of the operator.
4. **Kaspr-Helm** — Copy updated CRDs to `charts/operator/crds/`, update `Chart.yaml` appVersion, bump chart version.
5. **Kaspr-Docs** — Update user guides (`pages/docs/user-guide/`), API reference (`pages/docs/api-reference/v1alpha1.mdx`), and examples.

---

## 1. Faust — Stream Processing Framework

**Path:** `/faust/`  
**Package:** `twm-faust` (forked from Robinhood's faust-streaming)  
**Python:** ≥ 3.10  
**Key Dependencies:** `aiokafka`, `mode-streaming`, `aiohttp`, `rocksdict` (optional), `croniter`

### Architecture & Key Concepts

Faust is built around these core abstractions:

| Concept | Module | Description |
|---------|--------|-------------|
| **App** | `faust/app/base.py` | Central application object. Everything is registered here (agents, topics, tables, web views). 1977 lines. |
| **Agent** | `faust/agents/agent.py` | An async function that processes a stream of events. Agents are async generators. 1141 lines. |
| **Stream** | `faust/streams.py` | Async iterator over events from a channel. Supports `group_by`, `filter`, `take`, etc. 996 lines. |
| **Topic** | `faust/topics.py` | A named channel backed by a Kafka topic. Handles serialization/deserialization. 501 lines. |
| **Channel** | `faust/channels.py` | In-memory message channel (topic is a subclass). |
| **Table** | `faust/tables/table.py` | Key/value store backed by a Kafka changelog topic. Supports windowing (hopping, tumbling). |
| **GlobalTable** | `faust/tables/globaltable.py` | A table replicated to every worker instance. |
| **Store** | `faust/stores/` | Pluggable state backends: `memory`, `rocksdb`. |
| **Transport** | `faust/transport/drivers/` | Kafka drivers: `aiokafka` (default), `confluent`. |
| **Sensor** | `faust/sensors/` | Monitoring hooks (datadog, statsd, custom). |
| **Web** | `faust/web/` | Built-in aiohttp web server for views/endpoints. |
| **Model** | `faust/models/` | Serializable record types (like dataclasses for events). |
| **Window** | `faust/windows.py` | Time-based windowing (tumbling, hopping). |

### Directory Structure

```
faust/
├── __init__.py          # Version: 1.17.10
├── app/
│   ├── base.py          # App class — THE central object (1977 lines)
│   └── router.py        # App router for web
├── agents/
│   ├── agent.py         # Agent implementation (1141 lines)
│   ├── actor.py         # AsyncIterableActor, AwaitableActor
│   ├── manager.py       # AgentManager
│   ├── models.py        # ReqRep models
│   └── replies.py       # Reply/barrier state
├── channels.py          # Channel (in-memory), SerializedChannel
├── streams.py           # Stream class (996 lines)
├── topics.py            # Topic class (501 lines)
├── tables/
│   ├── base.py          # Collection base class (706 lines)
│   ├── table.py         # Table (non-windowed) (124 lines)
│   ├── globaltable.py   # GlobalTable
│   ├── wrappers.py      # WindowWrapper
│   ├── manager.py       # TableManager
│   └── recovery.py      # Table recovery from changelogs
├── stores/
│   ├── base.py          # Base store interface
│   ├── memory.py        # In-memory store
│   └── rocksdb.py       # RocksDB store (803 lines)
├── transport/
│   ├── base.py          # Base transport
│   ├── consumer.py      # Consumer abstraction
│   ├── producer.py      # Producer abstraction
│   ├── conductor.py     # Topic conductor
│   └── drivers/
│       ├── aiokafka.py  # Default Kafka driver
│       └── confluent.py # Confluent driver
├── sensors/
│   ├── base.py          # SensorDelegate
│   ├── monitor.py       # Monitor
│   ├── datadog.py       # Datadog integration
│   └── statsd.py        # StatsD integration
├── models/              # Serializable record types
├── web/                 # Built-in web server
├── types/               # Type interfaces (abstract base classes)
│   ├── agents.py, app.py, streams.py, tables.py, topics.py, ...
│   └── settings.py      # Settings type
├── utils/               # Utilities (cron, venusian, tracing)
├── _cython/             # Optional Cython acceleration
├── events.py            # Event wrapper
├── exceptions.py        # Exception types
├── joins.py             # Join implementations
└── windows.py           # Window types (HoppingWindow, TumblingWindow)
```

### Key Patterns

- **Type interfaces in `faust/types/`**: Every major class has an abstract interface (e.g., `AgentT`, `StreamT`, `TableT`). Implementations are in the corresponding modules.
- **Plugin system via `App` constructor**: Custom implementations are injected via string paths: `Agent="kaspr.core.agent.KasprAgent"`, `Table="kaspr.core.table.KasprTable"`, etc.
- **Service lifecycle via `mode`**: All services extend `mode.Service` with `on_start()`, `on_stop()`, `on_started()` lifecycle hooks.
- **Changelog-backed tables**: Every table has a Kafka changelog topic. State is recovered on rebalance by replaying the changelog.
- **Processing guarantee**: Supports `exactly_once` via Kafka transactions.

### How to Add a New Feature to Faust

1. Define the abstract type interface in `faust/types/`.
2. Implement the concrete class in the appropriate module.
3. Register it with the `App` class (possibly as a pluggable class).
4. Add tests in `t/` directory.
5. Bump version in `faust/__init__.py`.

---

## 2. Kaspr — YAML-Driven Stream Processing

**Path:** `/kaspr/`  
**Package:** `kaspr`  
**Python:** ≥ 3.10  
**Key Dependencies:** `twm-faust[rocksdict,prometheus]>=1.17.10`, `marshmallow`, `python-benedict`, `pyyaml`

### What Kaspr Does

Kaspr sits on top of Faust and provides:
1. **YAML-based configuration** — Stream processors are defined in YAML files instead of Python code.
2. **AppBuilder** — Reads YAML definitions and dynamically creates Faust agents, tables, web views, and tasks.
3. **PyCode execution** — Embedded Python code blocks in YAML are compiled and executed at runtime.
4. **Message Scheduler** — A built-in Kafka-based message scheduling system (delayed delivery).
5. **Custom extensions** — `KasprAgent`, `KasprTable`, `KasprStream` extend Faust's base classes with additional capabilities.

### Architecture

```
YAML Definition Files
        │
        ▼
  AppBuilder (kaspr/core/builder.py)
        │
        ├── Loads YAML → AppSpecSchema (marshmallow) → AppSpec model
        │
        ├── AppSpec.agents_spec → AgentSpec.prepare_agent() → app.agent()
        ├── AppSpec.tables_spec → TableSpec → app.Table()
        ├── AppSpec.webviews_spec → WebViewSpec → web views
        └── AppSpec.tasks_spec → TaskSpec → app.timer() / app.crontab()
```

### Directory Structure

```
kaspr/
├── __init__.py              # Version: 0.8.2, exports KasprApp
├── __main__.py              # Entry point
├── app.py                   # App instance creation (configures KasprApp)
├── agents.py                # Example FK join agent code
├── exceptions.py            # Custom exceptions
├── core/
│   ├── app.py               # KasprApp(faust.App) — main app class (121 lines)
│   ├── agent.py             # KasprAgent(faust.Agent) — custom agent
│   ├── stream.py            # KasprStream(faust.Stream) — adds take_events()
│   ├── table.py             # KasprTable(faust.Table) — partition-aware ops
│   ├── builder.py           # AppBuilder — YAML → Faust objects (builds everything)
│   ├── leader_assignor.py   # Custom leader assignor
│   └── partition_assignor.py# Custom partition assignor
├── types/
│   ├── __init__.py          # Re-exports all types
│   ├── app.py               # KasprAppT (abstract)
│   ├── agent.py             # KasprAgentT (abstract)
│   ├── stream.py            # KasprStreamT (abstract, adds take_events, filter)
│   ├── table.py             # KasprTableT, KasprGlobalTableT (abstract)
│   ├── webview.py           # KasprWebViewT
│   ├── code.py              # CodeT (abstract code execution interface)
│   ├── operation.py         # ProcessorOperatorT (abstract)
│   ├── builder.py           # AppBuilderT (abstract)
│   ├── settings.py          # CustomSettings — env-var-driven config (716 lines)
│   ├── topic.py             # KasprTopicT
│   ├── dispatcher.py        # DispatcherT (scheduler)
│   ├── message_scheduler.py # MessageSchedulerT (scheduler)
│   ├── checkpoint.py        # CheckpointT (scheduler)
│   ├── janitor.py           # JanitorT (scheduler)
│   ├── tuples.py            # Named tuples
│   ├── models/              # Concrete data models
│   │   ├── base.py          # BaseModel(SimpleNamespace), SpecComponent
│   │   ├── app.py           # AppSpec — top-level YAML model
│   │   ├── pycode.py        # PyCode — compiles & executes Python from YAML
│   │   ├── channel.py       # ChannelSpec
│   │   ├── topicsrc.py      # TopicSourceSpec (input topic)
│   │   ├── topicout.py      # TopicOutSpec (output topic)
│   │   ├── topicselector.py # TopicSelectorSpec
│   │   ├── tableref.py      # TableRefSpec (table references in operations)
│   │   ├── agent/
│   │   │   ├── agent.py     # AgentSpec — represents a YAML-defined agent
│   │   │   ├── input.py     # AgentInputSpec
│   │   │   ├── output.py    # AgentOutputSpec
│   │   │   ├── processor.py # AgentProcessorSpec — THE processor pipeline (176 lines)
│   │   │   └── operations.py# AgentProcessorOperation, Filter/Map operators
│   │   ├── table/           # TableSpec
│   │   ├── webview/         # WebViewSpec
│   │   └── task/            # TaskSpec
│   └── schemas/             # Marshmallow schemas (parallel to models/)
│       ├── base.py          # BaseSchema with post_load → __model__
│       ├── app.py           # AppSpecSchema
│       ├── pycode.py        # PyCodeSchema
│       ├── agent/
│       │   ├── agent.py     # AgentSpecSchema
│       │   ├── input.py     # AgentInputSpecSchema
│       │   ├── output.py    # AgentOutputSpecSchema
│       │   ├── processor.py # AgentProcessorSpecSchema
│       │   └── operations.py# OperationSchema
│       ├── table/           # TableSpecSchema
│       ├── webview/         # WebViewSpecSchema
│       └── task/            # TaskSpecSchema
├── scheduler/
│   ├── manager.py           # MessageScheduler service (657 lines)
│   ├── dispatcher.py        # Dispatcher (per-partition message delivery)
│   ├── checkpoint.py        # Checkpoint (tracks scheduler position)
│   ├── janitor.py           # Janitor (cleanup old scheduled messages)
│   └── utils.py             # Scheduler utilities
├── sensors/
│   ├── kaspr.py             # KasprMonitor (custom sensor)
│   └── prometheus_monitor.py# Prometheus metrics
├── blueprints/
│   ├── signal.py            # /signal endpoint (rebalance triggers)
│   └── status.py            # /status endpoint
└── utils/
    ├── functional.py        # Utility functions (utc_now, maybe_async, etc.)
    └── logging.py           # CompositeLogger
```

### Key Patterns

#### Model-Schema Pattern (Marshmallow)
Every YAML structure is represented by a pair:
- **Model** in `types/models/` — A `BaseModel(SimpleNamespace)` or `SpecComponent` class that holds the parsed data.
- **Schema** in `types/schemas/` — A `marshmallow.Schema` subclass that validates and deserializes YAML/JSON into the model. Uses `__model__` class attribute and `@post_load` to instantiate models.

```python
# Schema (types/schemas/agent/agent.py)
class AgentSpecSchema(BaseSchema):
    __model__ = AgentSpec
    name = fields.Str(data_key="name", required=True)
    input = fields.Nested(AgentInputSpecSchema(), data_key="input", required=True)
    processors = fields.Nested(AgentProcessorSpecSchema(), data_key="processors", required=True)

# Model (types/models/agent/agent.py)  
class AgentSpec(SpecComponent):
    name: str
    input: AgentInputSpec
    processors: AgentProcessorSpec
    def prepare_agent(self) -> KasprAgentT:
        return self.app.agent(self.input.channel, name=self.name)(self.processors.processor)
```

#### PyCode Execution Model
Python code embedded in YAML is handled by `PyCode`:
1. YAML `python:` field contains source code as a string.
2. `PyCode.compiled_python` compiles it via `compile()`.
3. `PyCode.with_scope(scope)` sets execution scope (includes `context`, tables, etc.).
4. `PyCode.execute()` runs `exec()` and extracts the callable function.
5. The callable is invoked during stream processing with the event value.

#### Processor Pipeline
The processor pipeline (`AgentProcessorSpec.prepare_processor()`) is the heart of event processing:
1. `init` block runs once to set up shared state (HTTP sessions, caches, config).
2. `pipeline` defines the ordered list of operation names.
3. Each operation has a `map` or `filter` with Python code.
4. Operations can reference tables via `table_refs`.
5. Values flow through the pipeline sequentially; `filter` can skip events.
6. Final values are sent to `output.topics`.

#### Settings (Environment Variables)
`kaspr/types/settings.py` reads configuration from environment variables with `KASPR_` or `K_` prefix. Every Kafka, RocksDB, and application setting is configurable via env vars.

### How to Add a New Feature to Kaspr

1. If the feature requires a new Faust capability, implement it in Faust first.
2. Define the **model** in `types/models/` (e.g., `types/models/agent/new_feature.py`).
3. Define the **schema** in `types/schemas/` (parallel structure).
4. Wire the model into the relevant parent model/schema (e.g., `AgentSpec`, `AgentSpecSchema`).
5. Implement the runtime behavior (how the model is used to create Faust objects).
6. Add env-var settings in `types/settings.py` if needed.
7. Bump version in `__init__.py`.

---

## 3. Kaspr-Operator — Kubernetes Operator

**Path:** `/kaspr-operator/` (TotalWineLabs)  
**Package:** `kaspr-operator`  
**Python:** ≥ 3.10  
**Key Dependencies:** `kopf==1.37.1`, `kubernetes-asyncio==33.3.0`, `marshmallow==3.21.1`, `mmh3`, `prometheus-client`

### What the Operator Does

The kaspr-operator is a **Kubernetes operator** built with [Kopf](https://kopf.readthedocs.io/) that watches for Kaspr CRDs and manages the lifecycle of stream processing applications:

1. **Watches** `KasprApp`, `KasprAgent`, `KasprTable`, `KasprWebView`, `KasprTask` custom resources.
2. **Creates/updates** Kubernetes resources: StatefulSets, Services, ConfigMaps, HPAs, PVCs, ServiceAccounts.
3. **Manages** scaling, rebalancing, health monitoring, and recovery.
4. **Translates** CRD specs into ConfigMaps containing YAML definitions that the kaspr library reads at startup.

### Custom Resource Definitions (CRDs)

| CRD | Short Names | File | Purpose |
|-----|-------------|------|---------|
| `KasprApp` | `kapps`, `kapp` | `crds/kasprapp.crd.yaml` (1284 lines) | The deployment unit — StatefulSet + Service + ConfigMap + HPA |
| `KasprAgent` | `kagents`, `kagent` | `crds/kaspragent.crd.yaml` (270 lines) | Stream processing agent definition |
| `KasprTable` | `ktables`, `ktable` | `crds/kasprtable.crd.yaml` (112 lines) | Stateful key/value table definition |
| `KasprWebView` | `kwebviews`, `kwebview` | `crds/kasprwebview.crd.yaml` (320 lines) | HTTP endpoint definition |
| `KasprTask` | `ktasks`, `ktask` | `crds/kasprtask.crd.yaml` (217 lines) | Scheduled/periodic task definition |

All CRDs use API group `kaspr.io` version `v1alpha1`.

### Architecture

```
Kopf Event Loop
├── kaspr/app.py                    # Startup/cleanup, shared API client
├── kaspr/handlers/
│   ├── kasprapp.py                 # KasprApp handler (2342 lines) — THE main handler
│   │   ├── @kopf.on.create/update/resume → reconciliation()
│   │   ├── @kopf.daemon → reconciliation_loop()
│   │   ├── @kopf.timer → periodic health checks, status updates
│   │   └── Helper functions for scaling, rebalancing, etc.
│   ├── kaspragent.py               # KasprAgent handler (201 lines)
│   ├── kasprwebview.py             # KasprWebView handler (202 lines)
│   ├── kasprtable.py               # KasprTable handler (202 lines)
│   ├── kasprtask.py                # KasprTask handler
│   └── probes.py                   # Health probe handlers
├── kaspr/resources/
│   ├── base.py                     # BaseResource — k8s resource CRUD (572 lines)
│   ├── kasprapp.py                 # KasprApp resource (3238 lines) — generates all k8s manifests
│   ├── appcomponent.py             # BaseAppComponent — base for agent/table/webview resources
│   ├── kaspragent.py               # KasprAgent resource
│   ├── kasprwebview.py             # KasprWebView resource
│   ├── kasprtable.py               # KasprTable resource
│   └── kasprtask.py                # KasprTask resource
├── kaspr/types/
│   ├── settings.py                 # Operator settings (env vars)
│   ├── base.py                     # BaseModel
│   ├── models/                     # CRD spec models (operator-side)
│   │   ├── kasprapp_spec.py        # KasprAppSpec
│   │   ├── kaspragent_spec.py      # KasprAgentSpec (operator-side, different from kaspr's)
│   │   ├── kasprtable_spec.py      # KasprTableSpec
│   │   ├── kasprwebview_spec.py    # KasprWebViewSpec
│   │   ├── kasprtask_spec.py       # KasprTaskSpec
│   │   ├── component.py            # KasprAppComponents (aggregates all component specs)
│   │   ├── operation.py            # MapOperation, FilterOperation
│   │   ├── code.py                 # CodeSpec
│   │   ├── topicout.py             # TopicOutSpec
│   │   ├── tableref.py             # TableRefSpec
│   │   ├── authentication.py       # SASL/TLS models
│   │   ├── storage.py              # Storage (PVC) models
│   │   ├── config.py               # KasprAppConfig
│   │   ├── python_packages.py      # PythonPackagesSpec (pip install support)
│   │   ├── resource_requirements.py# CPU/memory requests/limits
│   │   ├── probe.py                # Liveness/readiness probes
│   │   └── ...                     # Templates, pod specs, etc.
│   └── schemas/                    # Marshmallow schemas for CRD deserialization
│       ├── kasprapp_spec.py
│       ├── kaspragent_spec.py
│       ├── kasprtable_spec.py
│       ├── kasprwebview_spec.py
│       ├── kasprtask_spec.py
│       └── ...
├── kaspr/web/
│   ├── client.py                   # KasprWebClient — calls /status, /signal/rebalance on pods
│   └── session.py                  # HTTP session manager
├── kaspr/sensors/
│   ├── base.py                     # OperatorSensor — lifecycle hooks (404 lines)
│   ├── delegate.py                 # SensorDelegate (fan-out to multiple sensors)
│   ├── prometheus.py               # PrometheusMonitor
│   └── server.py                   # Metrics HTTP server
├── kaspr/common/models/
│   └── labels.py, version.py       # K8s label helpers
└── kaspr/utils/
    ├── helpers.py                  # upsert_condition, deep_compare_dict, now
    ├── errors.py                   # API exception conversion
    ├── objects.py                  # cached_property
    ├── override.py                 # Kopf monkey-patch for kubernetes_asyncio
    ├── python_packages.py          # Package hash computation
    └── gcs.py                      # GCS cache for Python packages
```

### Key Patterns

#### Handler Pattern (Kopf)
Each CRD has a handler module that registers Kopf decorators:
```python
@kopf.on.resume(kind=KIND)
@kopf.on.create(kind=KIND)  
@kopf.on.update(kind=KIND)
async def reconciliation(body, spec, name, namespace, logger, labels, patch, **kwargs):
    spec_model = SpecSchema().load(spec)        # Deserialize CRD spec
    resource = Resource.from_spec(name, ...)     # Create resource object
    await resource.create()                       # Sync k8s resources (ConfigMap, etc.)
    patch.status.update({...})                    # Update CRD status
```

#### Resource Model Pattern
- `BaseResource` provides CRUD methods for k8s resources (service, configmap, statefulset, etc.).
- `BaseAppComponent` (extends `BaseResource`) handles CRD components (agents, tables, webviews, tasks). It creates a ConfigMap containing the component's YAML spec.
- `KasprApp` (extends `BaseResource`) is the main orchestrator. It generates StatefulSets, Services, HPAs, ConfigMaps, PVCs, and manages the full lifecycle.

#### Operator → Kaspr Communication
The operator does NOT run Faust/Kaspr directly. Instead:
1. Agent/Table/WebView/Task CRDs are serialized into ConfigMaps.
2. The KasprApp StatefulSet mounts these ConfigMaps as YAML files.
3. The kaspr library's `AppBuilder` reads these files at startup.
4. The operator communicates with running kaspr pods via HTTP (`/status`, `/signal/rebalance`).

#### Reconciliation Flow (KasprApp)
1. CRD create/update triggers `reconciliation()` in `handlers/kasprapp.py`.
2. Fetches all related resources (agents, tables, webviews, tasks) in parallel.
3. Computes hashes for change detection.
4. Creates/patches: ServiceAccount, ConfigMap, PVC, Service, HeadlessService, StatefulSet, HPA.
5. Monitors member health via daemon loops.
6. Handles scaling (gradual scale-up via HPA policies).

#### Two-Level Model/Schema System
**Important:** The operator has its OWN model/schema system separate from kaspr's:
- **Operator models** (`kaspr-operator/kaspr/types/models/`) — Represent CRD specs for k8s resource generation.
- **Kaspr models** (`kaspr/kaspr/types/models/`) — Represent YAML definitions for Faust object creation.

They mirror each other in structure but serve different purposes. The operator models focus on k8s resource fields (replicas, storage, probes), while kaspr models focus on stream processing logic (input, output, processors, pipeline).

### How to Add a New CRD/Feature to the Operator

1. Define the CRD YAML in `crds/new_resource.crd.yaml`.
2. Create models in `types/models/new_resource_spec.py`.
3. Create schemas in `types/schemas/new_resource_spec.py`.
4. Create resource class in `resources/new_resource.py` (extends `BaseAppComponent`).
5. Create handler in `handlers/new_resource.py` (register Kopf decorators).
6. Import handler in `kaspr/__init__.py` (or `kaspr/app.py`).
7. Update `KasprAppComponents` if it's a child of KasprApp.
8. Add tests in `tests/unit/`.
9. Bump version in `__init__.py`.

---

## 4. Kaspr-Helm — Helm Charts

**Path:** `/kaspr-helm/`  
**Chart Version:** 0.2.20  
**App Version:** 0.5.15

### Structure

```
kaspr-helm/
├── charts/
│   ├── operator/                    # Main chart: deploys the operator itself
│   │   ├── Chart.yaml               # Chart metadata (version: 0.2.20, appVersion: 0.5.15)
│   │   ├── values.yaml              # Default values (image, RBAC, resources, etc.)
│   │   ├── crds/                    # CRD definitions (MUST match kaspr-operator/crds/)
│   │   │   ├── kasprapp.crd.yaml
│   │   │   ├── kaspragent.crd.yaml
│   │   │   ├── kasprtable.crd.yaml
│   │   │   └── kasprwebview.crd.yaml
│   │   └── templates/
│   │       ├── _helpers.tpl         # Template helpers (name, labels, etc.)
│   │       ├── 000-serviceaccount.yaml
│   │       ├── 010-clusterrole.yaml
│   │       ├── 010-clusterrolebinding.yaml
│   │       ├── 015-clusterrole.yaml # Secondary role (namespace-scoped)
│   │       ├── 015-clusterrolebinding.yaml
│   │       ├── 020-service.yaml
│   │       ├── 030-deployment.yaml  # Operator deployment
│   │       └── NOTES.txt
│   └── resources/                   # Helper chart: deploys Kaspr custom resources
│       ├── Chart.yaml
│       ├── values.yaml              # Example KasprApp + agents + tables definitions
│       └── templates/
│           ├── _helpers.tpl
│           ├── apps.yaml            # Renders KasprApp CRs from values
│           ├── agents.yaml          # Renders KasprAgent CRs from values
│           ├── tables.yaml          # Renders KasprTable CRs from values
│           ├── tasks.yaml           # Renders KasprTask CRs from values
│           ├── webviews.yaml        # Renders KasprWebView CRs from values
│           └── NOTES.txt
```

### Key Values (operator chart)

| Key | Default | Description |
|-----|---------|-------------|
| `operator.enable` | `True` | Deploy the operator |
| `operator.watchAnyNamespace` | `True` | Cluster-wide or namespace-scoped |
| `operator.watchNamespaces` | `[]` | Specific namespaces to watch |
| `operator.image.repository` | `kasprio/kaspr-operator` | Operator container image |
| `operator.image.tag` | `""` (uses appVersion) | Image tag |
| `operator.rbac.create` | `yes` | Auto-create RBAC |

### How to Update the Helm Chart

1. Copy updated CRDs from `kaspr-operator/crds/` to `charts/operator/crds/`.
2. Update `charts/operator/Chart.yaml`: bump `version` and `appVersion`.
3. Update templates if new k8s resources are needed.
4. Update `charts/resources/` templates if new CRD types are added.
5. Test with `helm template` and `helm install --dry-run`.

---

## 5. Kaspr-Docs — Documentation Website

**Path:** `/kaspr-docs/`  
**Framework:** [Nextra](https://nextra.site/) v3 (Next.js + MDX documentation theme)  
**Node.js dependencies:** `next ^13.5.7`, `nextra ^3.2.3`, `nextra-theme-docs ^3.2.3`, `react ^18.3.1`, `framer-motion`, `tailwindcss`

### Structure

```
kaspr-docs/
├── package.json                    # Scripts: dev, build, start
├── next.config.js                  # Next.js config
├── theme.config.jsx                # Nextra theme config (logo, links, etc.)
├── tailwind.config.ts              # Tailwind CSS config
├── pages/
│   ├── _app.tsx                    # App wrapper
│   ├── _meta.js                    # Top-level navigation
│   ├── index.mdx                   # Landing page
│   └── docs/
│       ├── _meta.js                # Docs section navigation
│       ├── index.mdx               # Docs overview
│       ├── copyright.mdx
│       ├── getting-started/
│       │   ├── _meta.js
│       │   ├── introduction.mdx
│       │   ├── architecture.mdx
│       │   ├── installation.mdx
│       │   └── installation/       # Sub-pages
│       ├── user-guide/
│       │   ├── _meta.js
│       │   ├── concepts.mdx        # Core concepts
│       │   ├── kafka-basics.mdx    # Kafka fundamentals
│       │   ├── agents.mdx          # KasprAgent guide (328 lines)
│       │   ├── patterns.mdx        # Common patterns
│       │   └── scheduler.mdx       # Message scheduler
│       ├── api-reference/
│       │   ├── _meta.js
│       │   └── v1alpha1.mdx        # CRD API reference
│       └── examples/               # (currently empty)
├── components/
│   ├── icons/                      # Logo/icons
│   ├── features/                   # Landing page features
│   ├── index-page/                 # Landing page components
│   └── table/                      # Table components
├── public/assets/                  # Static assets
└── utils/config.js                 # Configuration utilities
```

### Navigation (`_meta.js` pattern)
Nextra uses `_meta.js` files to control sidebar navigation order and titles. Each directory can have one to define page order.

### How to Update Documentation

1. Add/edit `.mdx` files in the appropriate `pages/docs/` subdirectory.
2. Update `_meta.js` to include new pages in navigation.
3. Use Nextra components: `<Callout>`, `<Tabs>`, `<Steps>`, code blocks with syntax highlighting.
4. For API reference updates, edit `pages/docs/api-reference/v1alpha1.mdx`.
5. Run `pnpm dev` to preview locally.

---

## Cross-Project Patterns & Conventions

### Naming Conventions

| Concept | Faust | Kaspr | Operator CRD | Helm |
|---------|-------|-------|--------------|------|
| Stream processor | `Agent` | `AgentSpec` | `KasprAgent` | `agents.yaml` |
| State store | `Table` | `TableSpec` | `KasprTable` | `tables.yaml` |
| HTTP endpoint | `View` | `WebViewSpec` | `KasprWebView` | `webviews.yaml` |
| Scheduled work | `crontab()`/`timer()` | `TaskSpec` | `KasprTask` | `tasks.yaml` |
| Application | `App` | `KasprApp`/`AppSpec` | `KasprApp` | `apps.yaml` |

### Shared Patterns

1. **Marshmallow Model/Schema Pattern** — Both kaspr and kaspr-operator use marshmallow for deserialization. The pattern is identical: `BaseSchema.__model__ = SomeModel`, `@post_load` creates the model.

2. **Environment Variables** — Both kaspr (`KASPR_`/`K_` prefix) and kaspr-operator use env vars for configuration. The operator's env vars are simpler (no prefix).

3. **Sensor/Monitor Pattern** — Faust has `SensorDelegate`, kaspr wraps it as `KasprMonitor` + `PrometheusMonitor`, and the operator has its own `OperatorSensor` + `SensorDelegate` + `PrometheusMonitor` (separate implementation, same pattern).

4. **Abstract Type → Concrete Implementation** — Faust defines `*T` types in `faust/types/`, kaspr defines `Kaspr*T` types in `kaspr/types/`, operator uses `BaseModel` in `kaspr/types/models/`.

5. **Python Code in YAML** — The `CodeSpec`/`PyCode` pattern appears in both kaspr and operator:
   - YAML field: `python:` (source code string) + optional `entrypoint:` (function name)
   - In kaspr: `PyCode.execute()` compiles and runs the code
   - In operator: `CodeSpec` is just a data model; execution happens in the kaspr runtime

### Version Coupling

| When you bump... | Also check/bump... |
|-------------------|-------------------|
| `faust/__init__.py` (`__version__`) | kaspr `requirements.txt` (faust version pin) |
| `kaspr/__init__.py` (`__version__`) | kaspr-operator Dockerfile / image tag |
| `kaspr-operator/__init__.py` (`__version__`) | kaspr-helm `Chart.yaml` (`appVersion`), operator Docker image |
| CRDs in `kaspr-operator/crds/` | kaspr-helm `charts/operator/crds/` (copy files) |
| kaspr-helm `Chart.yaml` | version + appVersion |

---

## Feature Implementation Checklist

When implementing a new end-to-end feature (e.g., a new agent capability):

### Faust Layer
- [ ] Define abstract type in `faust/types/` if new interface needed
- [ ] Implement in the appropriate module (`faust/agents/`, `faust/tables/`, etc.)
- [ ] Add to `App` class if it needs to be pluggable
- [ ] Add tests in `t/`
- [ ] Bump `faust/__init__.py` version

### Kaspr Layer
- [ ] Create model in `kaspr/types/models/` (data class)
- [ ] Create schema in `kaspr/types/schemas/` (marshmallow)
- [ ] Wire into parent model/schema (e.g., `AgentSpec` → `AgentSpecSchema`)
- [ ] Implement runtime behavior (how model creates Faust objects)
- [ ] Add settings in `kaspr/types/settings.py` if configurable
- [ ] Update `AppBuilder` if it affects build process
- [ ] Bump `kaspr/__init__.py` version

### Operator Layer
- [ ] Update CRD in `crds/*.crd.yaml` (OpenAPI schema)
- [ ] Create/update model in `kaspr-operator/kaspr/types/models/`
- [ ] Create/update schema in `kaspr-operator/kaspr/types/schemas/`
- [ ] Update resource class in `kaspr-operator/kaspr/resources/`
- [ ] Update handler in `kaspr-operator/kaspr/handlers/`
- [ ] Update `KasprAppComponents` if applicable
- [ ] Add operator sensor hooks if metrics needed
- [ ] Add tests in `tests/unit/`
- [ ] Bump `kaspr-operator/__init__.py` version

### Helm Layer
- [ ] Copy updated CRDs to `kaspr-helm/charts/operator/crds/`
- [ ] Update `charts/resources/templates/` if new resource types
- [ ] Update `charts/resources/values.yaml` with examples
- [ ] Bump `Chart.yaml` version + appVersion

### Documentation Layer
- [ ] Update relevant user guide in `pages/docs/user-guide/`
- [ ] Update API reference in `pages/docs/api-reference/v1alpha1.mdx`
- [ ] Add examples if applicable
- [ ] Update `_meta.js` navigation if adding new pages

---

## Quick Reference: File Sizes (Complexity Indicators)

| File | Lines | Notes |
|------|-------|-------|
| `faust/app/base.py` | 1,977 | Core app — most complex file in faust |
| `faust/agents/agent.py` | 1,141 | Agent implementation |
| `faust/streams.py` | 996 | Stream processing |
| `faust/stores/rocksdb.py` | 803 | RocksDB store |
| `faust/tables/base.py` | 706 | Table base class |
| `kaspr/types/settings.py` | 716 | All configurable settings |
| `kaspr/scheduler/manager.py` | 657 | Message scheduler |
| `kaspr-operator/handlers/kasprapp.py` | 2,342 | Main reconciliation handler |
| `kaspr-operator/resources/kasprapp.py` | 3,238 | K8s resource generation |
| `kaspr-operator/crds/kasprapp.crd.yaml` | 1,284 | KasprApp CRD schema |
| `kaspr-operator/sensors/base.py` | 404 | Operator sensor hooks |
