#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def use_inmemory_channel_layer(env: dict[str, str]) -> bool:
    redis_url = env.get("REDIS_URL", "").strip()
    force_inmemory = env.get("USE_INMEMORY_CHANNEL_LAYER", "").strip().lower() == "true"
    return force_inmemory or not redis_url


def run_manage(args: list[str], env: dict[str, str]) -> int:
    command = [sys.executable, "manage.py", *args]
    return subprocess.run(command, cwd=ROOT_DIR, env=env).returncode


def build_runserver_args(env: dict[str, str], host: str, port: int) -> list[str]:
    args = ["runserver", f"{host}:{port}"]
    if use_inmemory_channel_layer(env):
        args.append("--noreload")
    return args


def spawn_manage(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    command = [sys.executable, "manage.py", *args]
    kwargs: dict[str, object] = {
        "cwd": str(ROOT_DIR),
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def stop_process(proc: subprocess.Popen, label: str) -> None:
    if proc.poll() is not None:
        return

    print(f"[dev] stopping {label}...")
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=5)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        return
    except Exception:
        pass

    try:
        proc.terminate()
        proc.wait(timeout=5)
        return
    except Exception:
        pass

    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    except Exception:
        pass


def run_web(env: dict[str, str], host: str, port: int, skip_check: bool) -> int:
    if not skip_check:
        code = run_manage(["check"], env)
        if code != 0:
            return code
    if use_inmemory_channel_layer(env):
        print("[dev] in-memory embedded runtime detected; starting runserver with --noreload so live console/process handles survive code changes.")
    print(f"[dev] starting web on http://{host}:{port}/")
    return run_manage(build_runserver_args(env, host, port), env)


def run_worker(env: dict[str, str], skip_check: bool) -> int:
    if not skip_check:
        code = run_manage(["check"], env)
        if code != 0:
            return code
    if use_inmemory_channel_layer(env):
        print("[dev] warning: channel layer is in-memory; worker can run, but live cross-process websocket updates are limited without Redis.")
    print("[dev] starting runtime worker")
    return run_manage(["run_runtime_worker"], env)


def run_all(env: dict[str, str], host: str, port: int, skip_check: bool, force_worker: bool) -> int:
    if not skip_check:
        code = run_manage(["check"], env)
        if code != 0:
            return code

    worker_proc: subprocess.Popen | None = None
    worker_allowed = force_worker or not use_inmemory_channel_layer(env)

    if worker_allowed:
        print("[dev] starting runtime worker in background")
        worker_proc = spawn_manage(["run_runtime_worker"], env)
    else:
        print("[dev] worker skipped: no Redis configured, so the default in-memory channel layer would split websocket events across processes.")
        print("[dev] if you want full local multi-process runtime, configure REDIS_URL and set USE_INMEMORY_CHANNEL_LAYER=false.")

    try:
        if use_inmemory_channel_layer(env):
            print("[dev] in-memory embedded runtime detected; starting runserver with --noreload so live console/process handles survive code changes.")
        print(f"[dev] starting web on http://{host}:{port}/")
        return run_manage(build_runserver_args(env, host, port), env)
    finally:
        if worker_proc is not None:
            stop_process(worker_proc, "runtime worker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-platform dev launcher for GameServer Panel.")
    parser.add_argument("mode", choices=["check", "migrate", "web", "worker", "all"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--skip-check", action="store_true")
    parser.add_argument("--force-worker", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    env = build_env()

    if args.mode == "check":
        return run_manage(["check"], env)
    if args.mode == "migrate":
        return run_manage(["migrate"], env)
    if args.mode == "web":
        return run_web(env, args.host, args.port, args.skip_check)
    if args.mode == "worker":
        return run_worker(env, args.skip_check)
    return run_all(env, args.host, args.port, args.skip_check, args.force_worker)


if __name__ == "__main__":
    raise SystemExit(main())