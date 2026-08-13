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
    )
