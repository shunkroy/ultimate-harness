"""Idempotent legacy state import without deleting originals."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from .config import HarnessConfig
from .security import atomic_write_json, read_private_json, redact, task_hash
from .store import Store


def legacy_paths(config: HarnessConfig):
    home = str(config.platform.home)
    return (
        os.path.join(home, ".harness", "env.json"),
        (
            os.path.join(home, ".harness", "runs.jsonl"),
            os.path.join(home, ".prime-cli", "runs.jsonl"),
        ),
    )


def plan(config: HarnessConfig) -> Dict[str, Any]:
    legacy_env, legacy_runs = legacy_paths(config)
    return {
        "legacy_env_exists": os.path.isfile(legacy_env),
        "legacy_run_files": [p for p in legacy_runs if os.path.isfile(p)],
        "destination": config.state_root,
        "originals_deleted": False,
    }


def migrate(config: HarnessConfig, store: Store, dry_run: bool = False) -> Dict[str, Any]:
    result = plan(config)
    if dry_run:
        return result
    config.ensure()
    legacy_env, legacy_runs = legacy_paths(config)
    if not store.migration_applied("legacy.secrets.v1"):
        current = config.secrets()
        legacy = read_private_json(legacy_env)
        key = legacy.get("OPENCODE_API_KEY")
        if isinstance(key, str) and key:
            current["OPENCODE_API_KEY"] = key
            config.save_secrets(current)
        store.mark_migration("legacy.secrets.v1", "legacy Zen key imported if present")

    imported = 0
    if not store.migration_applied("legacy.runs.v1"):
        for path in legacy_runs:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        command = str(rec.get("command", "legacy"))
                        store.append_audit(
                            "legacy.run", task_hash(command),
                            {"source": path, "ok": bool(rec.get("ok")), "detail": redact(rec.get("detail", ""), 120)},
                        )
                        imported += 1
            except OSError:
                continue
        store.mark_migration("legacy.runs.v1", f"imported {imported} audit records")
    result["imported_run_records"] = imported
    return result
