"""Harness v2 command-line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import asdict

from . import __version__
from .adapters.local import LocalAdapter
from .adapters.prime import PrimeAdapter
from .config import HarnessConfig
from .doctor import core_integrity_artifacts, integrity_artifacts, run_checks, summarize
from .migrate import migrate
from .models import RunRequest
from .platforms import HarnessSplitStateError
from .jobs import JobManager
from .orchestrator import Orchestrator
from .policy import PolicyRefusal
from .registry import build_registry
from .security import atomic_write_json, ensure_private_dir, harden_paths, read_private_json, redact, sha256_file
from .store import Store
from . import supervisor
from .capabilities import registry as capability_registry
from .service import ServiceLoop, active_status, service_process_matches
from .context import ContextCompiler, ContextPackage, ContextRuntime
from .discovery import discover
from .kernel.catalog import build_catalog
from .bootstrap import bootstrap
from .kernel.contracts import ExecutionRequest
from .kernel.tasks import TaskState


ZEN_MODELS = (
    "opencode/gpt-5.5", "opencode/claude-sonnet-5",
    "opencode/gpt-5.4-mini", "opencode/qwen3.7-plus",
)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def runtime():
    app = bootstrap()
    return app.config, app.store, app.engines, app.orchestrator


def jobs_runtime():
    app = bootstrap()
    return app.config, app.store, app.engines, app.orchestrator, app.jobs()


def emit(value, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    elif isinstance(value, str):
        print(value)
    else:
        print(value)


def cmd_status(args) -> int:
    app = bootstrap()
    config, store, engines = app.config, app.store, app.engines
    statuses = {name: status.as_dict() if hasattr(status, "as_dict") else asdict(status) for name, status in ((n, e.status()) for n, e in engines.items())}
    task_counts = {state.value: 0 for state in TaskState}
    with store.connect() as con:
        for row in con.execute("SELECT state,COUNT(*) AS count FROM kernel_tasks GROUP BY state"):
            task_counts[str(row["state"])] = int(row["count"])
        event_count = int(con.execute("SELECT COUNT(*) FROM kernel_events").fetchone()[0])
    if args.json:
        emit({
            "version": __version__, "kernel": {"state": "healthy", "schema_version": store.schema_version()},
            "always_active": active_status(config), "tasks": task_counts,
            "events": {"persisted": event_count}, "engines": statuses,
        }, True)
        return 0
    print(f"Harness v{__version__}")
    print(f"KERNEL healthy schema={store.schema_version()}")
    active = active_status(config)
    print(f"ALWAYS_ACTIVE desired={active['desired_always_active']} observed={active['observed_state']}")
    print(
        "TASKS created={created} ready={ready} running={running} waiting={waiting} failed={failed}".format(
            **task_counts
        )
    )
    print(f"EVENTS persisted={event_count}")
    print("ENGINE     ENABLED  HEALTHY  STATUS       DETAIL")
    for name, item in statuses.items():
        print(f"{name:<10} {str(item['enabled']):<8} {str(item['healthy']):<8} {str(item['status'].value if hasattr(item['status'], 'value') else item['status']):<12} {item['detail']}")
    return 0


def cmd_platform(args) -> int:
    config, _, _, _ = runtime()
    p = config.platform
    payload = {
        "kind": p.kind.value,
        "platform": p.platform_id,
        "home": str(p.home),
        "state_dir": str(p.state_dir),
        "config_dir": str(p.config_dir),
        "cache_dir": str(p.cache_dir),
        "runtime_dir": str(p.runtime_dir),
        "supports_unix_supervision": p.supports_unix_supervision,
        "capabilities": p.capability_map(),
        "execution_profile": config.execution_profile(),
        "executables": {
            "opencode": config.opencode_bin,
            "prime": config.prime_launcher,
            "hermes": config.hermes_bin,
            "node": config.node_bin,
            "python": config.python_bin,
            "openssl": config.openssl_bin,
        },
    }
    emit(payload, args.json)
    return 0


def cmd_doctor(args) -> int:
    config, store, _, _ = runtime()
    result = summarize(run_checks(config, store))
    if args.fix_modes:
        paths = [config.state_root, config.database_path, config.database_path + "-wal", config.database_path + "-shm"]
        configured_paths = os.environ.get("HARNESS_HARDEN_PATHS", "")
        paths.extend(path for path in configured_paths.split(os.pathsep) if path)
        changed = harden_paths(paths)
        result["mode_fixes"] = changed
    if args.json:
        emit(result, True)
    else:
        for item in result["checks"]:
            print(f"{'OK' if item['ok'] else 'FAIL':<5} {item['name']:<24} {item['detail']}")
        if args.fix_modes and result.get("mode_fixes"):
            print(f"hardened {len(result['mode_fixes'])} state paths")
    return 0 if result["ok"] else 1


def cmd_integrity(args) -> int:
    config, store, _, _ = runtime()
    if args.action == "pin":
        artifacts = {}
        for name, path in {**core_integrity_artifacts(config), **integrity_artifacts(config)}.items():
            if not os.path.isfile(path):
                print(f"cannot pin missing artifact: {name}", file=sys.stderr)
                return 1
            artifacts[name] = sha256_file(path)
        atomic_write_json(config.integrity_manifest, {
            "schema": 1,
            "algorithm": "sha256",
            "artifacts": artifacts,
        })
        store.append_audit("integrity.pinned", "artifacts", {"count": str(len(artifacts))})
        emit({"ok": True, "manifest": config.integrity_manifest, "artifacts": artifacts}, args.json)
        return 0
    checks = [item for item in run_checks(config, store) if item.name.startswith("integrity.") or item.name in integrity_artifacts(config)]
    payload = {
        "ok": bool(checks) and all(item.ok for item in checks),
        "manifest": config.integrity_manifest,
        "checks": [item.as_dict() for item in checks],
    }
    emit(payload, args.json)
    return 0 if payload["ok"] else 1


def request_from_args(args) -> RunRequest:
    return RunRequest(
        prompt=args.prompt, engine=args.engine, agent=args.agent, model=args.model,
        provider=args.provider, timeout=args.timeout, cwd=args.cwd,
        sensitive=args.sensitive, untrusted=args.untrusted,
        no_fallback=args.no_fallback, dry_run=args.dry_run,
        retries=args.retries, harness_session_id=getattr(args, "session", None),
    )


def cmd_task(args) -> int:
    app = bootstrap()
    if args.action == "list":
        state = TaskState(args.state) if args.state else None
        values = [task.as_dict() for task in app.tasks.list(state=state, limit=args.limit)]
        emit({"tasks": values, "count": len(values)}, args.json)
        return 0
    if args.action == "submit":
        task_id = args.id or __import__("uuid").uuid4().hex
        request = ExecutionRequest(
            task_id=task_id,
            objective=args.objective,
            required_capabilities=tuple(args.capability),
            preferred_runtime=args.runtime,
            constraints={},
            budget={"max_attempts": args.max_attempts},
        )
        task = app.tasks.submit(
            request, task_type=args.type, source="cli.user", reason=args.reason,
            authority="authenticated_user", priority=args.priority,
            idempotency_key=args.idempotency_key, max_attempts=args.max_attempts,
        )
        emit(task.as_dict(), args.json)
        return 0
    task = app.tasks.get(args.id)
    if not task:
        print(f"task not found: {args.id}", file=sys.stderr)
        return 1
    if args.action == "inspect":
        payload = task.as_dict()
        payload["events"] = [event.as_dict() for event in app.events.replay(task_id=args.id, limit=args.limit)]
        emit(payload, args.json)
        return 0
    if args.action == "cancel":
        emit(app.tasks.cancel(args.id).as_dict(), args.json)
        return 0
    if args.action == "retry":
        emit(app.tasks.retry(args.id).as_dict(), args.json)
        return 0
    return 2


def cmd_events(args) -> int:
    app = bootstrap()
    if args.action == "replay":
        values = [event.as_dict() for event in app.events.replay(
            after_seq=args.after, limit=args.limit,
            event_type=args.type, task_id=args.task,
        )]
        emit({"events": values, "count": len(values)}, args.json)
        return 0
    if args.action == "cursor":
        emit({"consumer_id": args.consumer, "last_seq": app.events.cursor(args.consumer)}, args.json)
        return 0
    if args.action == "ack":
        value = app.events.ack(args.consumer, args.through)
        emit({"consumer_id": args.consumer, "last_seq": value}, args.json)
        return 0
    return 2


def cmd_provider(args) -> int:
    app = bootstrap()
    scores = [value.__dict__ for value in app.provider_intelligence.scores(
        capability_id=args.capability,
    )]
    emit({"scores": scores, "count": len(scores), "basis": "local_observations_only"}, args.json)
    return 0


def cmd_resources(args) -> int:
    app = bootstrap()
    if args.action == "status":
        with app.store.connect() as con:
            queue_length = int(con.execute(
                "SELECT COUNT(*) FROM kernel_tasks WHERE state NOT IN ('completed','failed','cancelled')"
            ).fetchone()[0])
        observation = app.resources.observe("local", app.config.state_root, queue_length=queue_length)
        app.resources.record(observation)
        decision = app.resources.evaluate(observation)
        emit({
            "observation": observation.__dict__,
            "decision": {
                "action": decision.action.value,
                "reasons": list(decision.reasons),
                "checkpoint_required": decision.checkpoint_required,
            },
        }, args.json)
        return 0
    return 2


def session_service(app):
    from .sessions import SessionService
    return SessionService(app.store, os.path.join(app.config.state_root, "job.key"))


def cmd_session(args) -> int:
    from .sessions import SessionError
    app = bootstrap()
    service = session_service(app)
    try:
        if args.action == "new":
            emit(service.create(title=getattr(args, "title", "") or ""), args.json)
            return 0
        if args.action == "list":
            emit({"sessions": service.list(limit=args.limit)}, args.json)
            return 0
        if args.action == "info":
            emit(service.info(args.id, limit=args.limit, include_text=args.text), args.json)
            return 0
        if args.action == "resume":
            emit(service.resume(args.id), args.json)
            return 0
        if args.action == "close":
            emit(service.close(args.id), args.json)
            return 0
        if args.action == "attach":
            service.attach(args.id, args.context_id)
            emit({"session_id": args.id, "attached": args.context_id}, args.json)
            return 0
    except SessionError as exc:
        print(f"session error: {exc}", file=sys.stderr)
        return 2
    return 2


def cmd_chat(args) -> int:
    from .sessions import SessionError, run_session_turn
    app = bootstrap()
    service = session_service(app)
    session_id = args.session
    if not session_id:
        created = service.create()
        session_id = created["id"]
    try:
        service.resume(session_id)
    except SessionError as exc:
        print(f"session error: {exc}", file=sys.stderr)
        return 2
    print(f"[harness chat session={session_id}]  (/exit to quit)", file=sys.stderr)
    try:
        while True:
            try:
                prompt = input("> ")
            except EOFError:
                break
            if not prompt.strip():
                continue
            if prompt.strip() in ("/exit", "/quit", "/close"):
                if prompt.strip() == "/close":
                    service.close(session_id)
                break
            try:
                outcome = run_session_turn(
                    service, app.foreground(), session_id, prompt,
                    engine=args.engine, agent=args.agent, model=args.model,
                    provider=args.provider, timeout=args.timeout,
                    no_fallback=args.no_fallback,
                )
            except PolicyRefusal as exc:
                print(f"policy refusal: {exc}", file=sys.stderr)
                continue
            except SessionError as exc:
                print(f"session error: {exc}", file=sys.stderr)
                continue
            if args.json:
                emit(outcome, True)
            elif outcome["success"]:
                print(outcome["assistant_turn"]["text"])
            else:
                turn = outcome["assistant_turn"]
                print(f"error: {turn.get('error_code') or 'run_failed'}", file=sys.stderr)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0


def cmd_run(args) -> int:
    app = bootstrap()
    if getattr(args, "session", None):
        from .sessions import SessionError, run_session_turn
        service = session_service(app)
        try:
            outcome = run_session_turn(
                service, app.foreground(), args.session, args.prompt,
                sensitive=args.sensitive, untrusted=args.untrusted,
                engine=args.engine, agent=args.agent, model=args.model,
                provider=args.provider, timeout=args.timeout, cwd=args.cwd,
                no_fallback=args.no_fallback, dry_run=args.dry_run,
                retries=args.retries,
            )
        except PolicyRefusal as exc:
            if args.json:
                emit({"success": False, "error": str(exc), "error_code": "policy_refusal"}, True)
            else:
                print(f"policy refusal: {exc}", file=sys.stderr)
            return 2
        except SessionError as exc:
            print(f"session error: {exc}", file=sys.stderr)
            return 2
        payload = {
            "session_id": outcome["session_id"],
            "user_turn": outcome["user_turn"], "assistant_turn": outcome["assistant_turn"],
            "decision": outcome["decision"], "run_id": outcome["run_id"],
            "success": outcome["success"], "context": outcome["context"],
        }
        if args.json:
            emit(payload, True)
        else:
            turn = outcome["assistant_turn"]
            print(f"[session={outcome['session_id']} seq={outcome['user_turn']['seq']} "
                  f"engine={turn.get('engine') or '-'} model={turn.get('model') or '-'}]")
            if outcome["success"]:
                print(turn["text"])
            else:
                print(f"error: {turn.get('error_code') or 'run_failed'}", file=sys.stderr)
        return 0 if outcome["success"] else 1
    request = request_from_args(args)
    try:
        decision, result, run_id = app.foreground().run(request)
    except PolicyRefusal as exc:
        if args.json:
            emit({"success": False, "error": str(exc), "error_code": "policy_refusal"}, True)
        else:
            print(f"policy refusal: {exc}", file=sys.stderr)
        return 2
    payload = {
        "run_id": run_id, "decision": asdict(decision), "result": asdict(result),
    }
    if args.json:
        emit(payload, True)
    else:
        print(f"[engine={decision.engine} agent={decision.agent or '-'} model={decision.model or '-'}]")
        if result.text:
            print(result.text)
        if result.error:
            print(f"error: {result.error}", file=sys.stderr)
        print(f"[{result.duration:.1f}s run={run_id or 'dry-run'}]")
    return 0 if result.success else result.exit_code or 1


def cmd_engines(args) -> int:
    _, _, engines, _ = runtime()
    if args.action in ("list", "probe"):
        values = {name: asdict(engine.status()) for name, engine in engines.items()}
        if args.json:
            emit(values, True)
        else:
            for name, value in values.items():
                print(f"{name:<10} {'healthy' if value['healthy'] else 'down':<8} {value['status'].value if hasattr(value['status'], 'value') else value['status']} — {value['detail']}")
        return 0
    return 2


def cmd_caps(args) -> int:
    _, _, _, orchestrator = runtime()
    values = capability_registry(orchestrator.statuses())
    if args.status:
        values = [item for item in values if item.status.value == args.status]
    payload = [item.as_dict() for item in values]
    if args.json:
        emit(payload, True)
    else:
        for item in payload:
            print(f"{item['status']:<12} {item['name']:<22} {item['engine']:<10} {item['description']}")
    return 0


def cmd_providers(args) -> int:
    config, _, engines, orchestrator = runtime()
    if args.action == "discover":
        values = [item.as_dict() for item in discover(config)]
    else:
        runtimes, capabilities = build_catalog(orchestrator.statuses())
        values = {
            "runtimes": [item.as_dict() for item in runtimes.all()],
            "capabilities": [item.as_dict() for item in capabilities.all()],
            "validation_errors": list(capabilities.validate(runtimes)),
            "manifests": {
                name: engine.manifest().as_dict()
                for name, engine in sorted(orchestrator.engines.items())
            },
        }
    emit(values, args.json)
    return 0


def cmd_context(args) -> int:
    app = bootstrap()
    config, store = app.config, app.store
    manager = app.context_jobs()
    if args.action == "compile":
        compiled = ContextCompiler().compile_file(args.source, name=args.name, version=args.version)
        destination = args.output or os.path.join(config.context_root, compiled.ir.context_id)
        package = ContextPackage.write(compiled, destination)
        store.append_audit("context.compiled", package.ir.context_id, {"version": package.ir.version})
        emit({"context_id": package.ir.context_id, "version": package.ir.version, "package": package.root}, args.json)
        return 0
    if args.action == "submit":
        job_id = manager.submit(args.source, name=args.name, version=args.version)
        emit({"id": job_id, "status": "queued"}, args.json)
        return 0
    if args.action == "jobs":
        emit(manager.list(), args.json)
        return 0
    if args.action == "job":
        value = manager.show(args.id)
        if not value:
            print("context job not found", file=sys.stderr)
            return 1
        emit(value, args.json)
        return 0
    context_runtime = ContextRuntime()
    package = context_runtime.load(args.package)
    if args.action == "inspect":
        emit(context_runtime.inspect(package.ir.context_id), args.json)
        return 0
    try:
        inputs = json.loads(args.inputs)
    except ValueError as exc:
        print(f"invalid --inputs JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(inputs, dict):
        print("--inputs must decode to a JSON object", file=sys.stderr)
        return 2
    result = context_runtime.execute(package.ir.context_id, args.operation, inputs)
    emit(result.as_dict(), args.json)
    return 0 if result.success else 1


def cmd_policy(args) -> int:
    _, _, _, orchestrator = runtime()
    request = RunRequest(
        args.prompt, engine=args.engine, agent=args.agent, model=args.model,
        sensitive=args.sensitive, untrusted=args.untrusted, cwd=args.cwd,
        dry_run=True,
    )
    try:
        decision = orchestrator.decide(request)
    except PolicyRefusal as exc:
        print(f"policy refusal: {exc}")
        return 2
    emit(asdict(decision), args.json)
    return 0


def cmd_zen(args) -> int:
    config, store, _, _ = runtime()
    if args.action == "status":
        configured = bool(config.credential("OPENCODE_API_KEY"))
        emit({"configured": configured, "models": ZEN_MODELS}, args.json)
        return 0 if configured else 1
    if args.action == "disconnect":
        secrets = config.secrets()
        secrets.pop("OPENCODE_API_KEY", None)
        config.save_secrets(secrets)
        store.append_audit("secret.removed", "opencode-zen", {"credential": "OPENCODE_API_KEY"})
        print("OpenCode Zen credential removed")
        return 0
    if args.from_file:
        st = os.lstat(args.from_file)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
            print("credential file must be a private regular file (0600)", file=sys.stderr)
            return 2
        fd = os.open(args.from_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as fh:
            key = fh.read().strip()
    elif not sys.stdin.isatty():
        key = sys.stdin.read().strip()
    else:
        key = getpass.getpass("OpenCode Zen key: ").strip()
    if len(key) < 12:
        print("invalid or empty key", file=sys.stderr)
        return 2
    secrets = config.secrets()
    secrets["OPENCODE_API_KEY"] = key
    config.save_secrets(secrets)
    store.append_audit("secret.updated", "opencode-zen", {"credential": "OPENCODE_API_KEY"})
    print("OpenCode Zen credential stored privately (value not displayed)")
    return 0


AUTH_KEYS = {
    "openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "zen": "OPENCODE_API_KEY",
}


def _read_private_input(from_file: str | None, label: str) -> str:
    if from_file:
        st = os.lstat(from_file)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
            raise ValueError("credential file must be a private regular file (0600)")
        fd = os.open(from_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as fh:
            return fh.read().strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return getpass.getpass(label).strip()


def cmd_auth(args) -> int:
    config, store, _, _ = runtime()
    if args.action == "status":
        payload = {provider: bool(config.credential(name)) for provider, name in AUTH_KEYS.items()}
        emit(payload, args.json)
        return 0
    if args.action == "import-env":
        secrets = config.secrets()
        imported = []
        for provider, name in AUTH_KEYS.items():
            value = os.environ.get(name)
            if value:
                secrets[name] = value
                imported.append(provider)
        config.save_secrets(secrets)
        store.append_audit("secret.imported", "providers", {"providers": ",".join(imported)})
        emit({"imported": imported}, args.json)
        return 0
    name = AUTH_KEYS[args.provider]
    secrets = config.secrets()
    if args.action == "remove":
        secrets.pop(name, None)
        config.save_secrets(secrets)
        store.append_audit("secret.removed", args.provider, {"credential": name})
        print(f"{args.provider} credential removed")
        return 0
    value = _read_private_input(args.from_file, f"{args.provider} credential: ")
    if len(value) < 12:
        print("invalid or empty credential", file=sys.stderr)
        return 2
    secrets[name] = value
    config.save_secrets(secrets)
    store.append_audit("secret.updated", args.provider, {"credential": name})
    print(f"{args.provider} credential stored privately (value not displayed)")
    return 0


def cmd_prime(args) -> int:
    config, _, engines, _ = runtime()
    prime: PrimeAdapter = engines["prime"]  # type: ignore[assignment]
    try:
        if args.action == "start":
            status = prime.start(wait_socket=args.wait)
            emit(status.as_dict(), args.json)
            return 0 if status.healthy else 1
        if args.action == "stop":
            status = prime.stop()
            emit(status.as_dict(), args.json)
            return 0 if not status.running else 1
        if args.action == "restart":
            prime.stop()
            status = prime.start(wait_socket=args.wait)
            emit(status.as_dict(), args.json)
            return 0 if status.healthy else 1
        if args.action == "status":
            status = prime.daemon_status()
            emit(status.as_dict(), args.json)
            return 0 if status.healthy else 1
        if args.action == "watch":
            print(f"Prime watchdog every {args.interval}s; Ctrl+C to stop")
            while True:
                status = prime.daemon_status()
                if not status.healthy:
                    try:
                        status = prime.start(wait_socket=args.wait)
                    except Exception as exc:
                        print(f"restart failed: {exc}", file=sys.stderr)
                print(f"{time.strftime('%H:%M:%S')} {'healthy' if status.healthy else 'down'}")
                time.sleep(args.interval)
        if args.action in ("list", "agents"):
            command = ["list"]
            if args.all:
                command.append("--all")
            if args.prime_json:
                command.append("--json")
        elif args.action == "send":
            command = ["send"]
            if args.from_agent:
                command += ["--from", args.from_agent]
            command += [args.target, args.message]
            if args.steer:
                command.append("--steer")
            if args.follow_up:
                command.append("--follow-up")
            if args.prime_json:
                command.append("--json")
        elif args.action == "attach":
            command = ["attach", args.target]
        else:
            command = [args.action, *args.rest]
        proc = prime.passthrough(command, timeout=args.timeout)
        print((proc.stdout or proc.stderr).strip())
        return proc.returncode
    except Exception as exc:
        print(f"prime {args.action} failed: {redact(exc)}", file=sys.stderr)
        return 1


def cmd_local(args) -> int:
    _, _, engines, _ = runtime()
    local: LocalAdapter = engines["local"]  # type: ignore[assignment]
    if args.action == "enable":
        local.set_enabled(True)
    elif args.action == "disable":
        local.set_enabled(False)
    emit(asdict(local.status()), args.json)
    return 0


def cmd_audit(args) -> int:
    _, store, _, _ = runtime()
    if args.action == "verify":
        ok, count, bad = store.verify_audit()
        emit({"ok": ok, "entries": count, "bad_seq": bad}, args.json)
        return 0 if ok else 1
    with store.connect() as con:
        rows = con.execute(
            "SELECT seq,ts,event,subject,metadata_json,entry_hash FROM audit ORDER BY seq DESC LIMIT ?",
            (args.tail,),
        ).fetchall()
    values = [dict(row) for row in rows]
    if args.json:
        emit(values, True)
    else:
        for row in values:
            print(f"{row['seq']:>5} {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['ts']))} {row['event']:<20} {row['subject']}")
    return 0


def cmd_log(args) -> int:
    _, store, _, _ = runtime()
    rows = store.list_runs(args.tail)
    if args.json:
        emit(rows, True)
    else:
        for row in rows:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['started_at']))} {row['status']:<9} {row['engine']:<9} {row['task_hash'][:12]} {row['error_code'] or ''}")
    return 0


def cmd_jobs(args) -> int:
    _, _, _, _orchestrator, manager = jobs_runtime()
    if args.action == "submit":
        request = RunRequest(
            args.prompt, engine=args.engine, agent=args.agent, model=args.model,
            provider=args.provider, timeout=args.timeout, cwd=args.cwd,
            sensitive=args.sensitive, untrusted=args.untrusted,
            no_fallback=args.no_fallback, retries=args.retries,
        )
        try:
            job_id = manager.submit(request, max_attempts=args.max_attempts)
        except PolicyRefusal as exc:
            print(f"policy refusal: {exc}", file=sys.stderr)
            return 2
        emit({"id": job_id, "status": "queued"}, args.json)
        return 0
    if args.action == "list":
        rows = manager.list(args.limit)
        if args.json:
            emit(rows, True)
        else:
            for row in rows:
                print(f"{row['id'][:12]} {row['status']:<10} {row['engine']:<9} attempt={row['attempt']}/{row['max_attempts']} {row['task_hash'][:12]}")
        return 0
    if args.action == "show":
        row = manager.show(args.id)
        if not row:
            print("job not found", file=sys.stderr)
            return 1
        emit(row, args.json)
        return 0
    operation = getattr(manager, args.action)
    ok = operation(args.id)
    emit({"id": args.id, "action": args.action, "ok": ok}, args.json)
    return 0 if ok else 1


def cmd_work(args) -> int:
    _, _, _, _, manager = jobs_runtime()
    if args.once:
        result = manager.work_once()
        emit(result or {"status": "idle"}, args.json)
        return 0
    print(f"Harness worker loop every {args.interval}s; Ctrl+C to stop")
    while True:
        result = manager.work_once()
        if result:
            emit(result, args.json)
        time.sleep(args.interval)


def _service_matches(pid: int, config: HarnessConfig | None = None) -> bool:
    return service_process_matches(config or HarnessConfig(), pid)


def _find_services(config: HarnessConfig | None = None) -> list[int]:
    values = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return values
    for entry in entries:
        if entry.isdigit() and _service_matches(int(entry), config):
            values.append(int(entry))
    return sorted(values)


def service_status(config: HarnessConfig):
    pidfile = os.path.join(config.state_root, "run", "service.pid")
    try:
        pid = supervisor.read_pidfile(pidfile)
    except Exception:
        pid = None
    alive = bool(pid and _service_matches(pid, config))
    if not alive:
        matches = _find_services(config)
        if matches:
            pid, alive = matches[0], True
            supervisor.write_pidfile(pidfile, pid)
    return pidfile, pid, alive


def cmd_svc(args) -> int:
    config, _, _, _ = runtime()
    if config.platform.kind.value not in {"linux", "termux", "proot"}:
        print("svc is only the Linux/Termux/PRoot helper; run 'harness supervise' via launchd, Task Scheduler, NSSM or WinSW", file=sys.stderr)
        return 2
    run_dir = ensure_private_dir(os.path.join(config.state_root, "run"))
    pidfile, pid, alive = service_status(config)
    if args.action == "status":
        payload = active_status(config)
        emit(payload, args.json)
        return 0 if payload["active"] else 1
    if args.action == "down":
        if alive and pid:
            try:
                os.killpg(pid, 15)
            except ProcessLookupError:
                alive = False
            deadline = time.time() + 5
            while time.time() < deadline and supervisor.pid_alive(pid):
                time.sleep(0.1)
            if supervisor.pid_alive(pid) and _service_matches(pid, config):
                try:
                    os.killpg(pid, 9)
                except ProcessLookupError:
                    pass
        try:
            supervisor.safe_unlink(pidfile)
        except (FileNotFoundError, supervisor.SecurityError):
            return 1
        print("harness supervisor stopped")
        return 0
    if args.action == "restart":
        cmd_svc(argparse.Namespace(action="down", json=args.json))
        time.sleep(1)
    lock = supervisor.acquire_start_lock(run_dir, name=".service-start.lock", timeout=10)
    try:
        pidfile, pid, alive = service_status(config)
        if alive:
            print(f"harness supervisor already running pid {pid}")
            return 0
        log_path = os.path.join(config.state_root, "logs", "service.log")
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            child_env = {
                **config.clean_env("prime", daemon=True),
                "PYTHONPATH": config.package_root,
                "HARNESS2_HOME": str(config.state_root),
                "HARNESS_ALWAYS_ACTIVE": "true" if config.always_active_default else "false",
            }
            if config.harness_launcher:
                child_env["HARNESS_LAUNCHER"] = config.harness_launcher
            proc = subprocess.Popen(
                [str(config.python_bin), "-m", "harness2", "supervise", "--interval", str(args.interval)],
                env=child_env,
                stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT,
                **config.platform.background_kwargs(), cwd=config.package_root,
            )
        finally:
            os.close(fd)
        supervisor.write_pidfile(pidfile, proc.pid)
    finally:
        lock.release()
    print(f"harness supervisor started pid {proc.pid}")
    return 0


def cmd_supervise(args) -> int:
    app = bootstrap()
    config, store, engines, manager = app.config, app.store, app.engines, app.jobs()
    prime: PrimeAdapter = engines["prime"]  # type: ignore[assignment]
    pidfile = os.path.join(config.state_root, "run", "service.pid")
    existing = supervisor.read_pidfile(pidfile)
    if existing and existing != os.getpid() and service_process_matches(config, existing):
        raise RuntimeError(f"Harness supervisor is already running (pid {existing})")
    supervisor.write_pidfile(pidfile, os.getpid())
    try:
        ServiceLoop.bootstrap(
            config, store, prime, manager, app.context_jobs(),
            interval=args.interval,
        ).run()
    finally:
        try:
            if supervisor.read_pidfile(pidfile) == os.getpid():
                supervisor.safe_unlink(pidfile)
        except (FileNotFoundError, supervisor.SecurityError):
            pass
    return 0


def cmd_migrate(args) -> int:
    config, store, _, _ = runtime()
    emit(migrate(config, store, dry_run=args.dry_run), args.json)
    return 0


def cmd_legacy(args) -> int:
    path = os.environ.get("HARNESS_V1_BIN", "/usr/local/bin/harness.v1" if os.name != "nt" else "")
    if not os.path.exists(path):
        print("legacy harness is not installed", file=sys.stderr)
        return 1
    return subprocess.call([path, *args.rest])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description=f"Unified agent harness v{__version__}")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version")
    sub.add_parser("status")
    sub.add_parser("platform")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--fix-modes", action="store_true")
    integrity = sub.add_parser("integrity")
    integrity.add_argument("action", choices=("pin", "verify"))

    run = sub.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--engine", choices=("auto", "opencode", "zen", "prime", "hermes", "local", "direct"), default="auto")
    run.add_argument("--agent")
    run.add_argument("--model")
    run.add_argument("--provider")
    run.add_argument("--timeout", type=positive_int, default=240)
    run.add_argument("--cwd")
    run.add_argument("--sensitive", action="store_true")
    run.add_argument("--untrusted", action="store_true")
    run.add_argument("--no-fallback", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--retries", type=nonnegative_int, default=1)
    run.add_argument("--session")

    chat = sub.add_parser("chat")
    chat.add_argument("--session")
    chat.add_argument("--engine", choices=("auto", "opencode", "zen", "prime", "hermes", "local", "direct"), default="auto")
    chat.add_argument("--agent")
    chat.add_argument("--model")
    chat.add_argument("--provider")
    chat.add_argument("--timeout", type=positive_int, default=240)
    chat.add_argument("--no-fallback", action="store_true")

    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="action", required=True)
    session_sub.add_parser("new").add_argument("--title")
    session_list = session_sub.add_parser("list")
    session_list.add_argument("--limit", type=positive_int, default=50)
    for action in ("resume", "close"):
        child = session_sub.add_parser(action)
        child.add_argument("id")
    session_info = session_sub.add_parser("info")
    session_info.add_argument("id")
    session_info.add_argument("--text", action="store_true")
    session_info.add_argument("--limit", type=positive_int, default=50)
    attach_s = session_sub.add_parser("attach")
    attach_s.add_argument("id")
    attach_s.add_argument("context_id")

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="action", required=True)
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--state", choices=tuple(state.value for state in TaskState))
    task_list.add_argument("--limit", type=positive_int, default=100)
    task_submit = task_sub.add_parser("submit")
    task_submit.add_argument("objective")
    task_submit.add_argument("--id")
    task_submit.add_argument("--idempotency-key")
    task_submit.add_argument("--type", default="execution")
    task_submit.add_argument("--reason", default="explicit request")
    task_submit.add_argument("--priority", type=int, default=0)
    task_submit.add_argument("--capability", action="append", default=[])
    task_submit.add_argument("--runtime")
    task_submit.add_argument("--max-attempts", type=positive_int, default=1)
    for action in ("inspect", "cancel", "retry"):
        child = task_sub.add_parser(action)
        child.add_argument("id")
        child.add_argument("--limit", type=positive_int, default=100)

    events = sub.add_parser("events")
    events_sub = events.add_subparsers(dest="action", required=True)
    replay = events_sub.add_parser("replay")
    replay.add_argument("--after", type=nonnegative_int, default=0)
    replay.add_argument("--limit", type=positive_int, default=100)
    replay.add_argument("--type")
    replay.add_argument("--task")
    cursor = events_sub.add_parser("cursor")
    cursor.add_argument("consumer")
    ack = events_sub.add_parser("ack")
    ack.add_argument("consumer")
    ack.add_argument("through", type=nonnegative_int)

    provider_scores = sub.add_parser("provider")
    provider_scores.add_argument("action", choices=("scores",))
    provider_scores.add_argument("--capability")

    resources = sub.add_parser("resources")
    resources.add_argument("action", choices=("status",))

    engines = sub.add_parser("engines")
    engines.add_argument("action", choices=("list", "probe"), default="list", nargs="?")
    caps = sub.add_parser("caps")
    caps.add_argument("--status", choices=("documented", "planned", "tested", "implemented", "active"))
    providers = sub.add_parser("providers")
    providers.add_argument("action", choices=("list", "discover"), nargs="?", default="list")

    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="action", required=True)
    compile_context = context_sub.add_parser("compile")
    compile_context.add_argument("source")
    compile_context.add_argument("--name")
    compile_context.add_argument("--version", default="0.1.0")
    compile_context.add_argument("--output")
    submit_context = context_sub.add_parser("submit")
    submit_context.add_argument("source")
    submit_context.add_argument("--name")
    submit_context.add_argument("--version", default="0.1.0")
    context_sub.add_parser("jobs")
    context_job = context_sub.add_parser("job")
    context_job.add_argument("id")
    inspect_context = context_sub.add_parser("inspect")
    inspect_context.add_argument("package")
    execute_context = context_sub.add_parser("execute")
    execute_context.add_argument("package")
    execute_context.add_argument("operation")
    execute_context.add_argument("--inputs", required=True)

    policy = sub.add_parser("policy")
    policy.add_argument("prompt")
    policy.add_argument("--engine", choices=("auto", "opencode", "zen", "prime", "hermes", "local"), default="auto")
    policy.add_argument("--agent")
    policy.add_argument("--model")
    policy.add_argument("--cwd")
    policy.add_argument("--sensitive", action="store_true")
    policy.add_argument("--untrusted", action="store_true")

    zen = sub.add_parser("zen")
    zen.add_argument("action", choices=("connect", "disconnect", "status"))
    zen.add_argument("--from-file")
    auth = sub.add_parser("auth")
    auth.add_argument("action", choices=("set", "remove", "status", "import-env"))
    auth.add_argument("provider", choices=tuple(AUTH_KEYS), nargs="?")
    auth.add_argument("--from-file")

    prime = sub.add_parser("prime")
    prime_sub = prime.add_subparsers(dest="action", required=True)
    for action in ("start", "restart"):
        child = prime_sub.add_parser(action)
        child.add_argument("--wait", type=positive_int, default=60)
        child.set_defaults(interval=30, timeout=180, rest=[])
    for action in ("stop", "status"):
        child = prime_sub.add_parser(action)
        child.set_defaults(wait=60, interval=30, timeout=180, rest=[])
    watch = prime_sub.add_parser("watch")
    watch.add_argument("--wait", type=positive_int, default=60)
    watch.add_argument("--interval", type=positive_int, default=30)
    watch.set_defaults(timeout=180, rest=[])
    for action in ("agents", "list"):
        child = prime_sub.add_parser(action)
        child.add_argument("--all", action="store_true")
        child.add_argument("--prime-json", action="store_true")
        child.add_argument("--timeout", type=positive_int, default=180)
        child.set_defaults(wait=60, interval=30, rest=[])
    send = prime_sub.add_parser("send")
    send.add_argument("target")
    send.add_argument("message")
    send.add_argument("--from", dest="from_agent")
    send.add_argument("--steer", action="store_true")
    send.add_argument("--follow-up", action="store_true")
    send.add_argument("--prime-json", action="store_true")
    send.add_argument("--timeout", type=positive_int, default=180)
    send.set_defaults(wait=60, interval=30, rest=[])
    attach = prime_sub.add_parser("attach")
    attach.add_argument("target")
    attach.add_argument("--timeout", type=positive_int, default=180)
    attach.set_defaults(wait=60, interval=30, rest=[])
    schedule = prime_sub.add_parser("schedule")
    schedule.add_argument("rest", nargs=argparse.REMAINDER)
    schedule.add_argument("--timeout", type=positive_int, default=180)
    schedule.set_defaults(wait=60, interval=30)

    local = sub.add_parser("local")
    local.add_argument("action", choices=("enable", "disable", "status"))

    audit = sub.add_parser("audit")
    audit.add_argument("action", choices=("tail", "verify"))
    audit.add_argument("--tail", type=positive_int, default=20)

    log = sub.add_parser("log")
    log.add_argument("--tail", type=positive_int, default=20)
    mig = sub.add_parser("migrate")
    mig.add_argument("--dry-run", action="store_true")
    legacy = sub.add_parser("legacy")
    legacy.add_argument("rest", nargs=argparse.REMAINDER)

    jobs = sub.add_parser("jobs")
    job_sub = jobs.add_subparsers(dest="action", required=True)
    submit = job_sub.add_parser("submit")
    submit.add_argument("prompt")
    submit.add_argument("--engine", choices=("auto", "opencode", "zen", "prime", "hermes", "local"), default="auto")
    submit.add_argument("--agent")
    submit.add_argument("--model")
    submit.add_argument("--provider")
    submit.add_argument("--timeout", type=positive_int, default=240)
    submit.add_argument("--cwd")
    submit.add_argument("--sensitive", action="store_true")
    submit.add_argument("--untrusted", action="store_true")
    submit.add_argument("--no-fallback", action="store_true")
    submit.add_argument("--retries", type=nonnegative_int, default=1)
    submit.add_argument("--max-attempts", type=positive_int, default=3)
    listing = job_sub.add_parser("list")
    listing.add_argument("--limit", type=positive_int, default=20)
    for action in ("show", "cancel", "retry", "purge"):
        child = job_sub.add_parser(action)
        child.add_argument("id")

    work = sub.add_parser("work")
    work.add_argument("--once", action="store_true")
    work.add_argument("--interval", type=positive_int, default=10)

    svc = sub.add_parser("svc")
    svc.add_argument("action", choices=("up", "down", "restart", "status"))
    svc.add_argument("--interval", type=positive_int, default=30)
    supervise = sub.add_parser("supervise")
    supervise.add_argument("--interval", type=positive_int, default=30)
    return parser


def main(argv=None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in values
    values = [value for value in values if value != "--json"]
    args = build_parser().parse_args(values)
    args.json = json_requested or getattr(args, "json", False)
    try:
        if args.command == "version":
            print(__version__)
            return 0
        handler = globals()[f"cmd_{args.command}"]
        return int(handler(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except HarnessSplitStateError as exc:
        message = (
            f"split Harness state detected:\n{exc}\n"
            "No state root was selected. Fix the split first (set HARNESS2_HOME "
            "or move one tree aside) and retry."
        )
        if getattr(args, "json", False):
            emit({"success": False, "error": str(exc), "error_code": "split_state"}, True)
        else:
            print(message, file=sys.stderr)
        return 2
    except Exception as exc:
        if getattr(args, "json", False):
            emit({"success": False, "error": redact(exc), "error_code": "internal_error"}, True)
        else:
            print(f"harness error: {redact(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
