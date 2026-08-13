"""Application composition root; command frontends contain no dependency wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .config import HarnessConfig
from .jobs import JobManager
from .kernel.catalog import build_catalog
from .kernel.event_bus import EventBus
from .kernel.tasks import TaskRepository
from .orchestrator import Orchestrator
from .registry import build_registry
from .store import Store
from .application import ForegroundExecutionService
from .kernel.provider_intelligence import ProviderIntelligence
from .kernel.resources import ResourceGovernor
from .storage import LocalAuthenticatedStorage
from .kernel.task_types import TaskTypeRegistry, default_task_types
from .kernel.execution_state import ExecutionStateRepository
from .context.jobs import ContextJobManager
from .sandbox import DisabledSandboxBackend, SandboxBackend
from .skills.provenance import ProvenanceRepository


@dataclass(frozen=True)
class ApplicationRuntime:
    config: HarnessConfig
    store: Store
    engines: Mapping[str, object]
    orchestrator: Orchestrator
    events: EventBus
    tasks: TaskRepository
    provider_intelligence: ProviderIntelligence
    resources: ResourceGovernor
    objects: LocalAuthenticatedStorage
    task_types: TaskTypeRegistry
    sandbox: SandboxBackend

    @property
    def catalog(self):
        statuses = {name: engine.status() for name, engine in self.engines.items()}
        return build_catalog(statuses)

    def jobs(self) -> JobManager:
        return JobManager(self.config, self.store, self.orchestrator)

    def foreground(self) -> ForegroundExecutionService:
        return ForegroundExecutionService(
            self.store, self.engines, self.orchestrator, self.events, self.tasks,
        )

    def execution_state(self) -> ExecutionStateRepository:
        return ExecutionStateRepository(
            self.store, self.events, self.tasks, self.objects, self.task_types,
        )

    def context_jobs(self) -> ContextJobManager:
        return ContextJobManager(self.config, self.store, self.execution_state())

    def provenance(self) -> ProvenanceRepository:
        return ProvenanceRepository(self.store, self.events, self.execution_state())


def bootstrap() -> ApplicationRuntime:
    config = HarnessConfig()
    config.ensure()
    store = Store(config.database_path)
    engines = build_registry(config, store)
    orchestrator = Orchestrator(engines, store)
    events = EventBus(store)
    tasks = TaskRepository(store, events)
    return ApplicationRuntime(
        config, store, engines, orchestrator, events, tasks,
        ProviderIntelligence(store), ResourceGovernor(store),
        LocalAuthenticatedStorage(
            config.object_store_root, config.object_store_key,
            openssl_bin=config.openssl_bin,
        ),
        default_task_types(),
        DisabledSandboxBackend(),
    )
