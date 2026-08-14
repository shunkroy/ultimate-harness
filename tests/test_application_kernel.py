"""Authoritative provider-neutral application-kernel execution tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from harness2.kernel.application import ApplicationKernel
from harness2.kernel.contracts import (
    CapabilityDescriptor,
    CapabilityEvidence,
    EvidenceKind,
    ExecutionOutcome,
    ExecutionPlan,
    ExecutionRequest,
    Health,
    Maturity,
    RuntimeDescriptor,
)
from harness2.kernel.event_bus import EventBus
from harness2.kernel.registry import CapabilityRegistry, RuntimeRegistry
from harness2.kernel.tasks import TaskRepository, TaskState
from harness2.store import Store
from harness2.application import ForegroundExecutionService
from harness2.models import CapabilityStatus, EngineResult, EngineStatus, RoutingDecision, RunRequest


class Planner:
    def __init__(self, runtime_id="provider-a"):
        self.runtime_id = runtime_id

    def plan(self, request, runtimes):
        return ExecutionPlan(
            "plan-1", request.task_id, self.runtime_id,
            request.required_capabilities, ("execute",), ("typed",), "test plan",
        )


class Driver:
    def __init__(self, success=True, explode=False):
        self.success = success
        self.explode = explode
        self.calls = 0

    def execute(self, request, plan):
        self.calls += 1
        if self.explode:
            raise RuntimeError("private provider failure")
        return ExecutionOutcome(
            request.task_id, plan.plan_id, self.success,
            output="private provider output",
            error_code=None if self.success else "provider_error",
        )


class WrongIdentityDriver(Driver):
    def execute(self, request, plan):
        self.calls += 1
        return ExecutionOutcome("wrong-task", plan.plan_id, True)


class FlakyPlanner(Planner):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def plan(self, request, runtimes):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporarily unavailable")
        return super().plan(request, runtimes)


class ApplicationKernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(os.path.join(self.temp.name, "state", "harness.db"))
        self.events = EventBus(self.store)
        self.tasks = TaskRepository(self.store, self.events)
        runtime = RuntimeDescriptor(
            "provider-a", "test", "Provider A", "1", None, "typed",
            "object", "object", ("code.execute",), maturity=Maturity.TESTED,
            health=Health.HEALTHY,
        )
        capability = CapabilityDescriptor(
            "code.execute", "execute code", Maturity.TESTED, ("provider-a",),
            (CapabilityEvidence(
                EvidenceKind.TEST_VERIFIED, "tests:test_application_kernel",
                "2026-08-13T00:00:00Z", "local test fixture",
            ),),
        )
        self.runtimes = RuntimeRegistry((runtime,))
        self.capabilities = CapabilityRegistry((capability,))

    @staticmethod
    def request(task_id="task-a", capability="code.execute"):
        return ExecutionRequest(task_id, "private objective", (capability,), "provider-a")

    def kernel(self, driver, planner=None):
        return ApplicationKernel(
            self.tasks, self.runtimes, self.capabilities,
            {"provider-a": driver}, planner or Planner(),
        )

    def test_typed_execution_is_authoritative_and_private_output_is_not_persisted(self):
        driver = Driver()
        result = self.kernel(driver).execute(
            self.request(), idempotency_key="run-a", owner_id="worker",
        )
        self.assertEqual(result.task.state, TaskState.COMPLETED)
        self.assertTrue(result.outcome.success)
        self.assertEqual(driver.calls, 1)
        with open(self.store.path, "rb") as stream:
            data = stream.read()
        self.assertNotIn(b"private objective", data)
        self.assertNotIn(b"private provider output", data)

    def test_idempotent_terminal_request_does_not_reexecute(self):
        driver = Driver()
        kernel = self.kernel(driver)
        one = kernel.execute(self.request(), idempotency_key="same", owner_id="one")
        two = kernel.execute(self.request(), idempotency_key="same", owner_id="two")
        self.assertFalse(one.replayed)
        self.assertTrue(two.replayed)
        self.assertEqual(driver.calls, 1)

    def test_missing_capability_fails_before_driver_invocation(self):
        driver = Driver()
        with self.assertRaises(Exception):
            self.kernel(driver).execute(
                self.request(capability="network.unowned"),
                idempotency_key="missing", owner_id="worker",
            )
        self.assertEqual(driver.calls, 0)
        self.assertEqual(self.tasks.get("task-a").state, TaskState.BLOCKED)

    def test_driver_exception_becomes_typed_failure_without_exception_text(self):
        driver = Driver(explode=True)
        result = self.kernel(driver).execute(
            self.request(), idempotency_key="explode", owner_id="worker",
        )
        self.assertEqual(result.task.state, TaskState.FAILED)
        self.assertEqual(result.outcome.error_code, "driver_exception")
        with open(self.store.path, "rb") as stream:
            self.assertNotIn(b"private provider failure", stream.read())

    def test_wrong_provider_outcome_identity_fails_closed(self):
        driver = WrongIdentityDriver()
        result = self.kernel(driver).execute(
            self.request(), idempotency_key="wrong-id", owner_id="worker",
        )
        self.assertEqual(result.task.state, TaskState.FAILED)
        self.assertEqual(result.outcome.error_code, "driver_exception")

    def test_blocked_planning_can_be_rechecked_idempotently(self):
        driver = Driver()
        planner = FlakyPlanner()
        kernel = self.kernel(driver, planner)
        with self.assertRaises(Exception):
            kernel.execute(self.request(), idempotency_key="planning", owner_id="worker")
        self.assertEqual(self.tasks.get("task-a").state, TaskState.BLOCKED)
        result = kernel.execute(self.request(), idempotency_key="planning", owner_id="worker")
        self.assertEqual(result.task.state, TaskState.COMPLETED)


class Adapter:
    def status(self):
        return EngineStatus(
            "opencode", True, True, True, CapabilityStatus.ACTIVE,
            "ready", ("coding",), "private-file",
        )

    def run(self, request):
        return EngineResult("opencode", True, text="answer", metadata={})


class OrchestratorStub:
    def __init__(self, store, adapter):
        from harness2.circuit import CircuitBreaker
        self.store = store
        self.adapter = adapter
        self.breakers = CircuitBreaker(store)
        self.legacy_called = False

    def decide(self, request):
        return RoutingDecision("opencode", "inventor", None, "explicit", (), "explicit")

    def prepare(self, request):
        from harness2.policy import canonicalize_request
        prepared = canonicalize_request(request)
        return prepared, self.decide(prepared)

    def statuses(self):
        return {"opencode": self.adapter.status()}

    @staticmethod
    def breaker_key(engine, request, decision):
        return f"{engine}:-:-"

    def run(self, request):
        self.legacy_called = True
        return self.decide(request), self.adapter.run(request), "legacy"


class ForegroundBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(os.path.join(self.temp.name, "state", "harness.db"))
        self.events = EventBus(self.store)
        self.tasks = TaskRepository(self.store, self.events)
        self.adapter = Adapter()
        self.orchestrator = OrchestratorStub(self.store, self.adapter)
        self.service = ForegroundExecutionService(
            self.store, {"opencode": self.adapter}, self.orchestrator,
            self.events, self.tasks,
        )

    def test_explicit_provider_uses_kernel_lifecycle(self):
        decision, result, run_id = self.service.run(RunRequest("private", engine="opencode"))
        self.assertFalse(self.orchestrator.legacy_called)
        self.assertTrue(result.success)
        self.assertTrue(run_id)
        self.assertTrue(result.metadata["kernel_task_id"])
        task = self.tasks.get(result.metadata["kernel_task_id"])
        self.assertEqual(task.state, TaskState.COMPLETED)

    def test_auto_mode_remains_compatible_during_staged_cutover(self):
        _, _, run_id = self.service.run(RunRequest("private", engine="auto"))
        self.assertTrue(self.orchestrator.legacy_called)
        self.assertEqual(run_id, "legacy")


if __name__ == "__main__":
    unittest.main()
