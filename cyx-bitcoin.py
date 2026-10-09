#!/usr/bin/env python3
"""CYX Prompt Auto - interactive XMRig/RandomX controller.

Interactive controller for a locally installed XMRig executable. It manages a
small JSON config, launches/stops XMRig, reports status, and can generate
RandomX test jobs using a single RandomX algorithm configuration.

It does not download software, alter system startup, or run hidden/background
persistence. Mining starts only after an explicit `start` command.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "cyx-xmrig-config.json"
STATE_PATH = APP_DIR / "cyx-prompt-state.json"
DEFAULT_POOL = "xmrig.nanswap.com:3333"
DEFAULT_ALGO = "rx/0"

# Accepted controller-level algorithm names. These are passed to XMRig as
# --algo values; availability ultimately depends on the installed XMRig build.
ALGO_ALIASES = {
    "rx/0": "rx/0",
    "randomx": "rx/0",
    "randomx/0": "rx/0",
}


DEFAULT_PASS = "x"



DEFAULT_CONFIG: dict[str, Any] = {
    "autosave": True,
    "cpu": True,
    "opencl": False,
    "cuda": False,
    "threads": 0,
    "pools": [{
        "algo": DEFAULT_ALGO,
        "coin": "monero",
        "url": DEFAULT_POOL,
        "user": "",
        "pass": DEFAULT_PASS,
        "keepalive": True,
        "tls": False,
    }],
}


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Config warning: {exc}; using defaults.")
        return json.loads(json.dumps(default))


def save_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def pool(cfg: dict[str, Any]) -> dict[str, Any]:
    pools = cfg.setdefault("pools", [])
    if not pools:
        pools.append(dict(DEFAULT_CONFIG["pools"][0]))
    return pools[0]


def find_xmrig(explicit: str | None = None) -> str | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend([
        APP_DIR / "xmrig",
        APP_DIR / "xmrig.exe",
        Path.cwd() / "xmrig",
        Path.cwd() / "xmrig.exe",
    ])
    for p in candidates:
        if p.is_file():
            return str(p)
    import shutil
    return shutil.which("xmrig") or shutil.which("xmrig.exe")


def process_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def load_saved_pid() -> int | None:
    """Recover a controller-owned XMRig PID when the controller is restarted."""
    state = load_json(STATE_PATH, {"pid": None})
    pid = state.get("pid")
    if isinstance(pid, int) and process_alive(pid):
        return pid
    return None


def status(cfg: dict[str, Any], proc: subprocess.Popen | None) -> None:
    p = pool(cfg)
    running = bool(proc and proc.poll() is None)
    pid = proc.pid if running and proc else None
    print(f"\nXMRig: {'RUNNING' if running else 'STOPPED'}" + (f" (PID {pid})" if pid else ""))
    print(f"Pool:  {p.get('url', '')}")
    print(f"Algo:  {p.get('algo', '')}")
    print(f"Backend: {cfg.get('miner_backend', 'stock/auto')}")
    print(f"User:  {'set' if p.get('user') else 'NOT SET'}")
    print(f"TLS:   {bool(p.get('tls', False))}")
    print(f"Threads: {cfg.get('threads', 0) or 'auto'}")
    print(f"Config: {CONFIG_PATH}")


def validate(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        p = pool(cfg)
    except Exception as exc:
        return [f"invalid pools configuration: {exc}"]
    for key in ("url", "user", "pass"):
        if not isinstance(p.get(key), str) or not p.get(key, "").strip():
            errors.append(f"pool.{key} is empty")
    algo = str(p.get("algo", "")).strip().lower()
    if algo not in set(ALGO_ALIASES.values()):
        errors.append("pool.algo must be RandomX (rx/0)")
    url = str(p.get("url", "")).strip()
    if url and not re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?[^\s:]+:\d{1,5}$", url):
        errors.append("pool.url must be HOST:PORT or scheme://HOST:PORT")
    threads = cfg.get("threads", 0)
    try:
        threads = int(threads)
        if threads < 0 or threads > 256:
            errors.append("threads must be 0 (auto) or an integer from 1-256")
    except (TypeError, ValueError):
        errors.append("threads must be 0 (auto) or an integer from 1-256")
    return errors


def save_state(pid: int | None = None) -> None:
    save_json(STATE_PATH, {"pid": pid, "updated": int(time.time())})


def algorithm_diagnostic(name: str, xmrig: str | None) -> None:
    """Explain stock-XMRig compatibility and optionally dry-run the binary."""
    requested = " ".join(name.strip().lower().split())
    canonical = ALGO_ALIASES.get(requested)
    if canonical is None:
        print(f"Unknown controller algorithm: {name}")
        return
    print(f"Requested: {requested}")
    print(f"Canonical: {canonical}")
    if canonical in XMRIG_STOCK_ALGOS:
        print("Stock XMRig: documented")
    else:
        print("Stock XMRig: NOT documented")
        print("This algorithm is not documented by stock XMRig.")
    exe = find_xmrig(xmrig)
    if not exe:
        print("Executable test: XMRig not found")
        return
    print(f"Executable: {exe}")
    try:
        result = subprocess.run(
            [exe, f"--algo={canonical}", "--dry-run", "--no-color"],
            cwd=str(Path(exe).resolve().parent),
            capture_output=True, text=True, timeout=20, check=False,
        )
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode == 0:
            print("Binary test: ACCEPTED by this XMRig build")
        else:
            print(f"Binary test: REJECTED (exit {result.returncode})")
        if output:
            lines = output.splitlines()
            print("\n".join(lines[-20:]))
    except subprocess.TimeoutExpired:
        print("Binary test: timed out after 20 seconds")
    except OSError as exc:
        print(f"Binary test failed: {exc}")


def start(cfg: dict[str, Any], proc: subprocess.Popen | None, xmrig: str | None) -> subprocess.Popen | None:
    if proc and proc.poll() is None:
        print("Already running.")
        return proc
    errors = validate(cfg)
    if errors:
        print("Cannot start:")
        for e in errors:
            print(f"  - {e}")
        print("Use `wallet`, `pool`, or `set` to configure it first.")
        return None
    xmrig = find_xmrig(xmrig)
    if not xmrig:
        print("XMRig executable not found. Put xmrig/xmrig.exe beside this script or use `xmrig <path>`.")
        return None
    save_json(CONFIG_PATH, cfg)
    print(f"Starting {xmrig} with {CONFIG_PATH} ...")
    try:
        kwargs: dict[str, Any] = {"cwd": str(Path(xmrig).resolve().parent)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        cmd = [xmrig, "--config", str(CONFIG_PATH)]
        threads = int(cfg.get("threads", 0) or 0)
        if threads > 0:
            cmd.append(f"--threads={threads}")
        proc = subprocess.Popen(cmd, **kwargs)
        save_state(proc.pid)
        print(f"Started PID {proc.pid}.")
        return proc
    except OSError as exc:
        print(f"Start failed: {exc}")
        return None


def stop(proc: subprocess.Popen | None) -> subprocess.Popen | None:
    """Stop the session process, or the last controller-owned persisted PID."""
    pid = None
    session_proc = proc if proc and proc.poll() is None else None
    if session_proc:
        pid = session_proc.pid
    else:
        pid = load_saved_pid()
    if not pid:
        print("XMRig is not running.")
        save_state(None)
        return None
    print(f"Stopping PID {pid} ...")
    try:
        if session_proc is not None:
            if os.name == "nt":
                session_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(pid, signal.SIGTERM)
            session_proc.wait(timeout=8)
        else:
            # The controller persisted this PID, so it can recover control
            # after a restart without pretending it has a Popen handle.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGTERM)
                deadline = time.time() + 8
                while process_alive(pid) and time.time() < deadline:
                    time.sleep(0.2)
                if process_alive(pid):
                    os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Graceful stop failed: {exc}")
        try:
            if session_proc is not None:
                session_proc.kill()
            elif process_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    save_state(None)
    print("Stopped.")
    return None


def set_value(cfg: dict[str, Any], key: str, value: str) -> None:
    """Set miner parameters from the prompt."""
    p = pool(cfg)
    aliases = {
        "pool": "url", "wallet": "user", "password": "pass",
        "algorithm": "algo", "algo": "algo",
        "thread": "threads", "cpu_threads": "threads",
    }
    raw_key = key.strip().lower()
    key = aliases.get(raw_key, raw_key)
    if raw_key in ALGO_ALIASES and not value.strip():
        value = raw_key

    if key == "algo":
        requested = " ".join(value.strip().lower().split())
        canonical = ALGO_ALIASES.get(requested)
        if canonical is None:
            print("Unsupported algorithm. Use: rx/0 (RandomX)")
            return
        p["algo"] = canonical
        save_json(CONFIG_PATH, cfg)
        print(f"Set algo = {canonical}")
        return

    if key == "threads":
        try:
            n = int(value.strip())
            if n < 0 or n > 256:
                raise ValueError
        except ValueError:
            print("threads must be 0 (auto) or an integer from 1-256")
            return
        cfg["threads"] = n
        save_json(CONFIG_PATH, cfg)
        print(f"Set threads = {n if n else 'auto'}")
        return

    key = aliases.get(key, key)
    if key not in {"url", "user", "pass", "algo", "coin", "tls", "keepalive", "threads"}:
        print("Settable keys: pool, wallet, password, algorithm, coin, tls, keepalive, threads")
        return
    if key in {"tls", "keepalive"}:
        value = value.lower() in {"1", "true", "yes", "on"}
    if key == "coin" and value.strip().lower() in {"", "auto", "none", "null"}:
        p.pop("coin", None)
        save_json(CONFIG_PATH, cfg)
        print("Cleared XMRig coin hint (automatic detection).")
        return
    p[key] = value.strip() if isinstance(value, str) else value
    save_json(CONFIG_PATH, cfg)
    print(f"Set {key} = {p[key]}")




def edit_config(cfg: dict[str, Any]) -> None:
    """Interactively edit miner-only settings."""
    p = pool(cfg)
    fields = [
        ("pool", p.get("url", ""), "XMRig pool"),
        ("user", p.get("user", ""), "XMRig wallet/user"),
        ("pass", p.get("pass", "x"), "XMRig password"),
        ("algo", p.get("algo", "rx/0"), "XMRig algorithm"),
        ("coin", p.get("coin", "monero"), "XMRig coin identifier"),
        ("tls", "on" if p.get("tls", False) else "off", "XMRig TLS"),
        ("keepalive", "on" if p.get("keepalive", True) else "off", "XMRig keepalive"),
        ("threads", str(cfg.get("threads", 0) or "auto"), "XMRig threads (0=auto)"),
    ]
    print("Edit config — press Enter to keep the current value.")
    for key, current, label in fields:
        value = input(f"{label} [{current}]: ").strip()
        if value:
            set_value(cfg, key, value)
    save_json(CONFIG_PATH, cfg)
    print(f"Saved {CONFIG_PATH}")


def help_text() -> None:
    print("""
Commands:
  start                         Start XMRig using the managed config
  stop                          Stop XMRig started by this controller
  restart                       Stop then start XMRig
  status                        Show miner/config status
  pool [HOST:PORT]              Set pool endpoint
  wallet [ADDRESS]              Set payout/user address
  password [VALUE]              Set pool password
  algorithm [NAME]              Set supported controller algorithm
  set algo NAME                 Set algorithm
  threads [N]                   Set CPU threads (0=auto, 1-256)
  coin [NAME]                   Set XMRig coin identifier
  tls on|off                    Toggle TLS in generated config
  config                        Print current XMRig JSON config
  edit config                   Interactively edit miner settings
  save                          Save current config
  check algo NAME               Check algorithm alias and XMRig dry-run support
  xmrig [PATH]                  Set XMRig executable path for this session
  debug                         Print validation diagnostics
  help                          Show commands
  quit                          Exit (does not stop a running miner)

Natural aliases: "start mining", "stop mining", "show status",
"configure pool", "set wallet".
""")


def main() -> int:
    global CONFIG_PATH
    ap = argparse.ArgumentParser(description="Prompt-driven XMRig/RandomX controller")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--xmrig", default=None, help="path to xmrig executable")
    args = ap.parse_args()

    CONFIG_PATH = Path(args.config).expanduser().resolve()
    cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not isinstance(cfg.get("pools"), list):
        cfg["pools"] = []
    cfg.pop("organism", None)
    cfg.pop("miner_backend", None)
    pool(cfg)
    proc: subprocess.Popen | None = None
    xmrig = args.xmrig
    recovered_pid = load_saved_pid()

    print("CYX Prompt Auto — type `help` for commands.")
    if recovered_pid:
        print(f"Recovered controller state: XMRig PID {recovered_pid} is still alive; `stop` can terminate the recovered process.")
    status(cfg, proc)
    while True:
        try:
            raw = input("\ncyx> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            print(f"Input error: {exc}")
            continue
        cmd = parts[0].lower()
        args2 = parts[1:]
        normalized = raw.lower()

        if cmd in {"quit", "exit", "q"}:
            break
        if cmd in {"help", "?"}:
            help_text(); continue
        if cmd == "start" or "start mining" in normalized:
            proc = start(cfg, proc, xmrig); continue
        if cmd == "stop" or "stop mining" in normalized:
            proc = stop(proc); continue
        if cmd == "restart":
            proc = stop(proc); proc = start(cfg, proc, xmrig); continue
        if cmd == "status" or "show status" in normalized:
            status(cfg, proc); continue
        if cmd == "config":
            print(json.dumps(cfg, indent=2)); continue
        if (cmd == "edit" and args2 and args2[0].lower() == "config") or cmd == "edit-config":
            edit_config(cfg); continue
        if cmd == "save":
            save_json(CONFIG_PATH, cfg); print(f"Saved {CONFIG_PATH}"); continue
        if cmd in {"check", "test"} and args2 and args2[0].lower() in {"algo", "algorithm"}:
            name = " ".join(args2[1:])
            if not name:
                print("Usage: check algo NAME")
            else:
                algorithm_diagnostic(name, xmrig)
            continue
        if cmd == "xmrig":
            if args2: xmrig = args2[0]
            print(f"XMRig path: {xmrig or find_xmrig() or 'not found'}"); continue
        if cmd in {"pool", "wallet", "password", "algorithm", "coin", "tls", "keepalive", "threads"}:
            key = "pool" if cmd == "pool" else cmd
            value = args2[0] if args2 else input(f"{key}: ").strip()
            if cmd in {"tls", "keepalive"} and not args2:
                value = input(f"{key} on/off: ").strip()
            set_value(cfg, key, value); continue
        if cmd == "set" and len(args2) >= 2:
            set_value(cfg, args2[0], " ".join(args2[1:]))
            continue
        if cmd == "set" and len(args2) == 1 and args2[0].lower() in ALGO_ALIASES:
            set_value(cfg, "algo", args2[0])
            continue
        if cmd == "debug":
            print("Validation:")
            errors = validate(cfg)
            print("  OK" if not errors else "  " + "\n  ".join(errors))
            print(f"XMRig discovered: {find_xmrig(xmrig) or 'NO'}")
            print(f"Config path: {CONFIG_PATH}")
            continue
        print("Unknown command. Type `help`.")

    print("Exiting controller. Any XMRig process already started is left running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
