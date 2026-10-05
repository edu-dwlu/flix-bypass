"""
FELIX DZHQ BYPASS API — MULTI-ACCOUNT + ADMIN PANEL + NICK BOT + ALEX API
Developer: Mrr_unknown
Version: 2.0

Features:
- Multi-account round-robin bypass
- /admin panel (password: felix56)
- Add/Remove accounts
- Update session string (no restart needed)
- Pause/Resume per account or all
- Live stats per account
- NEVER CRASHES — even if default session fails!
- PERSISTENCE FIX — accounts survive server restarts!
- Nick_Bypass_Bot DM support (2-way race)
- Alex Bypass API used as the primary URL resolver
- Bot toggles (DZHQ / Nick / Alex on-off from admin panel)
- DZHQ First Mode — DZHQ runs first; Nick only activates if DZHQ fails

HOW TO RUN:
  python bot.py
  (packages install automatically on first run)

SETUP:
  Open /admin to add Telegram accounts if Telegram fallback is needed.

BYPASS:
  GET /bypass?link=YOUR_LINK
"""

# ══════════════════════════════════════════════════════════════
#  SETUP — loads .env automatically
# ══════════════════════════════════════════════════════════════
import sys, os, subprocess

def _load_dotenv(path=".env"):
    """Load key=value pairs from .env file into os.environ."""
    if not os.path.exists(path):
        # Create .env.example for the user
        example = ".env.example"
        if not os.path.exists(example):
            with open(example, "w") as f:
                f.write(
                    "# Admin panel password (default: felix56)\n"
                    "ADMIN_PASSWORD=felix56\n\n"
                    "# Flask secret key\n"
                    "SECRET_KEY=change_this_to_random_string\n"
                )
            print(f"ℹ️  Created {example} — copy it to .env and fill your values\n")
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:   # don't override existing env vars
                os.environ[k] = v
    print(f"✅ Loaded {path}\n")

_load_dotenv()
# ══════════════════════════════════════════════════════════════

from flask import Flask, request, jsonify, session as flask_session, redirect, render_template_string, send_file
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
import asyncio
import threading
import time
import re
import logging
import os
import secrets
import json as _json
import uuid
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import wraps

# ==================== CONFIG ====================
ADMIN_PASSWORD  = 'felix56'

# Group 1 — DZHQ
DZHQ_BOT        = "@DZHQ_BypassBot"
DZHQ_GROUP      = -1003644908415

# Group 2 — Nick (DM mode)
NICK_BOT        = "@Nick_Bypass_Bot"

# Group 3 — Alex Bypass Bot (DM mode) — races against the Alex HTTP API
ALEX_BOT        = "@alexbypassbot"

DEVELOPER       = "Mrr Unknown"
PORT            = int(os.environ.get('PORT', 5000))
SECRET_KEY      = (
    os.environ.get("SECRET_KEY")
    or os.environ.get("SESSION_SECRET")
    or secrets.token_hex(32)
)
ACCOUNTS_FILE   = "accounts_data.json"

# Alex API is only used for the two supported short-link domains.
ALEX_BYPASS_API = os.environ.get(
    "ALEX_BYPASS_API",
    "https://alexbypassapi.up.railway.app/bypass?url=",
).strip()
ALEX_TIMEOUT_SEC = float(os.environ.get("ALEX_TIMEOUT_SEC", "20"))
ALEX_ALLOWED_HOST_PARTS = ("urlking", "monteolympus")
# @alexbypassbot can take 150s+ on slow links (tipsguru). This default must
# stay above its worst real latency + margin; the shared absolute budget is
# min(ALEX_RACE_MAX_TIMEOUT_SEC, max(...configured...) + 5).
ALEX_BOT_TIMEOUT_SEC = float(os.environ.get("ALEX_BOT_TIMEOUT_SEC", "180"))
# Alex route toggle — alex branch ONLY (DZHQ/Nick never reach the Alex code).
# race = API + @alexbypassbot compete (default) · bot = bot only, skip the API
# api = Alex HTTP API only, no Telegram DM is sent.
ALEX_ROUTE = os.environ.get("ALEX_ROUTE", "race").strip().lower()
if ALEX_ROUTE not in ("race", "bot", "api"):
    ALEX_ROUTE = "race"
# Only silence triggers this timeout. Progress/loading messages refresh it.
BYPASS_IDLE_TIMEOUT_SEC = float(os.environ.get("BYPASS_IDLE_TIMEOUT_SEC", "30"))
# Post-contact patience for @alexbypassbot: once it has sent at least one
# message, long silent "cracking" phases are normal — allow this much quiet
# before giving up. Before first contact the strict BYPASS_IDLE_TIMEOUT_SEC
# still applies so dead bots fail fast. DZHQ/Nick never use this value.
ALEX_BOT_IDLE_TIMEOUT_SEC = float(os.environ.get("ALEX_BOT_IDLE_TIMEOUT_SEC", "120"))
# A non-positive per-bot timeout must never turn an HTTP request into an
# unbounded wait. These caps are only used when a bot/API does not answer.
ALEX_RACE_MAX_TIMEOUT_SEC = max(
    30.0, float(os.environ.get("ALEX_RACE_MAX_TIMEOUT_SEC", "210"))
)
MAX_BYPASS_TIMEOUT_SEC = max(
    30.0, float(os.environ.get("MAX_BYPASS_TIMEOUT_SEC", "120"))
)

# ===== FELIX OSINT ADDITIVE PATCH =====
# OSINT bridge is intentionally separate from the existing bypass state.
# It uses the same Telethon accounts, but its own round-robin pointer so the
# existing bypass round_robin_idx and selection logic are not changed.
OSINT_ENABLED = os.environ.get("OSINT_ENABLED", "0").lower() in ("1", "true", "yes", "on")
OSINT_GROUP = int(os.environ.get("OSINT_GROUP", "-1003622891965"))
OSINT_TIMEOUT_SEC = max(5.0, float(os.environ.get("OSINT_TIMEOUT_SEC", "45")))
OSINT_API_KEY = os.environ.get("OSINT_API_KEY", "").strip()
OSINT_SERVICE_USER_ID = os.environ.get("OSINT_SERVICE_USER_ID", "").strip()
OSINT_ALLOWED_COMMANDS = {
    x.strip().lower().lstrip("/")
    for x in os.environ.get("OSINT_ALLOWED_COMMANDS", "num,tg").split(",")
    if x.strip()
}

# Multiple OSINT groups (same commands work in every group). Comma-separated
# group IDs, e.g. OSINT_GROUPS="-100111,-100222". OSINT_GROUP remains the
# default (first) group for backward compatibility.
def _parse_osint_groups():
    groups = []
    for x in os.environ.get("OSINT_GROUPS", "").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            groups.append(int(x))
        except ValueError:
            print(f"⚠️ Ignoring invalid OSINT_GROUPS entry: {x!r}")
    return groups or [OSINT_GROUP]

OSINT_GROUPS = _parse_osint_groups()

# Fixed per-command routing, e.g. OSINT_COMMAND_GROUPS="num:-100111,tg:-100222".
# Values accept a full group ID or a 1-based index into OSINT_GROUPS
# ("num:1,tg:2"). Commands without a mapping use the first group.
def _parse_osint_command_groups():
    mapping = {}
    for part in os.environ.get("OSINT_COMMAND_GROUPS", "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        cmd, _, val = part.partition(":")
        cmd, val = cmd.strip().lower().lstrip("/"), val.strip()
        if cmd and val:
            mapping[cmd] = val
    return mapping

OSINT_COMMAND_GROUPS = _parse_osint_command_groups()

# Per-group responders: "group_id:responder_id" pairs, e.g.
# OSINT_GROUP_RESPONDERS="-100111:12345,-100222:67890".
# Each group can have a DIFFERENT responder bot. Groups without an entry fall
# back to OSINT_SERVICE_USER_ID, then to accepting any sender.
def _parse_osint_group_responders():
    mapping = {}
    for part in os.environ.get("OSINT_GROUP_RESPONDERS", "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        gval, _, sval = part.partition(":")
        try:
            gid = int(gval.strip())
            sid = int(sval.strip())
        except (TypeError, ValueError):
            print(f"⚠️ Ignoring invalid OSINT_GROUP_RESPONDERS entry: {part!r}")
            continue
        mapping[gid] = sid
    return mapping

OSINT_GROUP_RESPONDERS = _parse_osint_group_responders()

# Backend-level default when the caller passes only {"command": ...} with no
# mode/group — i.e. the frontend never needs to know about routing.
# "single": fixed per-command group (OSINT_COMMAND_GROUPS, else first group).
# "race":   fan out to all OSINT_GROUPS, first reply wins.
OSINT_DEFAULT_MODE = os.environ.get("OSINT_DEFAULT_MODE", "single").strip().lower()
if OSINT_DEFAULT_MODE not in ("single", "race"):
    print(f"⚠️ Invalid OSINT_DEFAULT_MODE {OSINT_DEFAULT_MODE!r}; using 'single'")
    OSINT_DEFAULT_MODE = "single"


def _resolve_osint_group(value):
    """Resolve a group selector (full ID or 1-based index) to a group ID."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        gid = int(float(s)) if "." in s else int(s)
    except (TypeError, ValueError):
        return None
    if gid in OSINT_GROUPS:
        return gid
    if 1 <= gid <= len(OSINT_GROUPS):
        return OSINT_GROUPS[gid - 1]
    return None


def _default_osint_group(command_name):
    """Fixed per-command group, else the first configured group."""
    mapped = OSINT_COMMAND_GROUPS.get((command_name or "").lower())
    resolved = _resolve_osint_group(mapped) if mapped else None
    return resolved if resolved is not None else OSINT_GROUPS[0]


def _osint_group_queue(acc_state, group):
    """Per (account, group) FIFO queue — replies must never cross groups."""
    return acc_state.setdefault("osint_group_queues", {}).setdefault(group, [])


def _osint_group_lock(acc_state, group):
    """Per (account, group) in-flight lock."""
    locks = acc_state.setdefault("osint_group_locks", {})
    lock = locks.get(group)
    if lock is None:
        lock = locks[group] = threading.Lock()
    return lock


# ===== FELIX OSINT ADMIN ROUTING =====
# Routing (race vs fixed group, per command) is managed from the /admin panel
# and persisted to disk, so API callers only ever pass {"command": "..."}.
# Env vars below are first-run bootstrap defaults only.
OSINT_SETTINGS_FILE = os.environ.get(
    "OSINT_SETTINGS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "osint_settings.json"),
)

osint_settings = {
    "default_mode": OSINT_DEFAULT_MODE,  # single | race
    "command_modes": {},    # {"tg": "race"} — per-command mode override
    "command_groups": {},   # {"num": [-100111]} — per-command group list override
    "group_responders": {},  # {-100111: 12345} — responder numeric ID per group
    "disabled_commands": [],  # allowlisted commands rejected with 403
    "timeout_sec": None,    # global override, None = OSINT_TIMEOUT_SEC env
    "command_timeouts": {},  # {"num": 60} — per-command timeout override
}
_osint_settings_lock = threading.Lock()


def _osint_settings_snapshot():
    """Thread-safe copy of the routing config plus UI metadata."""
    with _osint_settings_lock:
        cfg = {
            "default_mode": osint_settings["default_mode"],
            "command_modes": dict(osint_settings["command_modes"]),
            "command_groups": {k: list(v) for k, v in osint_settings["command_groups"].items()},
            "group_responders": dict(osint_settings["group_responders"]),
            "disabled_commands": list(osint_settings["disabled_commands"]),
            "timeout_sec": osint_settings["timeout_sec"],
            "command_timeouts": dict(osint_settings["command_timeouts"]),
        }
    cfg["_meta"] = {
        "groups": list(OSINT_GROUPS),
        "allowed_commands": sorted(OSINT_ALLOWED_COMMANDS),
        "env_timeout_sec": OSINT_TIMEOUT_SEC,
    }
    return cfg


def _save_osint_settings():
    try:
        with _osint_settings_lock:
            payload = {
                "default_mode": osint_settings["default_mode"],
                "command_modes": dict(osint_settings["command_modes"]),
                "command_groups": {k: list(v) for k, v in osint_settings["command_groups"].items()},
                "group_responders": dict(osint_settings["group_responders"]),
                "disabled_commands": list(osint_settings["disabled_commands"]),
                "timeout_sec": osint_settings["timeout_sec"],
                "command_timeouts": dict(osint_settings["command_timeouts"]),
            }
        tmp = OSINT_SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, OSINT_SETTINGS_FILE)
    except Exception as e:
        print(f"⚠️ Could not save OSINT settings: {e}")


def _load_osint_settings():
    """File (admin panel) overrides the env bootstrap. Runs once at startup."""
    # Seed per-command fixed groups + per-group responders from env (first-run).
    with _osint_settings_lock:
        for cmd, val in OSINT_COMMAND_GROUPS.items():
            g = _resolve_osint_group(val)
            if g is not None:
                osint_settings["command_groups"].setdefault(cmd, [g])
        for gid, sid in OSINT_GROUP_RESPONDERS.items():
            osint_settings["group_responders"].setdefault(gid, sid)
    if not os.path.exists(OSINT_SETTINGS_FILE):
        return
    try:
        with open(OSINT_SETTINGS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("settings root must be an object")
        ok, err = _validate_osint_settings(data, partial=True)
        if not ok:
            print(f"⚠️ Ignoring invalid {OSINT_SETTINGS_FILE}: {err}")
            return
        with _osint_settings_lock:
            if "default_mode" in data:
                osint_settings["default_mode"] = data["default_mode"]
            if "command_modes" in data:
                osint_settings["command_modes"] = data["command_modes"]
            if "group_responders" in data:
                osint_settings["group_responders"] = {int(k): int(v) for k, v in data["group_responders"].items()}
            if "command_groups" in data:
                osint_settings["command_groups"] = {k: list(v) for k, v in data["command_groups"].items()}
            if "disabled_commands" in data:
                osint_settings["disabled_commands"] = list(data["disabled_commands"])
            if "timeout_sec" in data:
                osint_settings["timeout_sec"] = data["timeout_sec"]
            if "command_timeouts" in data:
                osint_settings["command_timeouts"] = data["command_timeouts"]
        print(f"💾 Loaded OSINT routing from {OSINT_SETTINGS_FILE}")
    except Exception as e:
        print(f"⚠️ Could not load OSINT settings: {e}")


def _validate_osint_settings(data, partial=False):
    """Validate an admin settings payload. Returns (ok, error)."""
    if not isinstance(data, dict):
        return False, "Body must be a JSON object"
    allowed_keys = {"default_mode", "command_modes", "command_groups",
                    "group_responders", "disabled_commands", "timeout_sec",
                    "command_timeouts"}
    for k in data:
        if k not in allowed_keys:
            return False, f"Unknown key: {k}"
    with _osint_settings_lock:
        current = {
            "default_mode": osint_settings["default_mode"],
            "command_modes": dict(osint_settings["command_modes"]),
            "command_groups": {k: list(v) for k, v in osint_settings["command_groups"].items()},
        }
    merged = dict(current)
    if "default_mode" in data:
        if data["default_mode"] not in ("single", "race"):
            return False, "default_mode must be 'single' or 'race'"
        merged["default_mode"] = data["default_mode"]
    if "command_modes" in data:
        if not isinstance(data["command_modes"], dict):
            return False, "command_modes must be an object"
        for cmd, m in data["command_modes"].items():
            if m not in ("single", "race"):
                return False, f"command_modes[{cmd}] must be 'single' or 'race'"
        merged["command_modes"] = {str(k).strip().lower().lstrip("/"): v for k, v in data["command_modes"].items()}
    if "command_groups" in data:
        if not isinstance(data["command_groups"], dict):
            return False, "command_groups must be an object"
        cleaned = {}
        for cmd, groups in data["command_groups"].items():
            if not isinstance(groups, list) or not groups:
                return False, f"command_groups[{cmd}] must be a non-empty array of group IDs"
            seen, out = set(), []
            for g in groups:
                try:
                    gid = int(g)
                except (TypeError, ValueError):
                    return False, f"command_groups[{cmd}] has invalid group ID: {g!r}"
                if gid not in OSINT_GROUPS:
                    return False, f"command_groups[{cmd}]: group {gid} is not in OSINT_GROUPS"
                if gid not in seen:
                    seen.add(gid)
                    out.append(gid)
            cleaned[str(cmd).strip().lower().lstrip("/")] = out
        merged["command_groups"] = cleaned
    if "group_responders" in data:
        if not isinstance(data["group_responders"], dict):
            return False, "group_responders must be an object"
        cleaned_resp = {}
        for k, v in data["group_responders"].items():
            try:
                gid = int(str(k).strip())
            except (TypeError, ValueError):
                return False, f"group_responders has invalid group ID: {k!r}"
            if gid not in OSINT_GROUPS:
                return False, f"group_responders: group {gid} is not in OSINT_GROUPS"
            if v is None or (isinstance(v, str) and not v.strip()):
                continue  # blank = accept any sender in that group
            try:
                sid = int(str(v).strip())
            except (TypeError, ValueError):
                return False, f"group_responders[{gid}] has invalid responder ID: {v!r}"
            cleaned_resp[gid] = sid
        data["group_responders"] = cleaned_resp
    if "disabled_commands" in data:
        if not isinstance(data["disabled_commands"], list):
            return False, "disabled_commands must be an array"
        data["disabled_commands"] = [str(x).strip().lower().lstrip("/") for x in data["disabled_commands"] if str(x).strip()]
    if "timeout_sec" in data:
        if data["timeout_sec"] is not None:
            try:
                t = float(data["timeout_sec"])
            except (TypeError, ValueError):
                return False, "timeout_sec must be a number or null"
            if t < 5:
                return False, "timeout_sec must be >= 5"
            data["timeout_sec"] = t
    if "command_timeouts" in data:
        if not isinstance(data["command_timeouts"], dict):
            return False, "command_timeouts must be an object"
        cleaned = {}
        for cmd, t in data["command_timeouts"].items():
            try:
                t = float(t)
            except (TypeError, ValueError):
                return False, f"command_timeouts[{cmd}] must be a number"
            if t < 5:
                return False, f"command_timeouts[{cmd}] must be >= 5"
            cleaned[str(cmd).strip().lower().lstrip("/")] = t
        data["command_timeouts"] = cleaned
    # Cross-check: single mode with several groups is ambiguous — reject it.
    probed = set(OSINT_ALLOWED_COMMANDS) | set(merged["command_modes"]) | set(merged["command_groups"])
    for cmd in probed:
        eff_mode = merged["command_modes"].get(cmd, merged["default_mode"])
        eff_groups = merged["command_groups"].get(cmd)
        if eff_mode == "single" and eff_groups and len(eff_groups) > 1:
            return False, f"/{cmd}: single mode with {len(eff_groups)} groups is ambiguous — use race or one group"
    return True, ""


def _resolve_osint_routing(command_name, group_param="", mode_param=""):
    """Resolve (target_groups, mode, error, http_status).

    Priority: explicit request params > admin panel config > env defaults,
    so plain {"command": "..."} calls are fully admin-driven.
    """
    with _osint_settings_lock:
        cfg_modes = dict(osint_settings["command_modes"])
        cfg_groups = {k: list(v) for k, v in osint_settings["command_groups"].items()}
        cfg_default = osint_settings["default_mode"]
        cfg_disabled = set(osint_settings["disabled_commands"])
    if command_name in cfg_disabled:
        return None, None, f"Command /{command_name} is disabled by admin", 403
    if group_param:
        wanted = [g for g in (_resolve_osint_group(x) for x in str(group_param).split(",")) if g is not None]
        if not wanted:
            return None, None, f"Unknown OSINT group: {group_param}", 400
        mode = (mode_param or "").strip().lower() or cfg_modes.get(command_name) or cfg_default
        if mode not in ("single", "race"):
            return None, None, "Invalid mode: use 'single' or 'race'", 400
        if mode == "single" and len(wanted) > 1:
            return None, None, "Single mode accepts exactly one group; use mode=race for several", 400
        return wanted, mode, "", 200
    mode = (mode_param or "").strip().lower() or cfg_modes.get(command_name) or cfg_default
    if mode not in ("single", "race"):
        return None, None, "Invalid mode: use 'single' or 'race'", 400
    groups = [g for g in cfg_groups.get(command_name, []) if g in OSINT_GROUPS]
    if cfg_groups.get(command_name) and not groups:
        return None, None, f"No valid OSINT group configured for /{command_name}", 400
    if groups:
        return (groups[:1] if mode == "single" else groups), mode, "", 200
    if mode == "race":
        return list(OSINT_GROUPS), mode, "", 200
    return [_default_osint_group(command_name)], mode, "", 200


def _osint_timeout_for(command_name):
    """Effective wait budget: per-command > global admin > env."""
    with _osint_settings_lock:
        t = osint_settings["command_timeouts"].get(command_name)
        if t is None:
            t = osint_settings["timeout_sec"]
    if t is None:
        return OSINT_TIMEOUT_SEC
    try:
        return max(5.0, float(t))
    except (TypeError, ValueError):
        return OSINT_TIMEOUT_SEC


def _osint_responder_for(group):
    """Responder numeric ID for a group: admin map > global env > None (any)."""
    with _osint_settings_lock:
        sid = osint_settings["group_responders"].get(group)
    if sid is not None:
        return sid
    if OSINT_SERVICE_USER_ID:
        try:
            return int(OSINT_SERVICE_USER_ID)
        except (TypeError, ValueError):
            return None
    return None


_load_osint_settings()

_osint_round_robin_idx = 0
_osint_rr_lock = threading.Lock()

TRACE_BOTS = os.environ.get("TRACE_BOTS", "0").lower() in ("1", "true", "yes", "on")

def _trace(label, message):
    if TRACE_BOTS:
        print(f"[TRACE][{label}] {message}", flush=True)

# ── Bot toggle settings ────────────────────────────────────────────────────────
# Set to False to disable a bot entirely (it won't be sent to or waited on)
bot_settings        = {"dzhq": True, "nick": True, "dzhq_first": False,
                       "nick_first": False, "random_mode": False, "alex_bot": True,
                       "alex_route": ALEX_ROUTE}

# ── Random Mode state ──────────────────────────────────────────────────────────
# Random Mode: ek bot par lagataar N requests (random 3-10), phir doosre bot par
# switch. Dono bots aapas me baari-baari kaam karte hain.
RANDOM_BATCH_MIN = 3
RANDOM_BATCH_MAX = 10
_random_state = {"bot": None, "left": 0, "batch": 0}
_random_lock  = threading.Lock()

def _random_pick_bot():
    """Return the bot ('dzhq'/'nick') that should handle this request in Random Mode."""
    import random as _rnd
    with _random_lock:
        if _random_state["bot"] is None or _random_state["left"] <= 0:
            prev = _random_state["bot"]
            nxt  = "nick" if prev == "dzhq" else ("dzhq" if prev == "nick" else _rnd.choice(["dzhq", "nick"]))
            size = _rnd.randint(RANDOM_BATCH_MIN, RANDOM_BATCH_MAX)
            _random_state["bot"]   = nxt
            _random_state["left"]  = size
            _random_state["batch"] = size
            print(f"[RandomMode] switched to {nxt.upper()} for next {size} link(s)", flush=True)
        _random_state["left"] -= 1
        return _random_state["bot"]

def _random_status():
    with _random_lock:
        return {"current": _random_state["bot"], "remaining": max(0, _random_state["left"]),
                "batch": _random_state["batch"]}

def _set_exclusive_mode(mode):
    """Only one of dzhq_first / nick_first / random_mode can be ON at a time."""
    for m in ("dzhq_first", "nick_first", "random_mode"):
        bot_settings[m] = (m == mode)

# ── Bypass priority rules ─────────────────────────────────────────────────
# Ordered {pattern → target} rules. The first rule whose pattern appears in
# the link (case-insensitive substring) wins, and that resolver runs
# STRICTLY — no fallback to the other bots. Example: tipsguru.in → alex_bot
# sends every tipsguru link to @alexbypassbot, even though tipsguru is not an
# Alex-branch domain.
BYPASS_PRIORITY_TARGETS = ("dzhq", "nick", "alex_bot", "alex_api", "alex_race")
BYPASS_SETTINGS_FILE = os.environ.get("BYPASS_SETTINGS_FILE", "bypass_settings.json")
_MAX_BYPASS_PRIORITIES = 50

def _parse_bypass_priorities(raw):
    """Parse BYPASS_PRIORITIES env: 'tipsguru.in:alex_bot,foo.com:nick'."""
    rules = []
    seen = set()
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        pattern, _, target = item.rpartition(":")
        pattern = pattern.strip()
        target = target.strip().lower()
        if not pattern or target not in BYPASS_PRIORITY_TARGETS:
            print(f"[Priorities] ignoring invalid env entry: {item!r}", flush=True)
            continue
        if pattern.lower() in seen:
            continue
        seen.add(pattern.lower())
        rules.append({"pattern": pattern, "target": target})
    return rules

bypass_settings = {"priorities": _parse_bypass_priorities(os.environ.get("BYPASS_PRIORITIES", ""))}
_bypass_settings_lock = threading.Lock()

def _validate_bypass_priorities(data):
    if not isinstance(data, dict) or not isinstance(data.get("priorities"), list):
        return False, "body must be {priorities: [{pattern, target}]}"
    rules = data["priorities"]
    if len(rules) > _MAX_BYPASS_PRIORITIES:
        return False, f"too many rules (max {_MAX_BYPASS_PRIORITIES})"
    cleaned, seen = [], set()
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            return False, f"rule #{i+1} must be an object"
        pattern = str(r.get("pattern", "")).strip()
        target = str(r.get("target", "")).strip().lower()
        if not pattern:
            return False, f"rule #{i+1} has an empty pattern"
        if len(pattern) > 200:
            return False, f"rule #{i+1} pattern is too long (max 200 chars)"
        if target not in BYPASS_PRIORITY_TARGETS:
            return False, f"rule #{i+1} has invalid target — use: {', '.join(BYPASS_PRIORITY_TARGETS)}"
        if pattern.lower() in seen:
            return False, f"duplicate pattern: {pattern!r}"
        seen.add(pattern.lower())
        cleaned.append({"pattern": pattern, "target": target})
    data["priorities"] = cleaned
    return True, None

def _save_bypass_settings():
    try:
        tmp = BYPASS_SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"priorities": bypass_settings["priorities"]}, f)
        os.replace(tmp, BYPASS_SETTINGS_FILE)
    except Exception as e:
        print(f"[Priorities] save failed: {e}", flush=True)

def _load_bypass_settings():
    try:
        with open(BYPASS_SETTINGS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[Priorities] load failed ({e}); using env bootstrap", flush=True)
        return
    ok, err = _validate_bypass_priorities(data)
    if not ok:
        print(f"[Priorities] settings file invalid ({err}); using env bootstrap", flush=True)
        return
    with _bypass_settings_lock:
        bypass_settings["priorities"] = data["priorities"]

_load_bypass_settings()

def _match_bypass_priority(link):
    """First rule whose pattern appears in the link (case-insensitive)."""
    lowered = link.lower()
    with _bypass_settings_lock:
        rules = list(bypass_settings["priorities"])
    for rule in rules:
        if rule["pattern"].lower() in lowered:
            return rule
    return None

def _bypass_priorities_snapshot():
    with _bypass_settings_lock:
        rules = [dict(r) for r in bypass_settings["priorities"]]
    return {"success": True, "priorities": rules,
            "_meta": {"targets": list(BYPASS_PRIORITY_TARGETS)}}

def _run_bypass_priority(rule, link):
    """Run a matched priority rule STRICTLY. Returns (winner|None, error|None).

    dzhq/nick targets are NOT run here — the caller restricts the normal
    Telegram flow to that single bot via its use_dzhq/use_nick flags.
    """
    target = rule["target"]
    if target == "alex_api":
        if not ALEX_BYPASS_API:
            return None, "Alex API is not configured"
        res, err = alex_bypass(link)
        if res:
            return {"source": "alex", "url": res["bypassed"],
                    "extra": {"module": res.get("raw_type", "alex_api"),
                             "account": "alex_api"}}, None
        return None, err
    if target == "alex_bot":
        if not bot_settings.get("alex_bot", True):
            return None, "Alex DM bot is disabled — enable it in /admin"
        r, err = _alex_bot_roundtrip(link)
        if r:
            return {"source": "alex_bot", "url": r["bypassed"],
                    "extra": {"module": "alex_bot", "account": r["account"]}}, None
        return None, err or "Alex bot gave no result"
    if target == "alex_race":
        return alex_race(link)
    return None, f"unsupported priority target: {target}"

# Public aliases for priority targets — end users must never see internal
# bot names or raw resolver dumps. Owners get the full error in server logs.
BYPASS_TARGET_ALIASES = {
    "dzhq":      "dz_q",
    "nick":      "nk_b",
    "alex_bot":  "ax_b",
    "alex_api":  "ax_a",
    "alex_race": "ax_r",
}

def _priority_public_message(rule):
    """Short end-user message for a failed priority rule (no raw dumps)."""
    alias = BYPASS_TARGET_ALIASES.get((rule or {}).get("target"), "srv")
    return (f"{(rule or {}).get('pattern')} priority set bot i.e. {alias} "
            f"failed — please try another link.")

# Async requests are kept separate from the synchronous /bypass endpoint.  This
# prevents a slow resolver (especially a Telegram bot) from making the caller's
# HTTP request time out before the final Telegram message arrives.
_jobs = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 1000


def _flow_update(job_id, status, message, **extra):
    """Append a small, client-friendly status message to an async job."""
    if not job_id:
        return
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["status"] = status
        job["message"] = message
        job["updated_at"] = time.time()
        if "result" in extra:
            job["result"] = extra["result"]
        event = {"status": status, "message": message, "at": job["updated_at"]}
        event.update(extra)
        job["flow"].append(event)
        # Keep memory bounded even when a client never polls the job.
        if len(job["flow"]) > 50:
            del job["flow"][:-50]

logging.basicConfig(level=logging.WARNING)
logging.getLogger('telethon').setLevel(logging.WARNING)

# ==================== ALEX API ====================
def alex_bypass(link):
    """Resolve a URL through the user-configured Alex Bypass API."""
    if not ALEX_BYPASS_API:
        return None, "Alex API is not configured"

    encoded_link = urllib.parse.quote(link, safe="")
    if ALEX_BYPASS_API.endswith("url="):
        target = f"{ALEX_BYPASS_API}{encoded_link}"
    else:
        separator = "&" if "?" in ALEX_BYPASS_API else "?"
        target = f"{ALEX_BYPASS_API}{separator}url={encoded_link}"
    req = urllib.request.Request(
        target,
        headers={
            "Accept": "application/json",
            "User-Agent": "Felix-Bypass-API/3.0",
        },
        method="GET",
    )

    try:
        # Never pass None here: urllib treats it as an unlimited network wait.
        api_timeout = (
            ALEX_TIMEOUT_SEC
            if ALEX_TIMEOUT_SEC > 0
            else ALEX_RACE_MAX_TIMEOUT_SEC
        )
        with urllib.request.urlopen(req, timeout=api_timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
            _trace("ALEX_API", f"response for {link}: {str(payload)[:500]}")
    except urllib.error.HTTPError as exc:
        return None, f"Alex API HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"Alex API unavailable: {exc.reason}"
    except TimeoutError:
        return None, "Alex API request timed out"
    except (ValueError, json.JSONDecodeError):
        return None, "Alex API returned invalid JSON"
    except Exception as exc:
        return None, f"Alex API error: {exc}"

    if not isinstance(payload, dict):
        return None, "Alex API returned an unexpected response"
    if str(payload.get("status", "")).lower() not in ("success", "ok", "true", "1"):
        return None, str(payload.get("message") or payload.get("error") or "Alex API could not bypass this URL")

    result_url = payload.get("url") or payload.get("bypassed") or payload.get("result")
    if isinstance(result_url, list):
        result_url = result_url[0] if result_url else None
    if not isinstance(result_url, str) or not result_url.startswith(("http://", "https://")):
        return None, "Alex API response did not contain a valid result URL"

    return {
        "status": "ok",
        "original": link,
        "bypassed": result_url,
        "raw_type": payload.get("module", "alex"),
        "alex_response": payload,
    }, None


def _should_use_alex(link):
    """Return True only for URLKing and MonteOlympus links."""
    try:
        hostname = (urllib.parse.urlsplit(link).hostname or "").lower()
    except ValueError:
        return False
    return any(part in hostname for part in ALEX_ALLOWED_HOST_PARTS)


# ==================== ACCOUNTS STORE ====================
accounts        = {}
accounts_lock   = threading.Lock()
round_robin_idx = 0
rr_lock         = threading.Lock()
admin_tokens    = set()


# ==================== PERSISTENCE ====================
def save_accounts_to_file():
    try:
        data = []
        with accounts_lock:
            for acc in accounts.values():
                data.append({
                    "id":             acc["id"],
                    "name":           acc["name"],
                    "api_id":         acc["api_id"],
                    "api_hash":       acc["api_hash"],
                    "session_string": acc["session_string"],
                })
        with open(ACCOUNTS_FILE, "w") as f:
            _json.dump(data, f, indent=2)
        print(f"💾 Saved {len(data)} accounts to {ACCOUNTS_FILE}")
    except Exception as e:
        print(f"⚠️ Could not save accounts: {e}")

def load_accounts_from_file():
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"ℹ️ No saved accounts file found ({ACCOUNTS_FILE})")
        return
    try:
        with open(ACCOUNTS_FILE, "r") as f:
            data = _json.load(f)
        count = 0
        for entry in data:
            try:
                acc_id = entry["id"]
                with accounts_lock:
                    accounts[acc_id] = _make_acc_dict(
                        acc_id, entry["name"],
                        int(entry["api_id"]), entry["api_hash"], entry["session_string"]
                    )
                _start_account_thread(acc_id)
                count += 1
                print(f"✅ Loaded saved account: {entry['name']} ({acc_id})")
            except Exception as e:
                print(f"⚠️ Could not load account {entry.get('name', '?')}: {e}")
        print(f"💾 Loaded {count} accounts from {ACCOUNTS_FILE}")
    except Exception as e:
        print(f"⚠️ Could not load accounts file: {e}")


def _make_acc_dict(acc_id, name, api_id, api_hash, session_string):
    return {
        "id":             acc_id,
        "name":           name,
        "api_id":         int(api_id),
        "api_hash":       api_hash,
        "session_string": session_string,
        "status":         "connecting",
        "bypass_count":   0,
        "success_count":  0,
        "fail_count":     0,
        "last_used":      None,
        "error_message":  None,
        "client":         None,
        "loop":           None,
        "thread":         None,
        "dzhq_bot_id":    None,
        "nick_bot_id":    None,
        "alex_bot_id":    None,
        "pending":        {},
        "nick_dm_queue":  [],   # FIFO queue for Nick DM requests
        "nick_dm_lock":   threading.Lock(),
        "alex_dm_queue":  [],   # FIFO queue for Alex bot DM requests
        "alex_dm_lock":   threading.Lock(),


        # OSINT group state — additive; existing bypass state is untouched.
        "osint_queue":     [],
        "osint_lock":      threading.Lock(),
        "osint_group_queues": {},
        "osint_group_locks":  {},
        "osint_count":     0,
        "osint_success_count": 0,
        "osint_fail_count":    0,
        "last_osint_used": None,
        "username":       None,
        "first_name":     None,
    }


# ==================== DEFAULT ACCOUNT ====================
_default_loaded = False

def load_default_account():
    global _default_loaded
    if _default_loaded:
        return
    _default_loaded = True
    try:
        API_ID = int(os.environ.get("TELEGRAM_API_ID", "30605082"))
        API_HASH = os.environ.get("TELEGRAM_API_HASH", "8c8ea983caab6da3ca4ce1e901dba229").strip()
        SESSION_STR = os.environ.get("TELEGRAM_SESSION", "1BVtsOIEBuzC73J_y33yll45vlL7EAqjQQh2PKd3CzYTywGTtZLrkzLHbjg5g-ZYnj1NCf09NsSk1DyATDJJTsZfE90atSwwJ0A1GNfrjGnJopL7gpS_2X71wwfiMDtMnl14CrcPQ1kgrDfkgiWl00VuJ65d7ovNYVGF5AoizmYOLbGOn7i6qs3nP7csi2U7T6dwDenqmofQz30RBc_qu_FC6XkNhbHDm2VmVChhyaRNmiOSnON0DQw6KjOvDaVo8i1JAWbmjs1aczRgMKhTOruthlhw7S2EuCwaZFgJVz8W8iHdrBT88mjinrft18h8AcZCrzS_FU7XPOPmY31jPBQa5Zho7Blo=").strip()
        if SESSION_STR and API_ID and API_HASH:
            try:
                acc_id = _add_account_record("Johan", API_ID, API_HASH, SESSION_STR)
                print(f"✅ Default account loaded: {acc_id}")
            except Exception as e:
                print(f"⚠️ Default account failed: {e}")
    except Exception as e:
        print(f"⚠️ Error loading default account: {e}")

def _add_account_record(name, api_id, api_hash, session_string):
    acc_id = str(uuid.uuid4())[:8]
    with accounts_lock:
        accounts[acc_id] = _make_acc_dict(acc_id, name, api_id, api_hash, session_string)
    _start_account_thread(acc_id)
    return acc_id


# ==================== REGEX ====================
_RX_IN  = re.compile(r'In\s+Link[^:\n]*?:-\s*\*{0,4}\s*(https?://[^\s*\n]+)', re.I)
_RX_GOT = re.compile(r'Got\s+Result[^:\n]*?:-\s*\*{0,4}\s*(https?://[^\s*\n]+)', re.I)
_RICH_LABELS = {
    'instant_dl':      re.compile(r'instant\s*dl|10gbps',  re.I),
    'telegram_link':   re.compile(r'telegram\s*link|tgcdn', re.I),
    'direct_download': re.compile(r'direct\s*download',     re.I),
    'got_result':      re.compile(r'got\s*result',          re.I),
}
_RX_ERR          = re.compile(
    r'invalid\s*link|not\s*support|unsupported|no\s*script|not\s*found|'
    r'error|failed|cannot|wrong|sorry|got\s+error',
    re.I,
)
_RX_RATE         = re.compile(r'rate\s*limit|flood|wait\s*\d+|try\s*again', re.I)
_RX_INTERMEDIATE = re.compile(
    r'^[\*\s]*bypass(?:ing)?\.{0,6}[\*\s]*$'
    r'|^[\*\s]*processing\.{0,6}[\*\s]*$'
    r'|^[\*\s]*please\s*wait[\*\s]*$'
    r'|^[\*\s]*fetching[\*\s]*$'
    r'|^[\*\s]*checking[\*\s]*$',
    re.I | re.M)
_RX_GOT_ERR = re.compile(r'Got\s+Error[^:\n]*?:-\s*[`*\s]*(.*?)\s*[`*]*\s*$', re.I | re.M)
_RX_JUNK    = re.compile(r'[\*`\'\"✔️✅]+')
_RX_FNAME   = re.compile(r'File\s+Name\s*:-\s*[`*\s]*(.+?)\s*[`*]*\s*$', re.I | re.M)
_RX_FSIZE   = re.compile(r'File\s+Size\s*:-\s*[`*\s]*(.+?)\s*[`*]*\s*$', re.I | re.M)
_RX_SEP     = re.compile(r'━{3,}.*?✦.*?━{3,}')

def _clean(u):
    return _RX_JUNK.sub('', u).rstrip('.,;:!?)>]\'"').strip()

def parse_dzhq_message(text, entities, sent_link):
    if not text:
        return [{"status": "empty"}]
    stripped = text.strip()
    lines = [l.strip() for l in stripped.splitlines() if l.strip()]
    if len(lines) <= 2 and _RX_INTERMEDIATE.search(stripped):
        return [{"status": "intermediate", "raw": stripped[:80]}]
    if _RX_RATE.search(text):
        return [{"status": "rate_limit", "error": "Rate limited by DZHQ bot"}]
    if _RX_ERR.search(text):
        err_m = _RX_GOT_ERR.search(text)
        clean_err = re.sub(r'[\*`]+', '', _clean(err_m.group(1))).strip() if err_m else stripped[:120]
        return [{"status": "failed", "error": clean_err}]

    results, ent_urls = [], []
    if entities:
        for ent in entities:
            off = getattr(ent, 'offset', None)
            lng = getattr(ent, 'length', None)
            url = getattr(ent, 'url', None)
            if not url:
                if isinstance(ent, MessageEntityUrl) and off is not None and lng:
                    url = text[off:off + lng]
                else:
                    continue
            url = _clean(url)
            if not url.startswith(('http://', 'https://')):
                continue
            if off is not None:
                ls = text.rfind('\n', 0, off) + 1
                le = text.find('\n', off)
                le = le if le != -1 else len(text)
                anchor = _clean(text[off:off + (lng or 0)]) if lng else ''
                line   = _clean(text[ls:le])
                # Prefer the anchor text ("FSL Server", "Pixeldrain"…) so that
                # multi-link lines don't all inherit the same line label.
                if anchor and not anchor.startswith(('http://', 'https://')) \
                   and not re.fullmatch(r'(?i)[\s\*`\[\]\-–—:•]*((link|click|here|download|open|dl)[\s\*`\[\]\-–—:•]*)+', anchor):
                    label = anchor
                else:
                    label = line
                ent_urls.append((label, url))
            else:
                ent_urls.append((url, url))


    blocks = _RX_SEP.split(text)
    blocks = [b.strip() for b in blocks if b.strip()]
    for block in blocks:
        if 'Powered By' in block and 'DZHQBypass' in block and len(block) < 80:
            continue
        r = _parse_block(block, ent_urls, sent_link)
        if r:
            results.append(r)
    if not results:
        r = _parse_block(text, ent_urls, sent_link)
        if r:
            results.append(r)
    if not results:
        return [{"status": "no_link", "error": "No bypassed link found", "raw": text[:300]}]
    return results


# ==================== NICK BOT PARSER (DM mode) ====================
def parse_nick_message(text, entities, sent_link):
    """Parse DM reply from Nick_Bypass_Bot"""
    if not text:
        return None
    stripped = text.strip()
    # Skip intermediate messages
    if _RX_INTERMEDIATE.search(stripped) and len(stripped) < 100:
        return None
    if _RX_RATE.search(text):
        return {
            "status": "rate_limit",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }
    if _RX_ERR.search(text):
        return {
            "status": "failed",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }

    urls_found = []
    # Extract from entities first (most reliable)
    if entities:
        for ent in entities:
            off = getattr(ent, 'offset', None)
            lng = getattr(ent, 'length', None)
            url = getattr(ent, 'url', None)
            if not url:
                if isinstance(ent, MessageEntityUrl) and off is not None and lng:
                    url = text[off:off + lng]
                else:
                    continue
            url = _clean(url)
            if not url.startswith(('http://', 'https://')):
                continue
            if sent_link and (sent_link in url or url in sent_link):
                continue
            if url not in urls_found:
                urls_found.append(url)

    # Fallback: plain text URL regex
    if not urls_found:
        for u in re.findall(r'https?://[^\s\n\)\]>"\']+', text):
            u = _clean(u)
            if not u.startswith(('http://', 'https://')):
                continue
            if sent_link and (sent_link in u or u in sent_link):
                continue
            if u not in urls_found:
                urls_found.append(u)

    if not urls_found:
        return None

    def _flatten(lst):
        if not lst:       return None
        if len(lst) == 1: return lst[0]
        return lst

    return {
        "status":   "ok",
        "original": sent_link,
        "bypassed": _flatten(urls_found),
        "raw":      stripped[:300],
    }


def _parse_block(block, ent_urls, sent_link):
    original      = None
    bypassed      = []
    instant_dl    = []
    telegram_link = []
    direct_dl     = []
    file_name = file_size = None
    raw_type = "unknown"

    m = _RX_FNAME.search(block)
    if m: file_name = _clean(m.group(1))
    m = _RX_FSIZE.search(block)
    if m: file_size = _clean(m.group(1))
    m = _RX_IN.search(block)
    if m: original = _clean(m.group(1))
    m = _RX_GOT.search(block)
    if m:
        u = _clean(m.group(1))
        if u not in bypassed: bypassed.append(u)
        raw_type = "got_result"

    for label, url in ent_urls:
        ll, ul = label.lower(), url.lower()
        if original and (original in url or url in original):
            continue
        if _RICH_LABELS['instant_dl'].search(ll):
            if url not in instant_dl: instant_dl.append(url)
            raw_type = "rich"
        elif _RICH_LABELS['telegram_link'].search(ll) or 'tgcdn_bot' in ul or 'telegram.dog' in ul:
            if url not in telegram_link: telegram_link.append(url)
            raw_type = "rich"
        elif _RICH_LABELS['direct_download'].search(ll):
            if url not in direct_dl: direct_dl.append(url)
            raw_type = "rich"
        elif _RICH_LABELS['got_result'].search(ll):
            if url not in bypassed: bypassed.append(url)
            raw_type = "entity_got_result"
        else:
            if url not in bypassed: bypassed.append(url)
            if raw_type == "unknown": raw_type = "entity_generic"

    if not bypassed:
        urls = [_clean(u) for u in re.findall(r'https?://[^\s\*\n\)\]>]+', block)]
        for u in urls:
            if original and (original in u or u in original):
                continue
            if u not in bypassed: bypassed.append(u)
            raw_type = "plain_url"

    if not original:
        original = sent_link
    if not (bypassed or instant_dl or telegram_link or direct_dl):
        return None

    def _flatten(lst):
        if not lst:   return None
        if len(lst) == 1: return lst[0]
        return lst

    out = {"status": "ok", "original": original,
           "bypassed": _flatten(bypassed),
           "raw_type": raw_type, "file_name": file_name, "file_size": file_size}
    if instant_dl:    out["instant_dl"]   = _flatten(instant_dl)
    if telegram_link: out["telegram"]     = _flatten(telegram_link)
    if direct_dl:     out["direct"]       = _flatten(direct_dl)
    return out




# ===== FELIX OSINT ADDITIVE PATCH: response parser =====
_OSINT_REPORT_RE = re.compile(r"REPORT\s*:\s*([^\n{]+)", re.I)
_OSINT_TARGET_RE = re.compile(r"TARGET\s*:\s*([^\n]+)", re.I)


def _normalize_osint_target(value):
    """Normalize a phone number / username / chat ID for comparison.

    Returns None for empty values. Usernames lose a leading @ and case;
    phone-ish values collapse to digits so "+91 98765 43210" and
    "98765-43210" compare equal to "919876543210"-style targets.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.lstrip("@").strip()
    if re.fullmatch(r"[+\d][\d\s\-().]*", s) and re.sub(r"\D", "", s):
        return re.sub(r"\D", "", s)
    return s.lower()


def _osint_targets_equal(a, b):
    """True when two normalized targets refer to the same lookup."""
    if not a or not b:
        return False
    if a == b:
        return True
    # Tolerate a responder that echoes the number with/without country code.
    if a.isdigit() and b.isdigit() and len(min(a, b, key=len)) >= 7:
        if a.endswith(b) or b.endswith(a):
            return True
    return False


def parse_osint_group_message(text):
    """Parse the group's TARGET/REPORT + JSON response without rewriting JSON.

    Tolerant by design: the responder's exact layout may vary, so a message
    counts as a response when it carries a REPORT line, a TARGET line, or a
    JSON object. Anything else returns None so group chatter is ignored.
    """
    if not text or not text.strip():
        return None

    report_match = _OSINT_REPORT_RE.search(text)
    if report_match:
        report_name = report_match.group(1).strip(" `*:_-") or None
    else:
        report_name = None

    json_start = text.find("{", report_match.end()) if report_match else -1
    if json_start < 0:
        json_start = text.find("{")

    # Prefer the header area (before the JSON payload) for the TARGET line so
    # a "target"-looking key inside the JSON body cannot confuse matching.
    head = text[:json_start] if json_start >= 0 else text
    target_match = _OSINT_TARGET_RE.search(head)
    if target_match is None:
        target_match = _OSINT_TARGET_RE.search(text)
    if target_match:
        # Take the rest of the TARGET line (numbers may contain spaces), but
        # stop at an inline JSON payload or a same-line REPORT header.
        target = target_match.group(1).split("{", 1)[0]
        target = re.split(r"\breport\s*:", target, flags=re.I)[0]
        target = target.strip(" `@*:_-,.;") or None
    else:
        target = None

    if json_start < 0:
        if not report_match and not target_match:
            return None
        # Response-shaped message without a JSON payload: surface the raw
        # text as data instead of forcing a timeout downstream.
        return {
            "status": "ok",
            "report": report_name,
            "target": target,
            "data": {"text": text.strip()[:4000]},
            "raw": text[:3000],
        }

    try:
        data, _ = json.JSONDecoder().raw_decode(text[json_start:])
    except Exception as exc:
        return {
            "status": "parse_error",
            "report": report_name,
            "target": target,
            "error": f"Invalid JSON from OSINT group: {exc}",
            "raw": text[:3000],
        }

    return {
        "status": "ok",
        "report": report_name,
        "target": target,
        "data": data,
        "raw": text[:3000],
    }


def _osint_command_parts(command):
    """Validate an allowlisted OSINT command and return (name, args)."""
    command = (command or "").strip()
    if not command.startswith("/"):
        return None, "Command must start with /"
    if len(command) > 512:
        return None, "Command is too long"

    m = re.fullmatch(r"/([A-Za-z0-9_]+)(?:\s+(.+))?", command)
    if not m:
        return None, "Invalid command format"

    name = m.group(1).lower()
    args = (m.group(2) or "").strip()
    if name not in OSINT_ALLOWED_COMMANDS:
        return None, f"Command /{name} is not allowed"

    # Keep group traffic bounded and reject control characters.
    if any(ord(ch) < 32 and ch not in "\t" for ch in command):
        return None, "Control characters are not allowed"

    # Known commands get basic argument validation; other allowlisted
    # commands remain available without changing the existing bypass code.
    if name == "num":
        if not re.fullmatch(r"\d{10}", args):
            return None, "/num requires exactly 10 digits"
    elif name == "tg":
        if not re.fullmatch(r"[@A-Za-z0-9_.-]{1,128}", args):
            return None, "/tg requires a Telegram username or numeric/chat ID"

    return name, args



# ===== FELIX OSINT ADDITIVE PATCH: Telegram group handler =====
def make_osint_handler(acc_id, group):
    """Group-aware OSINT listener. One instance is registered per OSINT group;
    correlation (reply-to + FIFO queue) is scoped to that group only."""
    async def on_osint_message(event):
        if not OSINT_ENABLED:
            return

        with accounts_lock:
            state = accounts.get(acc_id)
        if not state:
            return

        # Per-group responder: each OSINT group may have a DIFFERENT responder
        # bot (admin panel or OSINT_GROUP_RESPONDERS env). Groups without one
        # fall back to OSINT_SERVICE_USER_ID, then to accepting any sender.
        # TIP: with TRACE_BOTS=1 the matched/ignored logs print every sender
        # ID, so you can discover a responder's numeric ID from the logs.
        _resp_id = _osint_responder_for(group)
        if _resp_id is not None:
            try:
                if event.sender_id != int(_resp_id):
                    _trace("OSINT", f"ignored group={group} sender={event.sender_id} (not the configured responder)")
                    return
            except (TypeError, ValueError):
                return

        msg = event.message
        text = (msg.text or msg.caption or "").strip()
        if not text:
            return

        parsed = parse_osint_group_message(text)
        if not parsed:
            return

        reply_to = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
        incoming_id = getattr(msg, "id", None)

        with accounts_lock:
            queue = list(_osint_group_queue(state, group))
            pending = state.get("pending", {})

        if not queue:
            return

        req_id = None
        if reply_to:
            # A threaded reply identifies its request exactly. A reply that
            # matches nothing pending belongs to somebody else — ignore it.
            # In race mode a request has one sent ID per group.
            for candidate in queue:
                req = pending.get(candidate)
                if not req or req.get("done"):
                    continue
                sent_ids = req.get("osint_sent_ids") or {}
                if req.get("osint_sent_id") == reply_to or reply_to in sent_ids.values():
                    req_id = candidate
                    break
            if req_id is None:
                _trace("OSINT", f"ignored account={acc_id} group={group} reply_to={reply_to} (no pending request)")
                return
        else:
            # Broadcast-style response (no reply header): attribute it to the
            # oldest unfinished request in THIS group. The OSINT endpoint
            # serializes one in-flight query per (account, group).
            for candidate in queue:
                req = pending.get(candidate)
                if req and not req.get("done"):
                    req_id = candidate
                    break
            if req_id is None:
                return

        req = pending.get(req_id)
        if not req or req.get("done"):
            return

        sent_id = req.get("osint_sent_id")
        if sent_id and incoming_id and incoming_id <= sent_id:
            return

        expected_target = _normalize_osint_target(req.get("osint_expected_target"))
        expected_command = (req.get("osint_command_name") or "").lower()
        received_target = _normalize_osint_target(parsed.get("target"))

        report_norm = re.sub(
            r"[^a-z0-9]+",
            "",
            str(parsed.get("report") or "").lower(),
        )

        expected_reports = {
            "num": {"num", "numinfo", "numberinfo", "phoneinfo", "mobileinfo",
                    "truecaller", "callerinfo"},
            "tg": {"tg", "tginfo", "telegraminfo", "userinfo", "usercheck"},
        }

        allowed_reports = expected_reports.get(expected_command, set())
        other_reports = set().union(
            *(names for cmd, names in expected_reports.items() if cmd != expected_command)
        )
        report_ok = bool(report_norm) and report_norm in allowed_reports
        report_is_other = bool(report_norm) and report_norm in other_reports
        sent_ids = req.get("osint_sent_ids") or {}
        reply_ok = bool(reply_to and (reply_to == sent_id or reply_to in sent_ids.values()))
        target_ok = _osint_targets_equal(expected_target, received_target)

        # Exact reply is the strongest correlation — accept it whatever the
        # responder chose to name its report.
        if reply_ok:
            accept, reason = True, "reply"
        elif report_is_other:
            # Clearly a response to a different command type.
            accept, reason = False, "other-command-report"
        elif expected_command == "num":
            # Numeric lookups must correlate by target (or by a recognized
            # report type when the responder omits the TARGET line).
            if expected_target and received_target:
                accept, reason = (target_ok, "target" if target_ok else "target-mismatch")
            else:
                accept, reason = (report_ok, "report" if report_ok else "no-target-no-report")
        else:
            # /tg and other allowlisted commands: broadcast responses are
            # FIFO-attributed unless they are clearly another command's.
            accept, reason = True, "fifo"

        if not accept:
            _trace(
                "OSINT",
                f"ignored account={acc_id} group={group} reason={reason} reply_to={reply_to} "
                f"sent={sent_id} report={parsed.get('report')} target={parsed.get('target')} "
                f"expected={req.get('osint_expected_target')}",
            )
            return

        req["last_osint_ts"] = time.time()
        req["osint_result"] = parsed
        req["osint_winning_group"] = group
        _trace(
            "OSINT",
            f"matched account={acc_id} group={group} sender={event.sender_id} req={req_id} via={reason} "
            f"report={parsed.get('report')} target={parsed.get('target')} status={parsed.get('status')}",
        )
        req["osint_event"].set()

    return on_osint_message

# ==================== PER-ACCOUNT TELEGRAM CLIENT ====================
def make_dzhq_handler(acc_id):
    async def on_reply(event):
        with accounts_lock:
            state = accounts.get(acc_id)
        if not state:
            return
        bot_id = state.get("dzhq_bot_id")
        if bot_id:
            if event.sender_id != bot_id:
                return
        else:
            try:
                s = await event.get_sender()
                u = (getattr(s, 'username', '') or '').lower()
                if 'dzhq' not in u:
                    return
            except:
                return

        msg  = event.message
        text = msg.text or ''
        if not text:
            return

        rt_obj      = getattr(msg, 'reply_to', None)
        reply_to_id = getattr(rt_obj, 'reply_to_msg_id', None) if rt_obj else None

        with accounts_lock:
            pending = state.get("pending", {})
        sent_id_map = {
            req['dzhq_sent_id']: rid
            for rid, req in pending.items()
            if not req.get('done') and req.get('dzhq_sent_id')
        }
        matched_id = sent_id_map.get(reply_to_id)
        if not matched_id or matched_id not in pending:
            return

        req     = pending[matched_id]
        ents    = msg.entities or []
        results = parse_dzhq_message(text, ents, req['link'])
        status  = results[0].get('status') if results else 'empty'
        _trace("DZHQ", f"received for {req['link']}: status={status} text={text[:500]!r} parsed={results}")
        req['last_dzhq_ts'] = time.time()
        if status in ('failed', 'rate_limit'):
            req['dzhq_fail'] = (
                results[0].get('error')
                or ("DZHQ rate limited" if status == "rate_limit" else "DZHQ rejected the link")
            )

        tg = state.get("client")
        if status not in ('intermediate', 'rate_limit'):
            if tg:
                async def _auto_click_delete(bot_msg, req_ref, acc):
                    """Click the Delete inline button on DZHQ bot's result message.
                    Only clicks if button is present — never deletes messages manually."""
                    try:
                        buttons = getattr(bot_msg, 'buttons', None)
                        clicked = False
                        if buttons:
                            flat = [btn for row in buttons for btn in row]
                            for btn in flat:
                                btn_text = (getattr(btn, 'text', '') or '').strip().lower()
                                if 'delete' in btn_text or '🗑' in btn_text or '❌' in btn_text:
                                    try:
                                        await btn.click()
                                        print(f"[DZHQ/{acc}] ✅ Delete button clicked")
                                        clicked = True
                                        break
                                    except Exception as e:
                                        print(f"[DZHQ/{acc}] ⚠️ btn.click error: {e}")
                        if not clicked:
                            print(f"[DZHQ/{acc}] ℹ️ No Delete button found — skipping")
                        req_ref['dzhq_sent_id'] = None
                    except Exception as e:
                        print(f"[DZHQ/{acc}] Delete error: {e}")

                asyncio.create_task(_auto_click_delete(msg, req, acc_id))

        if status in ('intermediate', 'empty'):
            return

        req['dzhq_result'] = results
        req['dzhq_event'].set()
    return on_reply


def make_nick_dm_handler(acc_id):
    """
    Handler for Nick_Bypass_Bot private DMs.
    - Nick is used as a Telegram fallback only; no captcha solver is used.
    - If Nick sends the bypass result → signal the waiting thread
    """
    async def on_dm(event):
        with accounts_lock:
            state = accounts.get(acc_id)
        if not state:
            return

        # Never treat Alex bot DMs as Nick replies
        alex_bot_id_guard = state.get("alex_bot_id")
        if alex_bot_id_guard and event.sender_id == alex_bot_id_guard:
            return

        # Only handle DMs from Nick bot
        nick_bot_id = state.get("nick_bot_id")
        if nick_bot_id:
            if event.sender_id != nick_bot_id:
                return
        else:
            try:
                s = await event.get_sender()
                u = (getattr(s, 'username', '') or '').lower()
                n = (getattr(s, 'first_name', '') or '').lower()
                if 'nick' not in u and 'nick' not in n and 'bypass' not in u:
                    return
            except:
                return

        msg  = event.message
        text = (msg.text or msg.caption or '').strip()
        tg   = state.get("client")

        # Need at least some text to parse a bypass result
        if not text:
            return

        # FIFO: pop oldest pending Nick DM request
        nick_lock = state.get("nick_dm_lock")
        if not nick_lock:
            return

        with nick_lock:
            queue = state.get("nick_dm_queue", [])
            if not queue:
                return
            req_id = queue[0]   # peek

        with accounts_lock:
            pending = state.get("pending", {})
            req     = pending.get(req_id)
        if not req or req.get('done'):
            with nick_lock:
                if queue and queue[0] == req_id:
                    queue.pop(0)
            return

        ents = msg.entities or []
        r    = parse_nick_message(text, ents, req['link'])
        _trace("NICK", f"received for {req['link']}: text={text[:500]!r} parsed={r}")
        req['last_nick_ts'] = time.time()
        if r is None:
            if TRACE_BOTS and not _RX_INTERMEDIATE.search(text) and not _RX_RATE.search(text):
                with nick_lock:
                    if queue and queue[0] == req_id:
                        queue.pop(0)
                req['nick_result'] = {
                    "status": "failed",
                    "error": "Nick parser found no bypassed URL",
                    "raw": text[:500],
                }
                req['nick_event'].set()
            return   # intermediate/rate_limit — keep waiting

        # Got a real bypass result — pop from queue and signal
        with nick_lock:
            if queue and queue[0] == req_id:
                queue.pop(0)

        req['nick_result'] = r
        req['nick_event'].set()
    return on_dm



# ==================== ALEX BOT PARSER (DM mode) ====================
# Alex bot writes everything in small-caps unicode ("ʙʏᴩᴀꜱꜱᴇᴅ ʟɪɴᴋ"), so all
# matching is done on a folded ASCII copy of the text.
_SMALLCAPS_MAP = {
    'ᴀ':'a','ʙ':'b','ᴄ':'c','ᴅ':'d','ᴇ':'e','ꜰ':'f','ɢ':'g','ʜ':'h','ɪ':'i','ᴊ':'j',
    'ᴋ':'k','ʟ':'l','ᴍ':'m','ɴ':'n','ᴏ':'o','ᴩ':'p','ᴘ':'p','q':'q','ǫ':'q','ʀ':'r',
    'ꜱ':'s','ѕ':'s','ᴛ':'t','ᴜ':'u','ᴠ':'v','ᴡ':'w','x':'x','ʏ':'y','ᴢ':'z','ɀ':'z',
}
def _fold(s):
    return ''.join(_SMALLCAPS_MAP.get(ch, ch) for ch in (s or ''))

_RX_ALEX_BYPASSED = re.compile(r'bypass(?:ed)?\s*link[^:\n]*:?-?\s*\**\s*\n?\s*(https?://\S+)', re.I)
_RX_ALEX_ORIGINAL = re.compile(r'original\s*link[^:\n]*:?-?\s*\**\s*\n?\s*(https?://\S+)', re.I)
_RX_ALEX_PROGRESS = re.compile(
    r'initialis|initializ|fetching|scanning|bypassing\s*security|cracking|decoding|solving|'
    r'processing|please\s*wait|\d{1,3}\s*%|[▰▱]', re.I)
_RX_ALEX_FAIL = re.compile(
    r'bypass\s*(?:failed|error)|invalid\s*link|not\s*support(?:ed)?|unsupported|'
    r'no\s*script|not\s*found|unable|unable\s+to|cannot|can[\'’]t|'
    r'could\s*not|couldn[\'’]t|not\s*possible|does\s*not\s*support|'
    r'doesn[\'’]t\s*support|failed|error', re.I)
_ALEX_SKIP_PARTS  = ("t.me/+", "t.me/alexmodz", "update", "join", "channel", "apkamitrr")


def parse_alex_message(text, entities, sent_link):
    """Parse DM reply from the Alex Bypass Bot (new + edited messages)."""
    if not text:
        return None
    stripped = text.strip()
    folded   = _fold(stripped)

    # Progress / animation frames — keep waiting
    if (
        _RX_ALEX_PROGRESS.search(folded)
        and not _RX_ALEX_BYPASSED.search(folded)
        and not _RX_ALEX_FAIL.search(folded)
    ):
        return None
    if _RX_INTERMEDIATE.search(folded) and len(folded) < 120:
        return None
    if _RX_RATE.search(folded):
        return {
            "status": "rate_limit",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }
    if _RX_ALEX_FAIL.search(folded) and not _RX_ALEX_BYPASSED.search(folded):
        return {
            "status": "failed",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }

    # 1) Preferred: the explicit "BYPASSED LINK" section
    m = _RX_ALEX_BYPASSED.search(folded)
    if m:
        u = _clean(m.group(1))
        mo = _RX_ALEX_ORIGINAL.search(folded)
        orig = _clean(mo.group(1)) if mo else sent_link
        same_as_input = (sent_link and (sent_link in u or u in sent_link)) or (orig and u == orig)
        if u.startswith(("http://", "https://")) and not same_as_input \
           and not any(k in u.lower() for k in _ALEX_SKIP_PARTS):
            return {"status": "ok", "original": orig or sent_link, "bypassed": u, "raw": stripped[:300]}


    # 2) Fallback: reuse the generic DM parser (entities + plain URLs)
    r = parse_nick_message(text, entities, sent_link)
    if not r:
        return None
    if r.get("status") != "ok":
        return r
    b = r.get("bypassed")
    if isinstance(b, list):
        b = next((x for x in b if not any(k in x.lower() for k in _ALEX_SKIP_PARTS)), b[0] if b else None)
    if not b:
        return None
    r["bypassed"] = b
    return r


def _same_link(left, right):
    """Compare links without treating a trailing slash as a different request."""
    if not left or not right:
        return False
    try:
        a = urllib.parse.urlsplit(str(left).strip())
        b = urllib.parse.urlsplit(str(right).strip())
        return (
            a.scheme.lower(),
            (a.hostname or "").lower(),
            a.port,
            a.path.rstrip("/") or "/",
            a.query,
            a.fragment,
        ) == (
            b.scheme.lower(),
            (b.hostname or "").lower(),
            b.port,
            b.path.rstrip("/") or "/",
            b.query,
            b.fragment,
        )
    except ValueError:
        return str(left).strip().rstrip("/") == str(right).strip().rstrip("/")


def make_alex_dm_handler(acc_id):
    """Handler for @alexbypassbot private DMs."""
    async def on_dm(event):
        with accounts_lock:
            state = accounts.get(acc_id)
        if not state:
            return

        alex_bot_id = state.get("alex_bot_id")
        if alex_bot_id:
            if event.sender_id != alex_bot_id:
                return
        else:
            try:
                sndr = await event.get_sender()
                u = (getattr(sndr, 'username', '') or '').lower()
                if 'alex' not in u:
                    return
            except Exception:
                return

        msg  = event.message
        text = (msg.text or msg.caption or '').strip()
        if not text:
            return

        alex_lock = state.get("alex_dm_lock")
        if not alex_lock:
            return

        with alex_lock:
            queue = state.get("alex_dm_queue", [])
            if not queue:
                return
            req_id = queue[0]

        with accounts_lock:
            req = state.get("pending", {}).get(req_id)
        if not req or req.get('done'):
            with alex_lock:
                if queue and queue[0] == req_id:
                    queue.pop(0)
            return

        # Alex edits its progress/result messages in the same DM. Ignore a
        # message from an earlier request, otherwise a stale result can be
        # accepted as the result for the next queued link.
        incoming_id = getattr(msg, "id", None)
        sent_id = req.get("alex_sent_id")
        if sent_id and incoming_id and incoming_id <= sent_id:
            _trace(
                "ALEX_BOT",
                f"ignored stale message id={incoming_id} for {req['link']} "
                f"(request message id={sent_id})",
            )
            return

        r = parse_alex_message(text, msg.entities or [], req['link'])
        if r and r.get("original") and not _same_link(r["original"], req["link"]):
            _trace(
                "ALEX_BOT",
                f"ignored mismatched result for {req['link']}: "
                f"message original={r['original']}",
            )
            return
        _trace("ALEX_BOT", f"received for {req['link']}: text={text[:500]!r} parsed={r}")
        req["last_alex_ts"] = time.time()
        if not req.get("alex_first_msg_ts"):
            req["alex_first_msg_ts"] = req["last_alex_ts"]
        if r is None:
            folded_text = _fold(text)
            is_known_failure = _RX_ERR.search(folded_text) and not _RX_ALEX_BYPASSED.search(folded_text)
            if (TRACE_BOTS or is_known_failure) \
                    and not _RX_ALEX_PROGRESS.search(folded_text) \
                    and not _RX_RATE.search(folded_text):
                with alex_lock:
                    if queue and queue[0] == req_id:
                        queue.pop(0)
                req['alex_bot_result'] = {
                    "status": "failed",
                    "error": "Alex parser found no bypassed URL",
                    "raw": text[:500],
                }
                req['alex_bot_event'].set()
            return   # intermediate message — keep waiting

        with alex_lock:
            if queue and queue[0] == req_id:
                queue.pop(0)

        req['alex_bot_result'] = r
        req['alex_bot_event'].set()
    return on_dm


async def _run_account(acc_id):
    with accounts_lock:
        state = accounts.get(acc_id)
    if not state:
        return

    api_id   = state['api_id']
    api_hash = state['api_hash']
    sess     = state['session_string']

    loop = asyncio.get_event_loop()
    tg = TelegramClient(
        StringSession(sess), api_id, api_hash,
        connection=ConnectionTcpAbridged,
        auto_reconnect=True, receive_updates=True, loop=loop,
    )
    with accounts_lock:
        accounts[acc_id]['client'] = tg
        accounts[acc_id]['loop']   = loop

    try:
        await tg.start()
        if not await tg.is_user_authorized():
            raise Exception("Session invalid / expired")

        # Resolve DZHQ bot ID
        try:
            ent = await tg.get_entity(DZHQ_BOT)
            with accounts_lock:
                accounts[acc_id]['dzhq_bot_id'] = ent.id
        except:
            pass

        # Resolve Nick bot ID
        try:
            ent3 = await tg.get_entity(NICK_BOT)
            with accounts_lock:
                accounts[acc_id]['nick_bot_id'] = ent3.id
            nick_id = ent3.id
        except:
            nick_id = None
            print(f"[Acc {acc_id}] ⚠️ Could not resolve {NICK_BOT}")

        # Resolve Alex bot ID
        try:
            ent4 = await tg.get_entity(ALEX_BOT)
            with accounts_lock:
                accounts[acc_id]['alex_bot_id'] = ent4.id
            alex_id = ent4.id
        except Exception:
            alex_id = None
            print(f"[Acc {acc_id}] ⚠️ Could not resolve {ALEX_BOT}")

        # Group handlers (DZHQ edits its message too)
        dzhq_h = make_dzhq_handler(acc_id)
        tg.add_event_handler(dzhq_h, events.NewMessage(chats=DZHQ_GROUP))
        tg.add_event_handler(dzhq_h, events.MessageEdited(chats=DZHQ_GROUP))

        # OSINT group listener — additive; existing DZHQ/Nick/Alex listeners
        # and their conditions remain unchanged.
        if OSINT_ENABLED:
            for _osint_group in OSINT_GROUPS:
                osint_h = make_osint_handler(acc_id, _osint_group)
                tg.add_event_handler(osint_h, events.NewMessage(chats=_osint_group))
                tg.add_event_handler(osint_h, events.MessageEdited(chats=_osint_group))


        # Alex bot DM handler — Alex EDITS the progress message into the result,
        # so MessageEdited must be handled as well.
        alex_h = make_alex_dm_handler(acc_id)
        if alex_id:
            tg.add_event_handler(alex_h, events.NewMessage(from_users=alex_id, incoming=True, func=lambda e: e.is_private))
            tg.add_event_handler(alex_h, events.MessageEdited(from_users=alex_id, incoming=True, func=lambda e: e.is_private))
        else:
            tg.add_event_handler(alex_h, events.NewMessage(incoming=True, func=lambda e: e.is_private))
            tg.add_event_handler(alex_h, events.MessageEdited(incoming=True, func=lambda e: e.is_private))


        # Nick DM handler — listen for private messages from Nick bot
        if nick_id:
            nick_h = make_nick_dm_handler(acc_id)
            # Nick can first send a loading message and then edit that same
            # message into the final result/error.  Listening only for
            # NewMessage leaves the request waiting until it times out.
            tg.add_event_handler(
                nick_h,
                events.NewMessage(from_users=nick_id, incoming=True, func=lambda e: e.is_private)
            )
            tg.add_event_handler(
                nick_h,
                events.MessageEdited(from_users=nick_id, incoming=True, func=lambda e: e.is_private)
            )
        else:
            # Fallback: listen to all private incoming and filter by username
            nick_h = make_nick_dm_handler(acc_id)
            tg.add_event_handler(
                nick_h,
                events.NewMessage(incoming=True, func=lambda e: e.is_private)
            )
            tg.add_event_handler(
                nick_h,
                events.MessageEdited(incoming=True, func=lambda e: e.is_private)
            )

        me = await tg.get_me()
        with accounts_lock:
            accounts[acc_id].update({
                "status":     "active",
                "username":   getattr(me, 'username', None),
                "first_name": getattr(me, 'first_name', None),
                "error_message": None,
            })
        print(f"[Acc {acc_id}] ✅ {me.first_name} (@{me.username})")
        await tg.run_until_disconnected()

    except Exception as e:
        err = str(e)
        print(f"[Acc {acc_id}] ❌ {err}")
        with accounts_lock:
            if acc_id in accounts:
                accounts[acc_id].update({"status": "error", "error_message": err})

def _thread_target(acc_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_account(acc_id))
    except Exception as e:
        print(f"[Acc {acc_id}] Thread crash: {e}")
        with accounts_lock:
            if acc_id in accounts:
                accounts[acc_id]['status'] = 'error'

def _start_account_thread(acc_id):
    t = threading.Thread(target=_thread_target, args=(acc_id,), daemon=True)
    t.start()
    with accounts_lock:
        if acc_id in accounts:
            accounts[acc_id]['thread'] = t

def _stop_account(acc_id):
    with accounts_lock:
        state = accounts.get(acc_id)
    if not state:
        return
    tg   = state.get("client")
    loop = state.get("loop")
    if tg and loop and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(tg.disconnect(), loop).result(timeout=5)
        except:
            pass


# ==================== ACCOUNT SELECTION ====================
def get_next_active():
    global round_robin_idx
    with accounts_lock:
        active = [(k, v) for k, v in accounts.items() if v.get("status") == "active"]
    if not active:
        return None, None
    with rr_lock:
        idx = round_robin_idx % len(active)
        round_robin_idx = (round_robin_idx + 1) % len(active)
    return active[idx]



# ===== FELIX OSINT ADDITIVE PATCH: account selector =====
def get_next_osint_active():
    """Round-robin over active accounts using an OSINT-only pointer.

    This deliberately does NOT touch the existing round_robin_idx used by
    /bypass, so adding OSINT traffic cannot change bypass account order.
    """
    global _osint_round_robin_idx
    with accounts_lock:
        active = [(k, v) for k, v in accounts.items() if v.get("status") == "active"]
    if not active:
        return None, None
    with _osint_rr_lock:
        idx = _osint_round_robin_idx % len(active)
        _osint_round_robin_idx = (_osint_round_robin_idx + 1) % len(active)
    return active[idx]



# ==================== ALEX API + ALEX BOT RACE ====================
def _alex_race_budget():
    """Absolute safety cap for Alex resolutions (race and single-bot runs)."""
    configured = [
        value
        for value in (ALEX_TIMEOUT_SEC, ALEX_BOT_TIMEOUT_SEC, BYPASS_IDLE_TIMEOUT_SEC,
                      ALEX_BOT_IDLE_TIMEOUT_SEC)
        if value > 0
    ]
    return min(
        ALEX_RACE_MAX_TIMEOUT_SEC,
        max(configured, default=30.0) + 5.0,
    )


def _alex_idle_timeout(req_entry):
    """Effective idle allowance for the Alex bot wait loop.

    Patient (ALEX_BOT_IDLE_TIMEOUT_SEC) once the bot has shown signs of
    life; strict (BYPASS_IDLE_TIMEOUT_SEC chain) before first contact so a
    dead bot still fails fast.
    """
    if req_entry.get("alex_first_msg_ts") and ALEX_BOT_IDLE_TIMEOUT_SEC > 0:
        return ALEX_BOT_IDLE_TIMEOUT_SEC
    if BYPASS_IDLE_TIMEOUT_SEC > 0:
        return BYPASS_IDLE_TIMEOUT_SEC
    return ALEX_BOT_TIMEOUT_SEC


def _alex_bot_roundtrip(link, stop_event=None, acc=None):
    """One @alexbypassbot DM round-trip: send the link, wait for the reply.

    Shared by the Alex race and bypass priority rules, so both use the exact
    same send/wait/cleanup logic. Returns (result_dict|None, error_str|None);
    (None, None) means stop_event was set — someone else already won.
    """
    if acc is None:
        acc_id, acc_state = get_next_active()
    else:
        acc_id, acc_state = acc
    if not acc_state:
        return None, "No active Telegram account for Alex bot"
    req_id = secrets.token_hex(8)
    req_entry = {
        "link":            link,
        "ts":              time.time(),
        "done":            False,
        "alex_sent_id":    None,
        "last_alex_ts":    None,
        "alex_bot_event":  threading.Event(),
        "alex_bot_result": None,
    }
    with accounts_lock:
        accounts[acc_id]["pending"][req_id] = req_entry
        alk = accounts[acc_id].get("alex_dm_lock")
        if alk:
            with alk:
                accounts[acc_id]["alex_dm_queue"].append(req_id)
    loop = acc_state.get("loop")

    async def _send_alex():
        sent = await acc_state["client"].send_message(ALEX_BOT, link)
        req_entry["alex_sent_id"] = getattr(sent, "id", None)
        req_entry["last_alex_ts"] = time.time()

    try:
        try:
            asyncio.run_coroutine_threadsafe(_send_alex(), loop).result(timeout=12)
        except Exception as e:
            return None, f"Alex bot send error: {e}"
        ev = req_entry["alex_bot_event"]
        budget_end = time.time() + _alex_race_budget()
        while stop_event is None or not stop_event.is_set():
            if time.time() >= budget_end:
                return None, "Alex bot timed out"
            idle_timeout = _alex_idle_timeout(req_entry)
            last_seen = req_entry.get("last_alex_ts") or time.time()
            rem = None if idle_timeout <= 0 else last_seen + idle_timeout - time.time()
            if rem is not None and rem <= 0:
                return None, "Alex bot idle timeout (no new message)"
            if ev.wait(timeout=0.3 if rem is None else min(0.3, rem)):
                r = req_entry.get("alex_bot_result")
                if r and r.get("bypassed"):
                    return {
                        "status": "ok",
                        "original": link,
                        "bypassed": r["bypassed"],
                        "account": acc_state.get("name") or acc_id,
                    }, None
                return None, (r or {}).get("error") or "Alex bot no result"
        return None, None  # stopped: someone else won the race
    finally:
        with accounts_lock:
            if acc_id in accounts:
                accounts[acc_id]["pending"].pop(req_id, None)
                alk = accounts[acc_id].get("alex_dm_lock")
                if alk:
                    with alk:
                        q = accounts[acc_id].get("alex_dm_queue", [])
                        if req_id in q:
                            q.remove(req_id)


def alex_race(link):
    """
    Race the Alex HTTP API against @alexbypassbot (Telegram DM).
    Whichever answers first wins. Returns (winner_dict|None, error_str|None).

    The alex_route toggle selects which racers run — alex branch ONLY:
    race (default) = API + bot compete · bot = @alexbypassbot only, the HTTP
    API is never called · api = HTTP API only, no Telegram DM is ever sent.
    """
    route = str(bot_settings.get("alex_route", "race")).strip().lower()
    if route not in ("race", "bot", "api"):
        route = "race"
    use_api = route in ("race", "api")
    use_bot = route in ("race", "bot") and bot_settings.get("alex_bot", True)

    race_event  = threading.Event()
    winner      = {}
    win_lock    = threading.Lock()
    errors      = {}
    errors_lock = threading.Lock()
    error_goal  = 1

    def _record_error(source, message):
        nonlocal error_goal
        with errors_lock:
            errors[source] = message
            should_stop = len(errors) >= error_goal
        if should_stop:
            race_event.set()

    def _declare(source, url, extra=None):
        with win_lock:
            if race_event.is_set():
                return
            winner['source'] = source
            winner['url']    = url
            winner['extra']  = extra or {}
            race_event.set()

    # ── Racer 1: Alex HTTP API ────────────────────────────────────────
    def _api_racer():
        res, err = alex_bypass(link)
        if res:
            _declare(
                "alex",
                res["bypassed"],
                {
                    "module": res.get("raw_type", "alex_api"),
                    "account": "alex_api",
                },
            )
        else:
            _record_error("api", err)

    threads = [threading.Thread(target=_api_racer, daemon=True)] if use_api else []

    # ── Racer 2: @alexbypassbot DM ────────────────────────────────────
    acc_id = None
    req_id = None
    req_entry = None
    acc_state = None

    if use_bot:
        acc_id, acc_state = get_next_active()
        error_goal = int(use_api) + int(bool(acc_state))
        if acc_state:
            def _bot_racer():
                r, err = _alex_bot_roundtrip(link, stop_event=race_event,
                                             acc=(acc_id, acc_state))
                if race_event.is_set():
                    return
                if r:
                    _declare(
                        "alex_bot",
                        r["bypassed"],
                        {
                            "module": "alex_bot",
                            "account": r["account"],
                        },
                    )
                elif err:
                    _record_error("bot", err)
                # err None + no result = stopped after someone else won → silent

            threads.append(threading.Thread(target=_bot_racer, daemon=True))
        else:
            errors['bot'] = "No active Telegram account for Alex bot"

    # No active racer (route=bot with no Telegram account, or the bot switch
    # is off while the API is skipped) must fail fast instead of sitting
    # through the whole race budget.
    if not threads and not winner.get("url"):
        if route == "bot" and not bot_settings.get("alex_bot", True):
            errors.setdefault("bot", "Alex DM bot is disabled — enable it in /admin")
        msg = " | ".join(f"{k}: {v}" for k, v in errors.items() if v) or \
            f"Alex resolver has no active racer (route={route})"
        return None, msg

    for t in threads:
        t.start()

    # Racers use idle deadlines, but the caller still needs an absolute safety
    # cap if a worker crashes before it records an error.
    race_budget = _alex_race_budget()
    if not race_event.wait(timeout=race_budget):
        race_event.set()
        with errors_lock:
            errors.setdefault("race", "Alex resolver timed out")

    # Cleanup the Alex DM queue entry
    if acc_id and req_id:
        with accounts_lock:
            if acc_id in accounts:
                accounts[acc_id]["pending"].pop(req_id, None)
                alk = accounts[acc_id].get("alex_dm_lock")
                if alk:
                    with alk:
                        q = accounts[acc_id].get("alex_dm_queue", [])
                        if req_id in q:
                            q.remove(req_id)

    if winner.get('url'):
        return winner, None

    msg = " | ".join(f"{k}: {v}" for k, v in errors.items() if v) or f"Alex resolver failed (route={route})"
    return None, msg


# ==================== FLASK APP ====================
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.json.sort_keys = False


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get('X-Admin-Token') or flask_session.get('admin_token')
        if not token or token not in admin_tokens:
            if request.path.startswith('/admin/api/'):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return wrapper


# ==================== ADMIN HTML ====================
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Felix Admin Panel</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #030712; --bg2: #0d1117; --bg3: #111827;
    --violet: #8b5cf6; --cyan: #06b6d4; --pink: #ec4899;
    --green: #22c55e; --amber: #f59e0b; --red: #ef4444; --blue: #3b82f6;
    --text: #e2e8f0; --muted: #64748b; --border: #1e293b;
    --card: rgba(255,255,255,0.03); --card-border: rgba(255,255,255,0.07);
  }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; overflow-x: hidden; }
  .bubbles { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0; }
  .bubble { position: absolute; border-radius: 50%; opacity: 0.15; animation: float linear infinite; }
  @keyframes float { 0%{transform:translateY(100vh) scale(0);opacity:0} 10%{opacity:0.15} 90%{opacity:0.1} 100%{transform:translateY(-100px) scale(1);opacity:0} }
  .login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; position: relative; z-index: 10; padding: 1rem; }
  .login-card { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1); backdrop-filter: blur(24px); border-radius: 1.25rem; padding: 2.5rem; width: 100%; max-width: 400px; position: relative; overflow: hidden; box-shadow: 0 25px 50px rgba(0,0,0,0.8); animation: slideUp 0.5s ease; }
  @keyframes slideUp { from{opacity:0;transform:translateY(24px) scale(0.97)} to{opacity:1;transform:none} }
  .login-card::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:linear-gradient(90deg,var(--violet),var(--cyan),var(--pink)); }
  .login-icon { width:64px; height:64px; background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:1rem; display:flex; align-items:center; justify-content:center; margin:0 auto 1.5rem; font-size:2rem; }
  .login-title { text-align:center; font-size:1.75rem; font-weight:900; text-transform:uppercase; letter-spacing:.1em; background:linear-gradient(90deg,var(--violet),var(--cyan),var(--pink)); background-size:200%; -webkit-background-clip:text; -webkit-text-fill-color:transparent; animation:grad 6s ease infinite; background-clip:text; margin-bottom:.25rem; }
  @keyframes grad { 0%,100%{background-position:0% 50%} 50%{background-position:100% 50%} }
  .login-sub { text-align:center; color:var(--muted); font-family:'JetBrains Mono',monospace; font-size:.75rem; letter-spacing:.15em; margin-bottom:2rem; }
  .form-group { margin-bottom:1rem; }
  .form-input { width:100%; background:rgba(0,0,0,0.4); border:1px solid rgba(255,255,255,0.1); border-radius:.75rem; padding:1rem; color:var(--text); font-family:'JetBrains Mono',monospace; font-size:.9rem; letter-spacing:.2em; text-align:center; outline:none; transition:border-color .2s; }
  .form-input:focus { border-color:var(--violet); box-shadow:0 0 0 2px rgba(139,92,246,0.2); }
  .btn-primary { width:100%; padding:1rem; background:linear-gradient(135deg,var(--violet),var(--cyan)); border:none; border-radius:.75rem; color:white; font-weight:700; font-size:.85rem; letter-spacing:.15em; text-transform:uppercase; cursor:pointer; transition:opacity .2s,transform .1s; box-shadow:0 0 20px rgba(139,92,246,0.4); }
  .btn-primary:hover { opacity:.9; }
  .btn-primary:active { transform:scale(.98); }
  .btn-primary:disabled { opacity:.5; cursor:not-allowed; }
  .err-msg { color:var(--red); font-size:.8rem; margin-top:.75rem; text-align:center; font-family:'JetBrains Mono',monospace; }
  .app { display:none; min-height:100vh; }
  .app.visible { display:flex; }
  .sidebar { width:240px; min-height:100vh; background:var(--bg2); border-right:1px solid var(--border); display:flex; flex-direction:column; position:sticky; top:0; height:100vh; overflow-y:auto; z-index:10; }
  .sidebar-logo { padding:1.5rem 1.25rem; border-bottom:1px solid var(--border); }
  .sidebar-logo-text { font-weight:900; font-size:1rem; text-transform:uppercase; letter-spacing:.1em; background:linear-gradient(90deg,var(--violet),var(--cyan)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; animation:grad 6s ease infinite; background-size:200%; }
  .sidebar-sub { font-size:.65rem; color:var(--muted); font-family:'JetBrains Mono',monospace; letter-spacing:.1em; margin-top:.2rem; }
  .sidebar-nav { flex:1; padding:.75rem; }
  .nav-item { display:flex; align-items:center; gap:.75rem; padding:.65rem .875rem; border-radius:.625rem; color:var(--muted); font-size:.85rem; font-weight:500; cursor:pointer; transition:all .15s; margin-bottom:.25rem; border:none; background:none; width:100%; text-align:left; }
  .nav-item:hover { background:rgba(255,255,255,0.04); color:var(--text); }
  .nav-item.active { background:rgba(139,92,246,0.15); color:var(--violet); border:1px solid rgba(139,92,246,0.2); }
  .nav-icon { font-size:1rem; width:1.25rem; text-align:center; }
  .sidebar-footer { padding:.75rem; border-top:1px solid var(--border); }
  .btn-logout { display:flex; align-items:center; gap:.75rem; padding:.65rem .875rem; border-radius:.625rem; color:var(--red); font-size:.85rem; font-weight:500; cursor:pointer; transition:all .15s; border:none; background:none; width:100%; }
  .btn-logout:hover { background:rgba(239,68,68,0.1); }
  .main { flex:1; overflow-y:auto; background:var(--bg); position:relative; z-index:5; }
  .page { display:none; padding:2rem; max-width:1200px; }
  .page.active { display:block; }
  .page-header { margin-bottom:2rem; }
  .page-title { font-size:1.75rem; font-weight:900; text-transform:uppercase; letter-spacing:.08em; background:linear-gradient(90deg,var(--violet),var(--cyan),var(--pink)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; animation:grad 6s ease infinite; background-size:200%; }
  .page-sub { color:var(--muted); font-family:'JetBrains Mono',monospace; font-size:.75rem; letter-spacing:.1em; margin-top:.25rem; }
  .glass { background:var(--card); border:1px solid var(--card-border); backdrop-filter:blur(12px); border-radius:1rem; }
  .stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:1rem; margin-bottom:2rem; }
  .stat-card { padding:1.25rem; position:relative; overflow:hidden; }
  .stat-card-icon { font-size:1.5rem; margin-bottom:.75rem; }
  .stat-card-value { font-size:2rem; font-weight:900; margin-bottom:.25rem; }
  .stat-card-label { font-size:.7rem; color:var(--muted); font-family:'JetBrains Mono',monospace; text-transform:uppercase; letter-spacing:.1em; }
  .badge { display:inline-flex; align-items:center; gap:.35rem; padding:.25rem .625rem; border-radius:.375rem; font-size:.7rem; font-weight:700; font-family:'JetBrains Mono',monospace; text-transform:uppercase; letter-spacing:.08em; border:1px solid transparent; }
  .badge-active { background:rgba(34,197,94,.12); color:var(--green); border-color:rgba(34,197,94,.25); }
  .badge-paused { background:rgba(245,158,11,.12); color:var(--amber); border-color:rgba(245,158,11,.25); }
  .badge-error { background:rgba(239,68,68,.12); color:var(--red); border-color:rgba(239,68,68,.25); }
  .badge-connecting { background:rgba(59,130,246,.12); color:var(--blue); border-color:rgba(59,130,246,.25); }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .dot-active { background:var(--green); box-shadow:0 0 6px var(--green); animation:pulse 2s infinite; }
  .dot-paused { background:var(--amber); }
  .dot-error { background:var(--red); box-shadow:0 0 6px var(--red); animation:pulse 1s infinite; }
  .dot-connecting { background:var(--blue); animation:spin 1s linear infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  @keyframes spin { to{transform:rotate(360deg)} }
  .table-wrap { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; }
  th { padding:.875rem 1.25rem; text-align:left; font-size:.7rem; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; font-family:'JetBrains Mono',monospace; border-bottom:1px solid var(--border); background:rgba(255,255,255,.02); }
  td { padding:.875rem 1.25rem; font-size:.85rem; border-bottom:1px solid rgba(255,255,255,.04); vertical-align:middle; }
  tr:last-child td { border-bottom:none; }
  tr:hover td { background:rgba(255,255,255,.02); }
  .btn { display:inline-flex; align-items:center; gap:.4rem; padding:.4rem .75rem; border-radius:.5rem; font-size:.78rem; font-weight:600; cursor:pointer; transition:all .15s; border:1px solid transparent; font-family:inherit; }
  .btn-sm { padding:.3rem .6rem; font-size:.72rem; }
  .btn-violet { background:rgba(139,92,246,.15); color:var(--violet); border-color:rgba(139,92,246,.3); }
  .btn-violet:hover { background:rgba(139,92,246,.25); }
  .btn-cyan { background:rgba(6,182,212,.15); color:var(--cyan); border-color:rgba(6,182,212,.3); }
  .btn-cyan:hover { background:rgba(6,182,212,.25); }
  .btn-green { background:rgba(34,197,94,.15); color:var(--green); border-color:rgba(34,197,94,.3); }
  .btn-green:hover { background:rgba(34,197,94,.25); }
  .btn-amber { background:rgba(245,158,11,.15); color:var(--amber); border-color:rgba(245,158,11,.25); }
  .btn-amber:hover { background:rgba(245,158,11,.25); }
  .btn-red { background:rgba(239,68,68,.15); color:var(--red); border-color:rgba(239,68,68,.3); }
  .btn-red:hover { background:rgba(239,68,68,.25); }
  .btn-full { width:100%; justify-content:center; padding:.75rem; font-size:.85rem; }
  .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.7); backdrop-filter:blur(8px); z-index:1000; align-items:center; justify-content:center; padding:1rem; }
  .modal-overlay.open { display:flex; }
  .modal { background:var(--bg2); border:1px solid rgba(255,255,255,.1); border-radius:1.25rem; width:100%; max-width:480px; overflow:hidden; box-shadow:0 25px 50px rgba(0,0,0,.8); animation:slideUp .3s ease; }
  .modal-header { padding:1.25rem 1.5rem; border-bottom:1px solid var(--border); background:rgba(255,255,255,.02); }
  .modal-title { font-weight:800; text-transform:uppercase; letter-spacing:.08em; font-size:1rem; background:linear-gradient(90deg,var(--violet),var(--cyan)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
  .modal-body { padding:1.5rem; }
  .modal-footer { padding:1rem 1.5rem; border-top:1px solid var(--border); display:flex; gap:.75rem; justify-content:flex-end; }
  .field { margin-bottom:1rem; }
  .field label { display:block; font-size:.72rem; color:var(--muted); font-family:'JetBrains Mono',monospace; text-transform:uppercase; letter-spacing:.1em; margin-bottom:.4rem; }
  .field input, .field textarea { width:100%; background:rgba(0,0,0,.4); border:1px solid rgba(255,255,255,.1); border-radius:.625rem; padding:.65rem .875rem; color:var(--text); font-family:'JetBrains Mono',monospace; font-size:.8rem; outline:none; transition:border-color .2s; resize:vertical; }
  .field input:focus, .field textarea:focus { border-color:var(--violet); box-shadow:0 0 0 2px rgba(139,92,246,.15); }
  select { background:rgba(0,0,0,.4); border:1px solid rgba(255,255,255,.1); border-radius:.5rem; padding:.45rem .6rem; color:var(--text); font-family:'JetBrains Mono',monospace; font-size:.78rem; outline:none; }
  select:focus { border-color:var(--violet); }
  option { background:#111; }
  .osint-chip { display:inline-flex; align-items:center; gap:.3rem; padding:.3rem .6rem; margin:.15rem; border-radius:.5rem; background:rgba(0,0,0,.3); border:1px solid rgba(255,255,255,.12); font-size:.72rem; font-family:'JetBrains Mono',monospace; cursor:pointer; white-space:nowrap; }
  .osint-chip input { accent-color:var(--green); }
  .field-row { display:grid; grid-template-columns:1fr 1fr; gap:.75rem; }
  .section-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem; flex-wrap:wrap; gap:.75rem; }
  .actions-bar { display:flex; gap:.5rem; flex-wrap:wrap; }
  .text-mono { font-family:'JetBrains Mono',monospace; }
  .text-muted { color:var(--muted); }
  .text-xs { font-size:.72rem; }
  .empty-state { padding:3rem; text-align:center; color:var(--muted); font-family:'JetBrains Mono',monospace; font-size:.75rem; text-transform:uppercase; letter-spacing:.1em; }
  .toast-container { position:fixed; bottom:1.5rem; right:1.5rem; z-index:9999; display:flex; flex-direction:column; gap:.5rem; }
  .toast { padding:.75rem 1.25rem; border-radius:.625rem; font-size:.82rem; font-weight:600; animation:slideUp .3s ease; max-width:300px; border:1px solid transparent; }
  .toast-success { background:rgba(34,197,94,.15); color:var(--green); border-color:rgba(34,197,94,.3); }
  .toast-error { background:rgba(239,68,68,.15); color:var(--red); border-color:rgba(239,68,68,.3); }
  .refresh-indicator { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--cyan); animation:pulse 1s infinite; margin-left:.5rem; }
  .toggle-switch { position:relative; width:44px; height:24px; }
  .toggle-switch input { opacity:0; width:0; height:0; }
  .toggle-slider { position:absolute; cursor:pointer; inset:0; background:#374151; border-radius:24px; transition:.3s; }
  .toggle-slider::before { position:absolute; content:""; height:18px; width:18px; left:3px; bottom:3px; background:#fff; border-radius:50%; transition:.3s; }
  input:checked + .toggle-slider { background:var(--green); }
  input:checked + .toggle-slider::before { transform:translateX(20px); }
  @media (max-width:640px) { .sidebar{width:56px} .sidebar-logo-text,.sidebar-sub,.nav-item span{display:none} .nav-item{justify-content:center} .page{padding:1rem} }
</style>
</head>
<body>
<div class="bubbles" id="bubbles"></div>

<!-- Login -->
<div class="login-wrap" id="loginScreen">
  <div class="login-card">
    <div class="login-icon">🔐</div>
    <div class="login-title">Felix Admin</div>
    <div class="login-sub">SECURE TERMINAL ACCESS</div>
    <form id="loginForm">
      <div class="form-group">
        <input class="form-input" type="password" id="passwordInput" placeholder="Enter access code" autocomplete="off">
      </div>
      <button class="btn-primary" type="submit" id="loginBtn">INITIALIZE</button>
      <div class="err-msg" id="loginErr" style="display:none"></div>
    </form>
  </div>
</div>

<!-- Main App -->
<div class="app" id="mainApp">
  <nav class="sidebar">
    <div class="sidebar-logo">
      <div class="sidebar-logo-text">Felix Admin</div>
      <div class="sidebar-sub">BYPASS CONTROL</div>
    </div>
    <div class="sidebar-nav">
      <button class="nav-item active" onclick="showPage('dashboard')" id="nav-dashboard"><span class="nav-icon">📊</span><span>Dashboard</span></button>
      <button class="nav-item" onclick="showPage('accounts')" id="nav-accounts"><span class="nav-icon">👤</span><span>Accounts</span></button>
      <button class="nav-item" onclick="showPage('bots')" id="nav-bots"><span class="nav-icon">⚡</span><span>Bots</span></button>
      <button class="nav-item" onclick="showPage('osint')" id="nav-osint"><span class="nav-icon">🛰️</span><span>OSINT</span></button>
    </div>
    <div class="sidebar-footer">
      <button class="btn-logout" onclick="logout()"><span class="nav-icon">🚪</span><span>Logout</span></button>
    </div>
  </nav>

  <main class="main">
    <!-- Dashboard -->
    <div class="page active" id="page-dashboard">
      <div class="page-header">
        <div class="page-title">System Overview</div>
        <div class="page-sub">LIVE STATUS <span class="refresh-indicator"></span></div>
      </div>
      <div class="stat-grid" id="statGrid"></div>
      <div class="section-header">
        <div style="font-weight:700;font-size:.9rem;">Quick Actions</div>
        <div class="actions-bar">
          <button class="btn btn-green" onclick="apiCall('POST','/admin/api/resume-all').then(()=>{toast('All resumed','success');refresh()})">▶ Resume All</button>
          <button class="btn btn-amber" onclick="apiCall('POST','/admin/api/pause-all').then(()=>{toast('All paused','success');refresh()})">⏸ Pause All</button>
        </div>
      </div>
      <div class="glass" style="margin-top:1rem">
        <table>
          <thead><tr><th>Account</th><th>Status</th><th>Bypasses</th><th>Win Rate</th><th>Last Used</th></tr></thead>
          <tbody id="dashTable"><tr><td colspan="5" class="empty-state">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- Accounts -->
    <div class="page" id="page-accounts">
      <div class="page-header">
        <div class="page-title">Accounts</div>
        <div class="page-sub">MANAGE TELEGRAM SESSIONS</div>
      </div>
      <div class="section-header">
        <div class="actions-bar">
          <button class="btn btn-violet" onclick="openAddModal()">➕ Add Account</button>
          <button class="btn btn-green" onclick="apiCall('POST','/admin/api/resume-all').then(()=>{toast('Resumed','success');refresh()})">▶ Resume All</button>
          <button class="btn btn-amber" onclick="apiCall('POST','/admin/api/pause-all').then(()=>{toast('Paused','success');refresh()})">⏸ Pause All</button>
        </div>
      </div>
      <div class="glass" style="margin-top:1rem">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Account</th><th>Status</th><th>Stats</th><th>Error</th><th>Actions</th></tr></thead>
            <tbody id="accTable"><tr><td colspan="5" class="empty-state">Loading...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Bot Toggles -->
    <div class="page" id="page-bots">
      <div class="page-header">
        <div class="page-title">Bot Control</div>
        <div class="page-sub">ENABLE / DISABLE RACE BOTS — AT LEAST ONE MUST STAY ON</div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-bottom:1.5rem;">

        <!-- DZHQ Card -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(139,92,246,.15);border:1px solid rgba(139,92,246,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🏆</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">DZHQ Group Bot</div>
              <div class="text-xs text-muted text-mono">@DZHQ_BypassBot · Group chat</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="dzhqLabel">Enabled</div>
              <div class="text-xs text-muted text-mono">Sends /b link to DZHQ group</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="dzhqToggle" onchange="toggleBot('dzhq', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div style="margin-top:.875rem;font-size:.75rem;color:var(--muted);line-height:1.6;">
            Races in parallel with Nick. Whichever replies first wins. If disabled, only Nick runs.
          </div>
        </div>

        <!-- Nick Card -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(6,182,212,.15);border:1px solid rgba(6,182,212,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">⚡</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Nick DM Bot</div>
              <div class="text-xs text-muted text-mono">@Nick_Bypass_Bot · Direct message</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="nickLabel">Enabled</div>
              <div class="text-xs text-muted text-mono">Sends link via private DM</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="nickToggle" onchange="toggleBot('nick', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div style="margin-top:.875rem;font-size:.75rem;color:var(--muted);line-height:1.6;">
            Races in parallel with DZHQ. If disabled, only DZHQ runs.
          </div>
        </div>

        <!-- Alex Bot Card -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(236,72,153,.15);border:1px solid rgba(236,72,153,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🅰️</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Alex DM Bot</div>
              <div class="text-xs text-muted text-mono">@alexbypassbot · Direct message</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="alexBotLabel">Enabled</div>
              <div class="text-xs text-muted text-mono">Races the Alex API on URLKing / MonteOlympus links</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="alexBotToggle" onchange="toggleBot('alex_bot', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div style="margin-top:.75rem;">
            <label style="font-size:.72rem;color:var(--muted);display:block;margin-bottom:.375rem;letter-spacing:.04em;">ROUTE — WHERE SHOULD URLKING / MONTEOLYMPUS LINKS GO?</label>
            <select id="alexRoute" onchange="setAlexRoute(this.value)" style="width:100%;padding:.625rem .75rem;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.12);border-radius:.5rem;color:var(--text);font-size:.82rem;">
              <option value="race">⚡ Race — API + @alexbypassbot (fastest wins)</option>
              <option value="bot">🤖 Bot only — @alexbypassbot (API skipped)</option>
              <option value="api">🌐 API only — Alex HTTP API (no Telegram DM)</option>
            </select>
            <div class="text-xs text-mono" id="alexRouteDesc" style="margin-top:.5rem;color:var(--muted);line-height:1.6;"></div>
          </div>
          <div style="margin-top:.875rem;font-size:.75rem;color:var(--muted);line-height:1.6;">
            On = Alex API aur @alexbypassbot dono ek saath try karte hain — jo pehle jawab de wohi jeetta hai. Off = sirf Alex API.
          </div>
        </div>
      </div>


        <!-- Bypass Priorities Card -->
        <div class="glass" style="padding:1.5rem;grid-column:1/-1;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(56,189,248,.12);border:1px solid rgba(56,189,248,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🎯</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Bypass Priorities</div>
              <div class="text-xs text-muted text-mono">pattern → fixed resolver · first match wins · strict (no fallback to other bots)</div>
            </div>
          </div>
          <div class="text-xs text-muted text-mono" style="margin-bottom:.75rem;">EXAMPLE: TIPSGURU.IN → BOT ONLY SENDS EVERY TIPSGURU LINK TO @ALEXBYPASSBOT</div>
          <div id="prioList"></div>
          <div style="display:flex;gap:.5rem;margin-top:.75rem;flex-wrap:wrap;">
            <button class="btn btn-green" onclick="addPriorityRow()">＋ Add rule</button>
            <button class="btn btn-green" onclick="savePriorities()">💾 Save priorities</button>
            <button class="btn btn-amber" onclick="resetPriorities()">↺ Reset to env</button>
          </div>
        </div>

        <!-- DZHQ First Card -->
        <div class="glass" style="padding:1.5rem;grid-column:1/-1;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">⚙️</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">DZHQ First Mode</div>
              <div class="text-xs text-muted text-mono">DZHQ pehle koshish karta hai — Nick sirf tab kaam karta hai jab DZHQ fail ho</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="dzhqFirstLabel">Disabled (Parallel Race)</div>
              <div class="text-xs text-muted text-mono">Off = Both run together &nbsp;|&nbsp; On = DZHQ first, Nick as fallback</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="dzhqFirstToggle" onchange="toggleBot('dzhq_first', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div style="margin-top:.875rem;display:grid;grid-template-columns:1fr 1fr;gap:.75rem;">
            <div style="padding:.75rem 1rem;background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.2);border-radius:.5rem;font-size:.75rem;color:var(--muted);line-height:1.7;">
              <div style="color:var(--text);font-weight:600;margin-bottom:.25rem;">🔴 OFF — Parallel Race</div>
              DZHQ + Nick dono ek saath chaltay hain. Jo pehle jawab de woh jeet jaata hai.
            </div>
            <div style="padding:.75rem 1rem;background:rgba(251,191,36,.06);border:1px solid rgba(251,191,36,.2);border-radius:.5rem;font-size:.75rem;color:var(--muted);line-height:1.7;">
              <div style="color:var(--text);font-weight:600;margin-bottom:.25rem;">🟡 ON — DZHQ First</div>
              DZHQ pehle koshish karta hai. Agar DZHQ succeed kare to Nick ko message hi nahi jaata. DZHQ fail ho to Nick fallback karta hai.
            </div>
          </div>
        </div>

        <!-- Nick First Card -->
        <div class="glass" style="padding:1.5rem;grid-column:1/-1;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(56,189,248,.12);border:1px solid rgba(56,189,248,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">⚡</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Nick First Mode</div>
              <div class="text-xs text-muted text-mono">Nick pehle koshish karta hai — DZHQ sirf tab kaam karta hai jab Nick fail ho</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="nickFirstLabel">Disabled (Parallel Race)</div>
              <div class="text-xs text-muted text-mono">Off = Both run together &nbsp;|&nbsp; On = Nick first, DZHQ as fallback</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="nickFirstToggle" onchange="toggleBot('nick_first', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>

        <!-- Random Mode Card -->
        <div class="glass" style="padding:1.5rem;grid-column:1/-1;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(139,92,246,.12);border:1px solid rgba(139,92,246,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🎲</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Random Mode</div>
              <div class="text-xs text-muted text-mono">Kabhi 5 link Nick pe, kabhi 10 link DZHQ pe — dono bots baari-baari switch hote rehte hain</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="randomModeLabel">Disabled</div>
              <div class="text-xs text-muted text-mono" id="randomModeInfo">Har switch par 3-10 links ka random batch ek hi bot ko jaata hai</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="randomModeToggle" onchange="toggleBot('random_mode', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>

      <!-- Race mode info banner -->
      <div class="glass" style="padding:1.25rem 1.5rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;">
        <div style="font-size:1.5rem;">🏁</div>
        <div>
          <div style="font-weight:700;font-size:.9rem;margin-bottom:.2rem;" id="raceModeText">2-Way Race Active</div>
          <div class="text-xs text-muted text-mono" id="raceModeDesc">Both bots receive the link simultaneously — first valid response wins and is returned to the caller.</div>
        </div>
        <div id="raceBadge" class="badge badge-active" style="margin-left:auto;">RACING</div>
      </div>
    </div>

    <!-- OSINT Routing -->
    <div class="page" id="page-osint">
      <div class="page-header">
        <div class="page-title">OSINT Routing</div>
        <div class="page-sub">PER-COMMAND GROUP + MODE CONTROL — API STAYS COMMAND-ONLY</div>
      </div>
      <div class="glass" style="padding:1.5rem;margin-bottom:1rem;">
        <div style="font-weight:700;font-size:.9rem;margin-bottom:1rem;">🌍 Global Defaults</div>
        <div class="field-row">
          <div class="field"><label>Default Mode</label>
            <select id="osintDefaultMode">
              <option value="single">Single — fixed group per command</option>
              <option value="race">Race — all groups, first reply wins</option>
            </select>
          </div>
          <div class="field"><label>Timeout Override (sec, blank = env)</label>
            <input type="number" id="osintTimeout" min="5" step="1" placeholder="e.g. 55">
          </div>
        </div>
        <div class="text-xs text-muted text-mono" id="osintGroupsLine"></div>
      </div>
      <div class="glass" style="padding:1.5rem;margin-bottom:1rem;">
        <div style="font-weight:700;font-size:.9rem;margin-bottom:.5rem;">🤖 Group Responders</div>
        <div class="text-xs text-muted text-mono" style="margin-bottom:.75rem;">NUMERIC TELEGRAM ID OF EACH GROUP'S RESPONDER BOT — GROUPS MAY HAVE DIFFERENT BOTS — BLANK = ACCEPT ANY SENDER</div>
        <div id="osintResponders"></div>
      </div>
      <div class="glass" style="padding:1.5rem;">
        <div class="section-header">
          <div style="font-weight:700;font-size:.9rem;">📡 Per-Command Rules</div>
          <div class="actions-bar">
            <button class="btn btn-green" onclick="saveOsintSettings()">💾 Save Routing</button>
            <button class="btn btn-amber" onclick="resetOsintSettings()">↺ Reset</button>
          </div>
        </div>
        <div class="text-xs text-muted text-mono" style="margin-bottom:.75rem;">MODE = INHERIT FOLLOWS GLOBAL DEFAULT · NO GROUP TICKED = INHERIT · BLANK TIMEOUT = INHERIT</div>
        <div class="table-wrap"><table>
          <thead><tr><th>Command</th><th>Enabled</th><th>Mode</th><th>Groups</th><th>Timeout</th></tr></thead>
          <tbody id="osintTable"><tr><td colspan="5" class="empty-state">Loading...</td></tr></tbody>
        </table></div>
      </div>
    </div>

  </main>
</div>

<!-- Add Account Modal -->
<div class="modal-overlay" id="addModal">
  <div class="modal">
    <div class="modal-header"><div class="modal-title">Initialize Account</div></div>
    <div class="modal-body">
      <div class="field"><label>Account Name / Label</label><input type="text" id="newName" placeholder="e.g. Worker-01"></div>
      <div class="field-row">
        <div class="field"><label>API ID</label><input type="number" id="newApiId" placeholder="12345678"></div>
        <div class="field"><label>API Hash</label><input type="text" id="newApiHash" placeholder="abcdef..."></div>
      </div>
      <div class="field"><label>Session String</label><textarea id="newSession" rows="5" placeholder="Paste Telethon session string..."></textarea></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('addModal')">Cancel</button>
      <button class="btn btn-violet" onclick="submitAddAccount()">Deploy</button>
    </div>
  </div>
</div>

<!-- Update Session Modal -->
<div class="modal-overlay" id="sessionModal">
  <div class="modal">
    <div class="modal-header"><div class="modal-title" id="sessionModalTitle">Update Session</div></div>
    <div class="modal-body">
      <div class="field"><label>New Session String</label><textarea id="newSessionStr" rows="6" placeholder="Paste new Telethon session string here..."></textarea></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('sessionModal')">Cancel</button>
      <button class="btn btn-cyan" onclick="submitUpdateSession()">Update Session</button>
    </div>
  </div>
</div>

<div class="toast-container" id="toastContainer"></div>

<script>
const TOKEN_KEY = 'felix_admin_token';
let token = localStorage.getItem(TOKEN_KEY) || '';
let currentSessionAccId = null;
let refreshTimer = null;

// Bubbles
(function() {
  const colors = ['#8b5cf6','#06b6d4','#ec4899','#3b82f6','#22c55e','#f59e0b'];
  const wrap = document.getElementById('bubbles');
  for (let i = 0; i < 28; i++) {
    const b = document.createElement('div');
    b.className = 'bubble';
    const size = Math.random()*60+20;
    b.style.cssText = `width:${size}px;height:${size}px;left:${Math.random()*100}%;background:${colors[Math.floor(Math.random()*colors.length)]};animation-duration:${Math.random()*15+10}s;animation-delay:${Math.random()*-20}s;filter:blur(${Math.random()*3+1}px);`;
    wrap.appendChild(b);
  }
})();

async function apiCall(method, path, body) {
  const opts = { method, headers: { 'X-Admin-Token': token, 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (r.status === 401) { logout(); throw new Error('Unauthorized'); }
  return r.json();
}

function toast(msg, type='success') {
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  document.getElementById('toastContainer').appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

if (token) showApp();

document.getElementById('loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const pw = document.getElementById('passwordInput').value;
  const btn = document.getElementById('loginBtn');
  btn.disabled = true; btn.textContent = 'AUTHENTICATING...';
  document.getElementById('loginErr').style.display = 'none';
  try {
    const r = await fetch('/admin/api/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pw}) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Access denied');
    token = d.token;
    localStorage.setItem(TOKEN_KEY, token);
    showApp();
  } catch(e) {
    const el = document.getElementById('loginErr');
    el.textContent = e.message; el.style.display = 'block';
    document.getElementById('passwordInput').value = '';
  } finally { btn.disabled = false; btn.textContent = 'INITIALIZE'; }
});

function showApp() {
  document.getElementById('loginScreen').style.display = 'none';
  document.getElementById('mainApp').classList.add('visible');
  startRefresh();
  loadBotsStatus();
  loadOsintSettings();
  loadPriorities();
}

function logout() {
  localStorage.removeItem(TOKEN_KEY); token = '';
  stopRefresh();
  document.getElementById('mainApp').classList.remove('visible');
  document.getElementById('loginScreen').style.display = 'flex';
}

function showPage(name) {
  const pg = document.getElementById('page-'+name);
  const nv = document.getElementById('nav-'+name);
  if (!pg || !nv) { toast('Page not found: ' + name, 'error'); return; }
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  pg.classList.add('active');
  nv.classList.add('active');
}

function startRefresh() { refresh(); refreshTimer = setInterval(refresh, 5000); }
function stopRefresh() { if (refreshTimer) clearInterval(refreshTimer); }

async function refresh() {
  try {
    const data = await apiCall('GET', '/admin/api/accounts');
    renderDashboard(data);
    renderAccounts(data);
  } catch(e) {}
}

function renderDashboard(accs) {
  const total    = accs.length;
  const active   = accs.filter(a=>a.status==='active').length;
  const paused   = accs.filter(a=>a.status==='paused').length;
  const errored  = accs.filter(a=>a.status==='error').length;
  const bypasses = accs.reduce((s,a)=>s+a.bypass_count,0);
  const succ     = accs.reduce((s,a)=>s+a.success_count,0);
  const rate     = bypasses>0 ? ((succ/bypasses)*100).toFixed(1) : 0;
  const stats = [
    {label:'Total Accounts', value:total,    color:'var(--violet)', icon:'👥'},
    {label:'Active',         value:active,   color:'var(--green)',  icon:'✅'},
    {label:'Paused',         value:paused,   color:'var(--amber)',  icon:'⏸'},
    {label:'Error',          value:errored,  color:'var(--red)',    icon:'⚠️'},
    {label:'Total Bypasses', value:bypasses.toLocaleString(), color:'var(--cyan)', icon:'⚡'},
    {label:'Success Rate',   value:rate+'%', color:'var(--pink)',   icon:'🎯'},
  ];
  document.getElementById('statGrid').innerHTML = stats.map(s=>`<div class="glass stat-card"><div class="stat-card-icon">${s.icon}</div><div class="stat-card-value" style="color:${s.color}">${s.value}</div><div class="stat-card-label">${s.label}</div></div>`).join('');
  document.getElementById('dashTable').innerHTML = accs.length ? accs.map(a=>`<tr><td><b>${esc(a.name)}</b>${a.username?`<div class="text-xs text-muted text-mono">@${esc(a.username)}</div>`:''}</td><td>${statusBadge(a.status)}</td><td class="text-mono text-xs">${a.bypass_count.toLocaleString()}</td><td class="text-mono" style="color:var(--cyan)">${a.bypass_count>0?((a.success_count/a.bypass_count)*100).toFixed(1)+'%':'-'}</td><td class="text-xs text-muted">${a.last_used?timeAgo(a.last_used):'Never'}</td></tr>`).join('') : '<tr><td colspan="5" class="empty-state">No accounts</td></tr>';
}

function renderAccounts(accs) {
  document.getElementById('accTable').innerHTML = accs.length ? accs.map(a=>`<tr><td><b>${esc(a.name)}</b>${a.first_name||a.username?`<div class="text-xs text-muted text-mono">${a.first_name?esc(a.first_name):''}${a.username?' @'+esc(a.username):''}</div>`:''}<div class="text-xs text-muted text-mono">ID: ${a.api_id}</div></td><td>${statusBadge(a.status)}</td><td class="text-mono text-xs"><div>Bypasses: <b>${a.bypass_count}</b></div><div style="color:var(--green)">Success: ${a.success_count}</div><div style="color:var(--red)">Failed: ${a.fail_count}</div></td><td class="text-xs" style="color:var(--red);max-width:180px;word-break:break-word">${a.error_message?esc(a.error_message):'-'}</td><td><div style="display:flex;gap:.375rem;flex-wrap:wrap">${a.status==='paused'||a.status==='error'?`<button class="btn btn-sm btn-green" onclick="toggleAcc('${a.id}','resume')">▶ Resume</button>`:`<button class="btn btn-sm btn-amber" onclick="toggleAcc('${a.id}','pause')">⏸ Pause</button>`}<button class="btn btn-sm btn-cyan" onclick="openSessionModal('${a.id}','${esc(a.name)}')">🔑 Session</button><button class="btn btn-sm btn-red" onclick="deleteAcc('${a.id}')">🗑</button></div></td></tr>`).join('') : '<tr><td colspan="5" class="empty-state">No accounts. Click "Add Account" to start.</td></tr>';
}

function statusBadge(s) {
  const cls = {active:'badge-active',paused:'badge-paused',error:'badge-error',connecting:'badge-connecting'};
  const dotCls = {active:'dot-active',paused:'dot-paused',error:'dot-error',connecting:'dot-connecting'};
  return `<span class="badge ${cls[s]||'badge-connecting'}"><span class="dot ${dotCls[s]||'dot-connecting'}"></span>${s}</span>`;
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function timeAgo(iso) {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff/60000);
  if (m < 1)  return 'just now';
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m/60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h/24) + 'd ago';
}

async function toggleAcc(id, action) {
  try { await apiCall('POST', `/admin/api/accounts/${id}/${action}`); toast(`Account ${action}d`,'success'); refresh(); }
  catch(e) { toast(e.message,'error'); }
}
async function deleteAcc(id) {
  if (!confirm('Delete this account?')) return;
  try { await apiCall('DELETE', `/admin/api/accounts/${id}`); toast('Account deleted','success'); refresh(); }
  catch(e) { toast(e.message,'error'); }
}

function openAddModal() {
  ['newName','newApiId','newApiHash','newSession'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('addModal').classList.add('open');
}
async function submitAddAccount() {
  const name=document.getElementById('newName').value.trim();
  const apiId=document.getElementById('newApiId').value.trim();
  const apiHash=document.getElementById('newApiHash').value.trim();
  const sess=document.getElementById('newSession').value.trim();
  if (!name||!apiId||!apiHash||!sess){toast('All fields required','error');return;}
  try { await apiCall('POST','/admin/api/accounts',{name,api_id:parseInt(apiId),api_hash:apiHash,session_string:sess}); toast('Account added!','success'); closeModal('addModal'); refresh(); }
  catch(e){toast(e.message,'error');}
}

function openSessionModal(id,name) {
  currentSessionAccId=id;
  document.getElementById('sessionModalTitle').textContent=`Update Session: ${name}`;
  document.getElementById('newSessionStr').value='';
  document.getElementById('sessionModal').classList.add('open');
}
async function submitUpdateSession() {
  const sess=document.getElementById('newSessionStr').value.trim();
  if (!sess){toast('Session string required','error');return;}
  try { await apiCall('PUT',`/admin/api/accounts/${currentSessionAccId}/session`,{session_string:sess}); toast('Session updated! Reconnecting...','success'); closeModal('sessionModal'); refresh(); }
  catch(e){toast(e.message,'error');}
}

function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(el=>{ el.addEventListener('click',e=>{if(e.target===el)el.classList.remove('open');}); });

// ── Bot Toggles ───────────────────────────────────────────────────────
async function loadBotsStatus() {
  try {
    const d = await apiCall('GET', '/admin/api/bots/status');
    _applyBotState(d);
  } catch(e) {}
}

function _applyBotState(d) {
  const dEl    = document.getElementById('dzhqToggle');
  const nEl    = document.getElementById('nickToggle');
  const dfEl   = document.getElementById('dzhqFirstToggle');
  const nfEl   = document.getElementById('nickFirstToggle');
  const rmEl   = document.getElementById('randomModeToggle');
  const nfLbl  = document.getElementById('nickFirstLabel');
  const rmLbl  = document.getElementById('randomModeLabel');
  const rmInfo = document.getElementById('randomModeInfo');
  const abEl   = document.getElementById('alexBotToggle');
  const abLbl  = document.getElementById('alexBotLabel');
  const dLbl   = document.getElementById('dzhqLabel');
  const nLbl   = document.getElementById('nickLabel');
  const dfLbl  = document.getElementById('dzhqFirstLabel');
  const raceText  = document.getElementById('raceModeText');
  const raceDesc  = document.getElementById('raceModeDesc');
  const raceBadge = document.getElementById('raceBadge');

  if (dEl)  dEl.checked  = d.dzhq;
  if (nEl)  nEl.checked  = d.nick;
  if (dfEl) dfEl.checked = !!d.dzhq_first;
  if (nfEl) nfEl.checked = !!d.nick_first;
  if (rmEl) rmEl.checked = !!d.random_mode;
  if (nfLbl) { nfLbl.textContent = d.nick_first ? 'ON — Nick First Mode' : 'Disabled (Parallel Race)'; nfLbl.style.color = d.nick_first ? 'var(--cyan)' : 'var(--muted)'; }
  if (rmLbl) { rmLbl.textContent = d.random_mode ? 'ON — Random Switching' : 'Disabled'; rmLbl.style.color = d.random_mode ? 'var(--green)' : 'var(--muted)'; }
  if (rmInfo) {
    if (d.random_mode && d.random && d.random.current) {
      rmInfo.textContent = `Abhi ${d.random.current.toUpperCase()} chal raha hai — ${d.random.remaining}/${d.random.batch} links baaki, phir doosre bot par switch`;
    } else {
      rmInfo.textContent = 'Har switch par 3-10 links ka random batch ek hi bot ko jaata hai';
    }
  }
  if (abEl) abEl.checked = (d.alex_bot !== false);
  if (abLbl) { const on = (d.alex_bot !== false); abLbl.textContent = on ? 'Enabled' : 'Disabled'; abLbl.style.color = on ? 'var(--green)' : 'var(--red)'; }
  const axRoute = document.getElementById('alexRoute');
  if (axRoute) axRoute.value = d.alex_route || 'race';
  updateAlexRouteDesc(d.alex_route || 'race', (d.alex_bot !== false));

  if (dLbl)  { dLbl.textContent  = d.dzhq       ? 'Enabled'               : 'Disabled'; dLbl.style.color  = d.dzhq       ? 'var(--green)' : 'var(--red)'; }
  if (nLbl)  { nLbl.textContent  = d.nick       ? 'Enabled'               : 'Disabled'; nLbl.style.color  = d.nick       ? 'var(--green)' : 'var(--red)'; }
  if (dfLbl) { dfLbl.textContent = d.dzhq_first ? 'ON — DZHQ First Mode'  : 'Disabled (Parallel Race)'; dfLbl.style.color = d.dzhq_first ? 'var(--amber)' : 'var(--muted)'; }

  if (raceText && raceBadge) {
    if (d.dzhq && d.nick && d.random_mode) {
      raceText.textContent = 'Random Mode — Auto Switching';
      if (raceDesc) raceDesc.textContent = 'Ek bot ko 3-10 links ka batch jaata hai, phir doosre bot par switch ho jaata hai. Fail hone par doosra bot fallback karta hai.';
      raceBadge.className = 'badge badge-active';
      raceBadge.textContent = 'RANDOM';
      raceBadge.style.background = 'rgba(139,92,246,.2)';
      raceBadge.style.color = '#a78bfa';
    } else if (d.dzhq && d.nick && d.nick_first) {
      raceText.textContent = 'Nick First Mode — DZHQ as Fallback';
      if (raceDesc) raceDesc.textContent = 'Nick pehle koshish karta hai. Nick succeed ho to DZHQ ko message nahi jaata. Nick fail ho to DZHQ try karta hai.';
      raceBadge.className = 'badge badge-active';
      raceBadge.textContent = 'NICK FIRST';
      raceBadge.style.background = 'rgba(56,189,248,.2)';
      raceBadge.style.color = '#38bdf8';
    } else if (d.dzhq && d.nick && d.dzhq_first) {
      raceText.textContent = 'DZHQ First Mode — Nick as Fallback';
      if (raceDesc) raceDesc.textContent = 'DZHQ pehle koshish karta hai. DZHQ succeed ho to Nick ko message nahi jaata. DZHQ fail ho to Nick try karta hai.';
      raceBadge.className = 'badge badge-amber';
      raceBadge.textContent = 'DZHQ FIRST';
      raceBadge.style.background = 'rgba(251,191,36,.2)';
      raceBadge.style.color = 'var(--amber)';
    } else if (d.dzhq && d.nick) {
      raceText.textContent = '2-Way Race Active — DZHQ + Nick';
      if (raceDesc) raceDesc.textContent = 'Both bots receive the link simultaneously — first valid response wins.';
      raceBadge.className = 'badge badge-active';
      raceBadge.textContent = 'RACING';
      raceBadge.style.background = '';
      raceBadge.style.color = '';
    } else if (d.dzhq) {
      raceText.textContent = 'Solo Mode — DZHQ Only';
      if (raceDesc) raceDesc.textContent = 'Only DZHQ group bot is active.';
      raceBadge.className = 'badge badge-paused';
      raceBadge.textContent = 'SOLO';
      raceBadge.style.background = '';
      raceBadge.style.color = '';
    } else {
      raceText.textContent = 'Solo Mode — Nick Only';
      if (raceDesc) raceDesc.textContent = 'Only Nick DM bot is active.';
      raceBadge.className = 'badge badge-paused';
      raceBadge.textContent = 'SOLO';
      raceBadge.style.background = '';
      raceBadge.style.color = '';
    }
  }
}

async function toggleBot(bot, enabled) {
  try {
    const d = await apiCall('POST', '/admin/api/bots/toggle', {bot, enabled});
    if (!d.success) { toast(d.error || 'Cannot disable both bots', 'error'); loadBotsStatus(); return; }
    _applyBotState(d);
    const labels = { dzhq: 'DZHQ', nick: 'Nick', dzhq_first: 'DZHQ First Mode', nick_first: 'Nick First Mode', random_mode: 'Random Mode', alex_bot: 'Alex DM Bot' };
    toast(`${labels[bot] || bot} ${enabled ? 'enabled' : 'disabled'}`, 'success');
  } catch(e) { toast(e.message, 'error'); loadBotsStatus(); }
}

function updateAlexRouteDesc(route, botOn) {
  const el = document.getElementById('alexRouteDesc');
  if (!el) return;
  const desc = {
    race: botOn ? '⚡ Race active: API + @alexbypassbot compete, fastest wins.' : '⚡ Route=race but the bot switch is OFF → API only.',
    bot: botOn ? '🤖 Bot only: every Alex link goes to @alexbypassbot, the API is never called.' : '🤖 Route=bot but the bot switch is OFF → resolver disabled.',
    api: '🌐 API only: every Alex link goes to the Alex HTTP API, no Telegram DM is sent.'
  };
  el.textContent = desc[route] || desc.race;
}

async function setAlexRoute(route) {
  try {
    const d = await apiCall('POST', '/admin/api/bots/alex-route', {route});
    if (!d.success) { toast(d.error || 'Invalid route', 'error'); loadBotsStatus(); return; }
    _applyBotState(d);
    toast(`Alex route → ${route}`, 'success');
  } catch(e) { toast(e.message, 'error'); loadBotsStatus(); }
}

let prioRules = [];
let prioTargets = ['dzhq', 'nick', 'alex_bot', 'alex_api', 'alex_race'];
async function loadPriorities() {
  try {
    const d = await apiCall('GET', '/admin/api/bypass/priorities');
    if (!d.success) return;
    prioRules = d.priorities || [];
    if (d._meta && d._meta.targets) prioTargets = d._meta.targets;
    renderPriorities();
  } catch(e) {}
}
function renderPriorities() {
  const box = document.getElementById('prioList');
  if (!box) return;
  const targetLabel = {dzhq: 'DZHQ group only', nick: 'Nick DM only', alex_bot: '@alexbypassbot only', alex_api: 'Alex API only', alex_race: 'Alex race (API + bot)'};
  box.innerHTML = prioRules.map((r, i) => `
    <div class="field-row" data-prio-row="${i}" style="margin-bottom:.5rem;align-items:end;">
      <div class="field" style="margin-bottom:0;flex:2;"><label>Link pattern (matches inside URL)</label>
        <input type="text" data-prio-pattern value="${esc(r.pattern || '')}" placeholder="e.g. tipsguru.in">
      </div>
      <div class="field" style="margin-bottom:0;flex:1;"><label>Resolver</label>
        <select data-prio-target style="width:100%;padding:.625rem .75rem;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.12);border-radius:.5rem;color:var(--text);font-size:.82rem;">
          ${prioTargets.map(t => `<option value="${t}"${t === r.target ? ' selected' : ''}>${targetLabel[t] || t}</option>`).join('')}
        </select>
      </div>
      <div class="field" style="margin-bottom:0;flex:0;"><label>&nbsp;</label>
        <button class="btn btn-amber" onclick="removePriorityRow(${i})">✕</button>
      </div>
    </div>`).join('') || '<div class="empty-state">No priority rules — every link uses the default flow</div>';
}
function readPriorityRows() {
  return [...document.querySelectorAll('#prioList [data-prio-row]')].map(row => ({
    pattern: row.querySelector('[data-prio-pattern]').value.trim(),
    target: row.querySelector('[data-prio-target]').value
  }));
}
function addPriorityRow() {
  prioRules = readPriorityRows();
  prioRules.push({pattern: '', target: 'alex_bot'});
  renderPriorities();
  const inputs = document.querySelectorAll('#prioList [data-prio-pattern]');
  if (inputs.length) inputs[inputs.length - 1].focus();
}
function removePriorityRow(i) {
  prioRules = readPriorityRows();
  prioRules.splice(i, 1);
  renderPriorities();
}
async function savePriorities() {
  try {
    const d = await apiCall('POST', '/admin/api/bypass/priorities', {priorities: readPriorityRows().filter(r => r.pattern !== '')});
    if (!d.success) { toast(d.error || 'Invalid rules', 'error'); return; }
    prioRules = d.priorities || [];
    if (d._meta && d._meta.targets) prioTargets = d._meta.targets;
    renderPriorities();
    toast('Priorities saved', 'success');
  } catch(e) { toast(e.message, 'error'); }
}
async function resetPriorities() {
  try {
    const d = await apiCall('POST', '/admin/api/bypass/priorities/reset', {});
    if (!d.success) { toast(d.error || 'Reset failed', 'error'); return; }
    prioRules = d.priorities || [];
    renderPriorities();
    toast('Priorities reset to env', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

let osintCfg = null;
async function loadOsintSettings() {
  try {
    const d = await apiCall('GET', '/admin/api/osint/settings');
    if (!d.success) return;
    osintCfg = d.settings;
    renderOsintSettings(d.settings);
  } catch(e) {}
}

function renderOsintSettings(s) {
  if (!s) return;
  const meta = s._meta || {};
  const dmEl = document.getElementById('osintDefaultMode');
  const toEl = document.getElementById('osintTimeout');
  const glEl = document.getElementById('osintGroupsLine');
  const tb = document.getElementById('osintTable');
  const respBox = document.getElementById('osintResponders');
  if (!dmEl || !toEl || !glEl || !tb || !respBox) return;
  dmEl.value = s.default_mode || 'single';
  toEl.value = (s.timeout_sec == null ? '' : s.timeout_sec);
  glEl.textContent =
    'GROUPS: ' + (meta.groups || []).join('  ·  ') + '    |    TIMEOUT ENV: ' + (meta.env_timeout_sec || '?') + 's';
  const resp = s.group_responders || {};
  respBox.innerHTML = (meta.groups || []).map(g => {
    const v = resp[g] ?? resp[String(g)] ?? '';
    return `<div class="field-row" style="margin-bottom:.5rem;align-items:end;">
      <div class="field" style="margin-bottom:0;"><label>Group</label>
        <input type="text" value="${g}" disabled style="opacity:.6;">
      </div>
      <div class="field" style="margin-bottom:0;"><label>Responder numeric ID (blank = any)</label>
        <input type="number" data-osint-responder="${g}" value="${v}" placeholder="e.g. 123456789">
      </div>
    </div>`;
  }).join('') || '<div class="empty-state">No groups configured</div>';
  const cmds = meta.allowed_commands || [];
  tb.innerHTML = cmds.map(cmd => {
    const cm = (s.command_modes || {})[cmd];
    const cg = (s.command_groups || {})[cmd] || [];
    const ct = (s.command_timeouts || {})[cmd];
    const en = !(s.disabled_commands || []).includes(cmd);
    const chips = (meta.groups || []).map(g =>
      `<label class="osint-chip"><input type="checkbox" data-osint-cmd="${cmd}" data-osint-group="${g}" ${cg.includes(g) ? 'checked' : ''}> ${g}</label>`
    ).join('');
    return `<tr>
      <td class="text-mono" style="font-weight:700;">/${cmd}</td>
      <td><label class="toggle-switch"><input type="checkbox" data-osint-enabled="${cmd}" ${en ? 'checked' : ''}><span class="toggle-slider"></span></label></td>
      <td><select data-osint-mode="${cmd}">
        <option value="" ${!cm ? 'selected' : ''}>Inherit (${s.default_mode})</option>
        <option value="single" ${cm === 'single' ? 'selected' : ''}>Single</option>
        <option value="race" ${cm === 'race' ? 'selected' : ''}>Race</option>
      </select></td>
      <td>${chips || '<span class="text-muted text-xs">no groups</span>'}</td>
      <td><input type="number" min="5" step="1" placeholder="inherit" style="width:90px;background:rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.1);border-radius:.5rem;padding:.4rem .6rem;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.78rem;" data-osint-timeout="${cmd}" value="${ct == null ? '' : ct}"></td>
    </tr>`;
  }).join('') || '<tr><td colspan="5" class="empty-state">No commands allowlisted</td></tr>';
}

async function saveOsintSettings() {
  try {
    const tVal = document.getElementById('osintTimeout').value;
    const body = {
      default_mode: document.getElementById('osintDefaultMode').value,
      timeout_sec: tVal === '' ? null : parseFloat(tVal),
      command_modes: {}, command_groups: {}, group_responders: {}, command_timeouts: {}, disabled_commands: []
    };
    document.querySelectorAll('[data-osint-responder]').forEach(el => {
      if (el.value !== '') body.group_responders[el.dataset.osintResponder] = parseInt(el.value);
    });
    document.querySelectorAll('[data-osint-mode]').forEach(el => {
      if (el.value) body.command_modes[el.dataset.osintMode] = el.value;
    });
    document.querySelectorAll('[data-osint-timeout]').forEach(el => {
      if (el.value !== '') body.command_timeouts[el.dataset.osintTimeout] = parseFloat(el.value);
    });
    document.querySelectorAll('[data-osint-enabled]').forEach(el => {
      if (!el.checked) body.disabled_commands.push(el.dataset.osintEnabled);
    });
    const grouped = {};
    document.querySelectorAll('[data-osint-group]').forEach(el => {
      if (el.checked) (grouped[el.dataset.osintCmd] = grouped[el.dataset.osintCmd] || []).push(parseInt(el.dataset.osintGroup));
    });
    body.command_groups = grouped;
    const d = await apiCall('POST', '/admin/api/osint/settings', body);
    if (!d.success) { toast(d.error || 'Save failed', 'error'); return; }
    renderOsintSettings(d.settings);
    toast('OSINT routing saved', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function resetOsintSettings() {
  try {
    const d = await apiCall('POST', '/admin/api/osint/settings/reset');
    if (!d.success) { toast('Reset failed', 'error'); return; }
    renderOsintSettings(d.settings);
    toast('OSINT routing reset to defaults', 'success');
  } catch(e) { toast(e.message, 'error'); }
}
</script>
</body>
</html>"""


# ==================== ADMIN API ROUTES ====================
@app.route('/admin')
@app.route('/admin/')
def admin_index():
    return render_template_string(ADMIN_HTML)

@app.route('/admin/login')
def admin_login_page():
    return render_template_string(ADMIN_HTML)

@app.route('/admin/api/login', methods=['POST'])
def admin_api_login():
    d = request.get_json(silent=True) or {}
    if d.get('password') != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid password"}), 401
    token = secrets.token_hex(32)
    admin_tokens.add(token)
    threading.Timer(86400, lambda: admin_tokens.discard(token)).start()
    return jsonify({"token": token, "message": "Login successful"})

@app.route('/admin/api/accounts', methods=['GET'])
@require_auth
def admin_list_accounts():
    with accounts_lock:
        result = [{
            "id":            a["id"],
            "name":          a["name"],
            "api_id":        a["api_id"],
            "status":        a["status"],
            "bypass_count":  a["bypass_count"],
            "success_count": a["success_count"],
            "fail_count":    a["fail_count"],
            "last_used":     a["last_used"],
            "error_message": a["error_message"],
            "username":      a["username"],
            "first_name":    a["first_name"],
        } for a in accounts.values()]
    return jsonify(result)

@app.route('/admin/api/accounts', methods=['POST'])
@require_auth
def admin_add_account():
    d = request.get_json(silent=True) or {}
    name    = d.get('name', '').strip()
    api_id  = d.get('api_id')
    api_hash= d.get('api_hash', '').strip()
    sess    = d.get('session_string', '').strip()
    if not (name and api_id and api_hash and sess):
        return jsonify({"error": "Missing required fields"}), 400
    acc_id = _add_account_record(name, api_id, api_hash, sess)
    save_accounts_to_file()
    return jsonify({"id": acc_id, "message": "Account added, connecting..."})

@app.route('/admin/api/accounts/<acc_id>', methods=['DELETE'])
@require_auth
def admin_delete_account(acc_id):
    _stop_account(acc_id)
    with accounts_lock:
        if acc_id not in accounts:
            return jsonify({"error": "Account not found"}), 404
        del accounts[acc_id]
    save_accounts_to_file()
    return jsonify({"success": True})

@app.route('/admin/api/accounts/<acc_id>/pause', methods=['POST'])
@require_auth
def admin_pause_account(acc_id):
    with accounts_lock:
        if acc_id not in accounts:
            return jsonify({"error": "Account not found"}), 404
        accounts[acc_id]['status'] = 'paused'
    return jsonify({"success": True})

@app.route('/admin/api/accounts/<acc_id>/resume', methods=['POST'])
@require_auth
def admin_resume_account(acc_id):
    with accounts_lock:
        if acc_id not in accounts:
            return jsonify({"error": "Account not found"}), 404
        acc = accounts[acc_id]
        if acc['status'] == 'paused':
            if acc.get('client') and acc.get('loop'):
                acc['status'] = 'active'
            else:
                acc['status'] = 'connecting'
                threading.Thread(target=_thread_target, args=(acc_id,), daemon=True).start()
        elif acc['status'] == 'error':
            acc['status'] = 'connecting'
            acc['error_message'] = None
            threading.Thread(target=_thread_target, args=(acc_id,), daemon=True).start()
    return jsonify({"success": True})

@app.route('/admin/api/accounts/<acc_id>/session', methods=['PUT'])
@require_auth
def admin_update_session(acc_id):
    d = request.get_json(silent=True) or {}
    new_sess = d.get('session_string', '').strip()
    if not new_sess:
        return jsonify({"error": "session_string required"}), 400
    with accounts_lock:
        if acc_id not in accounts:
            return jsonify({"error": "Account not found"}), 404
    _stop_account(acc_id)
    with accounts_lock:
        accounts[acc_id].update({
            'session_string': new_sess,
            'status': 'connecting',
            'error_message': None,
            'client': None,
            'loop': None,
            'dzhq_bot_id': None,
            'nick_bot_id': None,
            'alex_bot_id': None,
            'pending': {},
        })
    _start_account_thread(acc_id)
    save_accounts_to_file()
    return jsonify({"success": True, "message": "Session updated, reconnecting..."})

@app.route('/admin/api/pause-all', methods=['POST'])
@require_auth
def admin_pause_all():
    with accounts_lock:
        for a in accounts.values():
            a['status'] = 'paused'
    return jsonify({"success": True})

@app.route('/admin/api/resume-all', methods=['POST'])
@require_auth
def admin_resume_all():
    with accounts_lock:
        for acc_id, a in accounts.items():
            if a['status'] == 'paused':
                if a.get('client') and a.get('loop'):
                    a['status'] = 'active'
                else:
                    a['status'] = 'connecting'
    with accounts_lock:
        to_restart = [acc_id for acc_id, a in accounts.items() if a['status'] == 'connecting']
    for acc_id in to_restart:
        threading.Thread(target=_thread_target, args=(acc_id,), daemon=True).start()
    return jsonify({"success": True})

# ── Bot toggle endpoints ───────────────────────────────────────────────────────
def _bots_state(extra=None):
    st = {
        "dzhq":        bot_settings["dzhq"],
        "nick":        bot_settings["nick"],
        "dzhq_first":  bot_settings.get("dzhq_first", False),
        "nick_first":  bot_settings.get("nick_first", False),
        "random_mode": bot_settings.get("random_mode", False),
        "alex_bot":    bot_settings.get("alex_bot", True),
        "alex_route":  bot_settings.get("alex_route", "race"),
        "random":      _random_status(),
    }
    if extra:
        st.update(extra)
    return st

@app.route('/admin/api/bots/status', methods=['GET'])
@require_auth
def admin_bots_status():
    return jsonify(_bots_state())

@app.route('/admin/api/bots/toggle', methods=['POST'])
@require_auth
def admin_bots_toggle():
    d = request.get_json(silent=True) or {}
    bot  = d.get("bot")
    enab = bool(d.get("enabled", True))
    if bot not in ("dzhq", "nick", "dzhq_first", "nick_first", "random_mode", "alex_bot"):
        return jsonify({"success": False, "error": "Invalid bot name"}), 400
    # Alex bot toggle — independent of the DZHQ/Nick race
    if bot == "alex_bot":
        bot_settings["alex_bot"] = enab
        return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))
    # Mode toggles — only one mode can be active at a time
    if bot in ("dzhq_first", "nick_first", "random_mode"):
        _set_exclusive_mode(bot if enab else None)
        if bot == "random_mode":
            with _random_lock:
                _random_state["bot"] = None
                _random_state["left"] = 0
                _random_state["batch"] = 0
        return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))
    # At least one of dzhq/nick must remain enabled
    other = "nick" if bot == "dzhq" else "dzhq"
    if not enab and not bot_settings[other]:
        return jsonify({"success": False,
                        "error": "Cannot disable both bots — keep at least one enabled"}), 400
    bot_settings[bot] = enab
    return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))

@app.route('/admin/api/bots/alex-route', methods=['POST'])
@require_auth
def admin_bots_alex_route():
    """Alex-only route toggle: race (API+bot) | bot (@alexbypassbot only) | api (HTTP API only)."""
    d = request.get_json(silent=True) or {}
    route = str(d.get("route", "")).strip().lower()
    if route not in ("race", "bot", "api"):
        return jsonify({"success": False, "error": "Invalid route — use race, bot, or api"}), 400
    bot_settings["alex_route"] = route
    return jsonify(_bots_state({"success": True, "route": route}))

@app.route('/admin/api/bypass/priorities', methods=['GET'])
@require_auth
def admin_bypass_priorities():
    return jsonify(_bypass_priorities_snapshot())

@app.route('/admin/api/bypass/priorities', methods=['POST'])
@require_auth
def admin_bypass_priorities_save():
    d = request.get_json(silent=True) or {}
    ok, err = _validate_bypass_priorities(d)
    if not ok:
        return jsonify({"success": False, "error": err}), 400
    with _bypass_settings_lock:
        bypass_settings["priorities"] = d["priorities"]
    _save_bypass_settings()
    return jsonify(_bypass_priorities_snapshot())

@app.route('/admin/api/bypass/priorities/reset', methods=['POST'])
@require_auth
def admin_bypass_priorities_reset():
    with _bypass_settings_lock:
        bypass_settings["priorities"] = _parse_bypass_priorities(os.environ.get("BYPASS_PRIORITIES", ""))
    _save_bypass_settings()
    return jsonify(_bypass_priorities_snapshot())


# ==================== ADMIN OSINT ROUTING API — ADDITIVE ONLY ====================
@app.route('/admin/api/osint/settings', methods=['GET'])
@require_auth
def admin_osint_settings():
    return jsonify({"success": True, "settings": _osint_settings_snapshot()})

@app.route('/admin/api/osint/settings', methods=['POST'])
@require_auth
def admin_osint_save():
    d = request.get_json(silent=True) or {}
    ok, err = _validate_osint_settings(d, partial=True)
    if not ok:
        return jsonify({"success": False, "error": err}), 400
    with _osint_settings_lock:
        if "default_mode" in d:
            osint_settings["default_mode"] = d["default_mode"]
        if "command_modes" in d:
            osint_settings["command_modes"] = {str(k).strip().lower().lstrip("/"): v for k, v in d["command_modes"].items()}
        if "command_groups" in d:
            osint_settings["command_groups"] = {str(k).strip().lower().lstrip("/"): [int(g) for g in v] for k, v in d["command_groups"].items()}
        if "group_responders" in d:
            osint_settings["group_responders"] = {int(k): int(v) for k, v in d["group_responders"].items()}
        if "disabled_commands" in d:
            osint_settings["disabled_commands"] = list(d["disabled_commands"])
        if "timeout_sec" in d:
            osint_settings["timeout_sec"] = d["timeout_sec"]
        if "command_timeouts" in d:
            osint_settings["command_timeouts"] = {str(k).strip().lower().lstrip("/"): v for k, v in d["command_timeouts"].items()}
    _save_osint_settings()
    return jsonify({"success": True, "settings": _osint_settings_snapshot()})

@app.route('/admin/api/osint/settings/reset', methods=['POST'])
@require_auth
def admin_osint_reset():
    with _osint_settings_lock:
        osint_settings["default_mode"] = OSINT_DEFAULT_MODE
        osint_settings["command_modes"] = {}
        osint_settings["command_groups"] = {}
        for cmd, val in OSINT_COMMAND_GROUPS.items():
            g = _resolve_osint_group(val)
            if g is not None:
                osint_settings["command_groups"][cmd] = [g]
        osint_settings["group_responders"] = dict(OSINT_GROUP_RESPONDERS)
        osint_settings["disabled_commands"] = []
        osint_settings["timeout_sec"] = None
        osint_settings["command_timeouts"] = {}
    _save_osint_settings()
    return jsonify({"success": True, "settings": _osint_settings_snapshot()})


# ==================== PUBLIC HOME / STATUS ====================
def _public_status():
    with accounts_lock:
        accs = [{
            "name":       a["name"],
            "username":   ("@" + a["username"]) if a.get("username") else None,
            "first_name": a.get("first_name"),
            "status":     a["status"],
            "online":     a["status"] == "active",
            "bypasses":   a["bypass_count"],
            "success":    a["success_count"],
            "failed":     a["fail_count"],
            "last_used":  a["last_used"],
        } for a in accounts.values()]
    online = [a for a in accs if a["online"]]
    return {
        "status":         True,
        "developer":      DEVELOPER,
        "api_online":     bool(ALEX_BYPASS_API) or len(online) > 0,
        "total_accounts": len(accs),
        "online_accounts": len(online),
        "alex_api":       bool(ALEX_BYPASS_API),
        "bots":           {"dzhq": bot_settings.get("dzhq", True),
                           "nick": bot_settings.get("nick", True),
                           "dzhq_first": bot_settings.get("dzhq_first", False),
                           "nick_first": bot_settings.get("nick_first", False),
                            "random_mode": bot_settings.get("random_mode", False),
                            "alex_bot": bot_settings.get("alex_bot", True),
                            "alex_route": bot_settings.get("alex_route", "race")},
        "accounts":       accs,
    }


@app.route('/status', methods=['GET'])
def public_status():
    return jsonify(_public_status())


HOME_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Felix Bypass API — Status</title>
<style>
:root{--bg:#0b0f17;--card:#131a26;--line:#1f2a3a;--txt:#e6edf7;--mut:#8b9bb4;--grn:#22c55e;--red:#ef4444;--amb:#f59e0b;--cyn:#22d3ee}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:24px 16px}
.wrap{max-width:820px;margin:0 auto}h1{font-size:1.45rem;margin:0 0 4px}.mut{color:var(--mut);font-size:.85rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.big{font-size:1.6rem;font-weight:700}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.on{background:var(--grn);box-shadow:0 0 8px var(--grn)}.off{background:var(--red)}.pau{background:var(--amb)}
.acc{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.acc:last-child{border-bottom:0}.tag{font-size:.72rem;color:var(--mut);font-family:ui-monospace,monospace}
code{background:#0f1621;border:1px solid var(--line);padding:2px 6px;border-radius:6px;color:var(--cyn)}
</style></head><body><div class="wrap">
<h1>⚡ Felix Bypass API</h1><div class="mut" id="sub">Loading status…</div>
<div class="grid">
 <div class="card"><div class="mut">API</div><div class="big" id="api">—</div></div>
 <div class="card"><div class="mut">Online accounts</div><div class="big" id="on">—</div></div>
 <div class="card"><div class="mut">Total accounts</div><div class="big" id="tot">—</div></div>
 <div class="card"><div class="mut">Alex resolver</div><div class="big" id="alex">—</div></div>
</div>
<div class="card" style="padding:0"><div id="list"></div></div>
<p class="mut" style="margin-top:18px">Usage: <code>/bypass?link=YOUR_LINK</code> · JSON: <code>/status</code> · Panel: <code>/admin</code></p>
</div><script>
const S=s=>document.getElementById(s);
async function load(){
 try{const d=await (await fetch('/status')).json();
  S('sub').textContent='Developer '+d.developer+' · updated '+new Date().toLocaleTimeString();
  S('api').innerHTML='<span class="dot '+(d.api_online?'on':'off')+'"></span>'+(d.api_online?'Online':'Offline');
   S('on').textContent=d.online_accounts;S('tot').textContent=d.total_accounts;
   S('alex').textContent=d.alex_api?'Ready':'Off';
  S('list').innerHTML=d.accounts.length?d.accounts.map(a=>{
   const c=a.status==='active'?'on':(a.status==='paused'||a.status==='connecting'?'pau':'off');
   return '<div class="acc"><div><b><span class="dot '+c+'"></span>'+a.name+'</b>'+
    '<div class="tag">'+[a.first_name,a.username].filter(Boolean).join(' · ')+'</div></div>'+
    '<div class="tag">'+a.status.toUpperCase()+' · ✅'+a.success+' ❌'+a.failed+'</div></div>';
  }).join(''):'<div class="acc"><span class="mut">No accounts connected yet.</span></div>';
 }catch(e){S('sub').textContent='Status unavailable';}
}
load();setInterval(load,5000);
</script></body></html>"""


@app.route('/', methods=['GET'])
def home():
    return render_template_string(HOME_HTML)


# ==================== BYPASS ROUTE ====================

def _single_bypass_payload(original, bypassed, source, response_ms, account, module):
    """Return the shared response shape for exactly one bypassed URL."""
    return {
        "status":      True,
        "developer":   DEVELOPER,
        "response_ms": response_ms,
        "source":      source,
        "module":      module,
        "account":     account,
        "url":         bypassed,
        "links":       {
            "original": original,
            "bypassed": bypassed,
        },
    }


def _run_async_bypass(job_id, link):
    """Run the existing resolver in its own request context and save its reply."""
    try:
        # The normal resolver remains the single source of truth.  The header
        # lets it publish the same flow updates while running in the worker.
        with app.test_request_context(
            "/bypass",
            method="GET",
            headers={"X-Bypass-Job-Id": job_id},
        ):
            response = bypass(link_override=link)
            response_obj = app.make_response(response)
            payload = response_obj.get_json(silent=True)
            code = response_obj.status_code

        if isinstance(payload, dict):
            final_status = "success" if payload.get("status") is True else "failed"
        else:
            final_status = "failed"
            payload = {"status": False, "message": "Resolver returned an invalid response"}
        _flow_update(
            job_id,
            final_status,
            payload.get("message") or (
                "Bypass successful" if final_status == "success"
                else "Bypass failed"
            ),
            result=payload,
            http_status=code,
        )
    except Exception as exc:
        logging.exception("Async bypass job failed")
        _flow_update(job_id, "failed", f"Bypass flow crashed: {exc}")


@app.route('/bypass/async', methods=['GET', 'POST'])
def bypass_async():
    """Start a non-blocking bypass and return a job id for status polling."""
    if request.method == 'GET':
        link = (request.args.get('link') or request.args.get('url') or '').strip()
    else:
        data = request.get_json(silent=True) or {}
        link = (data.get('link') or data.get('url') or '').strip()
    if not link:
        return jsonify({
            "status": False,
            "developer": DEVELOPER,
            "message": "Missing 'link' parameter",
        }), 400
    if not link.startswith(("http://", "https://")):
        link = "https://" + link

    job_id = secrets.token_urlsafe(12)
    with _jobs_lock:
        if len(_jobs) >= _MAX_JOBS:
            # Remove oldest finished jobs first; never interrupt active work.
            finished = [
                (key, value.get("updated_at", 0))
                for key, value in _jobs.items()
                if value.get("status") in ("success", "failed")
            ]
            for key, _ in sorted(finished, key=lambda item: item[1])[:100]:
                _jobs.pop(key, None)
        _jobs[job_id] = {
            "status": "queued",
            "message": "Bypass queued",
            "link": link,
            "created_at": time.time(),
            "updated_at": time.time(),
            "flow": [{
                "status": "queued",
                "message": "Bypass queued",
                "at": time.time(),
            }],
        }

    threading.Thread(
        target=_run_async_bypass,
        args=(job_id, link),
        daemon=True,
        name=f"bypass-{job_id}",
    ).start()
    return jsonify({
        "status": "processing",
        "developer": DEVELOPER,
        "job_id": job_id,
        "message": "Bypass started; poll /bypass/status/<job_id> for flow updates",
        "status_url": f"/bypass/status/{job_id}",
    }), 202


@app.route('/bypass/status/<job_id>', methods=['GET'])
def bypass_status(job_id):
    """Return all status messages received so far for an async request."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({
                "status": False,
                "developer": DEVELOPER,
                "message": "Unknown or expired job id",
            }), 404
        snapshot = dict(job)
        snapshot["flow"] = list(job["flow"])
    snapshot["developer"] = DEVELOPER
    return jsonify(snapshot)


@app.route('/bypass', methods=['GET', 'POST'])
def bypass(link_override=None):
    if request.method == 'GET':
        link = (request.args.get('link') or request.args.get('url') or '').strip()
    else:
        d    = request.get_json(silent=True) or {}
        link = (d.get('link') or d.get('url') or '').strip()
    if link_override is not None:
        link = link_override.strip()

    if not link:
        return jsonify({"status": False, "developer": DEVELOPER, "message": "Missing 'link' parameter"}), 400
    if not link.startswith(('http://', 'https://')):
        link = 'https://' + link
    job_id = request.headers.get("X-Bypass-Job-Id")
    _flow_update(job_id, "processing", "Link received; starting bypass flow", link=link)
    _trace("REQUEST", f"start {link}")

    # ── Alex API only for URLKing / MonteOlympus links ───────────────
    # If Alex is offline or returns an error, continue with the original
    # Telegram race instead of failing the request.
    t0 = time.time()

    # ── Priority rules: pattern match → fixed resolver (STRICT) ─────────
    # A matched rule owns the whole request: alex_* targets run here and
    # return; dzhq/nick targets restrict the Telegram flow below. No silent
    # fallback to other resolvers — failures name the rule.
    prio_rule = _match_bypass_priority(link)
    prio_target = prio_rule["target"] if prio_rule else None
    if prio_rule:
        _flow_update(job_id, "processing",
                     f"Priority rule matched: {prio_rule['pattern']} → {prio_target}")
        _trace("PRIO", f"matched pattern={prio_rule['pattern']!r} target={prio_target} link={link}")
    if prio_target in ("alex_api", "alex_bot", "alex_race"):
        try:
            prio_winner, prio_error = _run_bypass_priority(prio_rule, link)
        except Exception as exc:
            logging.exception("Priority resolver crashed")
            prio_winner, prio_error = None, f"resolver crashed: {exc}"
        if prio_winner:
            result_url = prio_winner["url"]
            extra = prio_winner.get("extra", {})
            _flow_update(job_id, "success", "Bypass successful",
                         source=prio_winner["source"], url=result_url)
            return jsonify(_single_bypass_payload(
                original=link,
                bypassed=result_url,
                source=prio_winner["source"],
                response_ms=f"{int((time.time() - t0) * 1000)}ms",
                account=extra.get("account", prio_winner["source"]),
                module=extra.get("module", prio_winner["source"]),
            ))
        # Masked edge: users get ONLY the short aliased message. The full
        # resolver dump stays in server logs for the owner — never in the
        # API response (message, flow, or result).
        public_msg = _priority_public_message(prio_rule)
        print(f"[Prio] rule {prio_rule['pattern']!r} → {prio_target} failed: {prio_error}", flush=True)
        _trace("PRIO", f"rule failed pattern={prio_rule['pattern']!r} target={prio_target} error={str(prio_error)[:300]}")
        _flow_update(job_id, "failed", public_msg)
        return jsonify({
            "status":    False,
            "developer": DEVELOPER,
            "message":   public_msg,
        }), 502

    if prio_target not in ("dzhq", "nick") and _should_use_alex(link):
        _flow_update(job_id, "processing", "Checking Alex resolver")
        alex_winner, alex_error = alex_race(link)
        if alex_winner:
            result_url = alex_winner["url"]
            extra = alex_winner.get("extra", {})
            _flow_update(job_id, "success", "Bypass successful", source=alex_winner["source"], url=result_url)
            return jsonify(_single_bypass_payload(
                original=link,
                bypassed=result_url,
                source=alex_winner["source"],
                response_ms=f"{int((time.time() - t0) * 1000)}ms",
                account=extra.get("account", "alex_api"),
                module=extra.get("module", "alex_bot"),
            ))
        if alex_error:
            _flow_update(job_id, "processing", f"Alex resolver did not complete: {alex_error}; trying Telegram")
            print(f"[Alex] {alex_error} — trying Telegram fallback")

    # ── Account selection ─────────────────────────────────────────────
    acc_id, acc_state = get_next_active()
    if not acc_state:
        _flow_update(job_id, "failed", "No active Telegram account is available")
        return jsonify({
            "status":    False,
            "developer": DEVELOPER,
            "message":   "No active Telegram accounts. Add via /admin.",
        }), 503

    req_id = secrets.token_hex(8)
    loop   = acc_state.get("loop")

    # ── Respect bot toggles ───────────────────────────────────────────
    use_dzhq    = bot_settings.get("dzhq", True)
    use_nick    = bot_settings.get("nick", True)

    # ── Priority restriction: an explicit rule beats the global toggles ─
    if prio_target == "dzhq":
        use_dzhq, use_nick = True, False
        _trace("PRIO", f"restricted to DZHQ only for {link}")
    elif prio_target == "nick":
        use_dzhq, use_nick = False, True
        _trace("PRIO", f"restricted to Nick only for {link}")

    # ── Mode resolution: primary bot runs first, other one is fallback ──
    primary = None
    if use_dzhq and use_nick:
        if bot_settings.get("random_mode", False):
            primary = _random_pick_bot()
        elif bot_settings.get("dzhq_first", False):
            primary = "dzhq"
        elif bot_settings.get("nick_first", False):
            primary = "nick"
    seq_mode   = primary is not None
    dzhq_first = primary == "dzhq"
    nick_first = primary == "nick"

    if not use_dzhq and not use_nick:
        _flow_update(job_id, "failed", "All bypass bots are disabled")
        return jsonify({
            "status":    False,
            "developer": DEVELOPER,
            "message":   "All bots are disabled — enable at least one in /admin",
        }), 503

    req_entry = {
        "link":         link,
        "ts":           t0,
        "done":         False,
        # DZHQ group
        "dzhq_event":   threading.Event(),
        "dzhq_result":  None,
        "dzhq_sent_id": None,
        "last_dzhq_ts": None,
        # Nick DM
        "nick_event":   threading.Event(),
        "nick_result":  None,
        "last_nick_ts": None,
    }
    with accounts_lock:
        accounts[acc_id]["pending"][req_id] = req_entry
        # Register in Nick DM queue (FIFO) only if Nick is enabled
        # In DZHQ First mode, Nick queue registration is deferred until DZHQ fails
        if use_nick and primary != "dzhq":
            nick_lock = accounts[acc_id].get("nick_dm_lock")
            if nick_lock:
                with nick_lock:
                    accounts[acc_id]["nick_dm_queue"].append(req_id)

    TIMEOUT      = BYPASS_IDLE_TIMEOUT_SEC
    DZHQ_TIMEOUT = 20.0   # How long to wait for DZHQ before falling back to Nick

    # ── Helper: send to Nick (used in both modes) ─────────────────────
    async def _send_nick(tg):
        try:
            await tg.send_message(NICK_BOT, link)
            req_entry["last_nick_ts"] = time.time()
            _trace("NICK", f"sent {link}")
        except Exception as e:
            req_entry['nick_fail'] = f"Nick DM send error: {e}"
            _trace("NICK", f"send failed for {link}: {e}")
            with accounts_lock:
                nlk = accounts[acc_id].get("nick_dm_lock")
                if nlk:
                    with nlk:
                        q = accounts[acc_id].get("nick_dm_queue", [])
                        if req_id in q:
                            q.remove(req_id)

    # ── Send to DZHQ ──────────────────────────────────────────────────
    async def _send_dzhq(tg):
        try:
            s1 = await tg.send_message(DZHQ_GROUP, f"/b {link}")
            req_entry['dzhq_sent_id'] = s1.id
            req_entry["last_dzhq_ts"] = time.time()
            _trace("DZHQ", f"sent /b for {link}, message_id={s1.id}")
        except Exception as e:
            req_entry['dzhq_fail'] = f"DZHQ send error: {e}"
            _trace("DZHQ", f"send failed for {link}: {e}")

    # ── MODE A: Sequential (DZHQ First / Nick First / Random) ─────────
    if seq_mode:
        _flow_update(job_id, "processing", f"Sent link to {primary.upper()}; waiting for its reply")
        async def _send_primary():
            if primary == "dzhq":
                await _send_dzhq(acc_state["client"])
            else:
                await _send_nick(acc_state["client"])
        try:
            asyncio.run_coroutine_threadsafe(_send_primary(), loop).result(timeout=12)
        except Exception as e:
            with accounts_lock:
                accounts[acc_id]["pending"].pop(req_id, None)
            return jsonify({
                "status":    False,
                "developer": DEVELOPER,
                "message":   f"Failed to send to {primary}: {e}",
            }), 500

    # ── MODE B: Parallel — send to both simultaneously ────────────────
    else:
        _flow_update(job_id, "processing", "Sent link to enabled bypass bots; waiting for the first reply")
        async def _send_all():
            tg = acc_state["client"]
            sends = []
            if use_dzhq:
                sends.append(_send_dzhq(tg))
            if use_nick:
                sends.append(_send_nick(tg))
            if sends:
                await asyncio.gather(*sends)
        try:
            asyncio.run_coroutine_threadsafe(_send_all(), loop).result(timeout=12)
        except Exception as e:
            with accounts_lock:
                accounts[acc_id]["pending"].pop(req_id, None)
            return jsonify({
                "status":    False,
                "developer": DEVELOPER,
                "message":   f"Failed to send: {e}",
            }), 500

    # ── Race / watcher setup ──────────────────────────────────────────
    active_bots = sum([use_dzhq, use_nick])

    race_event  = threading.Event()
    race_winner = {}
    race_lock   = threading.Lock()
    done_count  = [0]
    done_lock   = threading.Lock()
    # DZHQ First uses one watcher for both sequential stages. Counting both
    # bots here would leave the request waiting forever after both fail.
    watcher_goal = 1 if seq_mode else active_bots

    def _declare_winner(source, data):
        with race_lock:
            if race_event.is_set():
                return False
            race_winner['source'] = source
            race_winner['data']   = data
            req_entry['race_done'] = True   # signal Nick handler: race already won
            race_event.set()
            _trace(source.upper(), f"winner for {link}: {data}")
            return True

    def _watcher_done():
        with done_lock:
            done_count[0] += 1
            if done_count[0] >= watcher_goal:
                race_event.set()   # all active bots timed out / failed

    request_budget = (
        TIMEOUT * (2 if seq_mode else 1)
        if TIMEOUT > 0
        else MAX_BYPASS_TIMEOUT_SEC
    )
    request_budget = min(MAX_BYPASS_TIMEOUT_SEC, max(1.0, request_budget))
    request_deadline = time.monotonic() + request_budget

    def _remaining(last_seen):
        hard_remaining = request_deadline - time.monotonic()
        if TIMEOUT <= 0:
            return hard_remaining
        idle_remaining = last_seen + TIMEOUT - time.time()
        return min(idle_remaining, hard_remaining)

    def _watch_dzhq():
        ev = req_entry['dzhq_event']
        while not race_event.is_set():
            last_seen = req_entry.get("last_dzhq_ts") or t0
            remaining = _remaining(last_seen)
            if remaining is not None and remaining <= 0: break
            if ev.wait(timeout=0.3 if remaining is None else min(0.3, remaining)):
                res = req_entry.get('dzhq_result')
                if res and res[0].get('status') == 'ok':
                    _declare_winner("dzhq", res)
                break

        # ── DZHQ First fallback: DZHQ failed/timed out → now send Nick ─
        if dzhq_first and not race_event.is_set():
            print(f"[Bypass] DZHQ First: DZHQ failed/timeout — sending to Nick now")
            # Register Nick in queue
            with accounts_lock:
                nlk = accounts[acc_id].get("nick_dm_lock")
                if nlk:
                    with nlk:
                        accounts[acc_id]["nick_dm_queue"].append(req_id)
            # Send to Nick
            async def _fallback_nick():
                await _send_nick(acc_state["client"])
            try:
                asyncio.run_coroutine_threadsafe(_fallback_nick(), loop).result(timeout=10)
            except Exception as e:
                print(f"[Bypass] DZHQ First fallback Nick send error: {e}")
            # Now watch Nick with remaining time
            nick_ev   = req_entry['nick_event']
            while not race_event.is_set():
                last_seen = req_entry.get("last_nick_ts") or time.time()
                rem = _remaining(last_seen)
                if rem is not None and rem <= 0: break
                if nick_ev.wait(timeout=0.3 if rem is None else min(0.3, rem)):
                    r = req_entry.get('nick_result')
                    if r and r.get('status') == 'ok':
                        _declare_winner("nick", r)
                    break

        _watcher_done()

    def _watch_nick():
        ev = req_entry['nick_event']
        while not race_event.is_set():
            last_seen = req_entry.get("last_nick_ts") or t0
            remaining = _remaining(last_seen)
            if remaining is not None and remaining <= 0: break
            if ev.wait(timeout=0.3 if remaining is None else min(0.3, remaining)):
                r = req_entry.get('nick_result')
                if r and r.get('status') == 'ok':
                    _declare_winner("nick", r)
                break

        # ── Nick First fallback: Nick failed/timed out → now send DZHQ ─
        if nick_first and not race_event.is_set():
            print("[Bypass] Nick First: Nick failed/timeout — sending to DZHQ now")
            async def _fallback_dzhq():
                await _send_dzhq(acc_state["client"])
            try:
                asyncio.run_coroutine_threadsafe(_fallback_dzhq(), loop).result(timeout=10)
            except Exception as e:
                print(f"[Bypass] Nick First fallback DZHQ send error: {e}")
            dz_ev = req_entry['dzhq_event']
            while not race_event.is_set():
                last_seen = req_entry.get("last_dzhq_ts") or time.time()
                rem = _remaining(last_seen)
                if rem is not None and rem <= 0: break
                if dz_ev.wait(timeout=0.3 if rem is None else min(0.3, rem)):
                    res = req_entry.get('dzhq_result')
                    if res and res[0].get('status') == 'ok':
                        _declare_winner("dzhq", res)
                    break

        _watcher_done()

    threads = []
    # In sequential modes only the primary watcher runs; it handles the
    # fallback bot itself. In parallel mode both watchers run together.
    if (use_dzhq and not seq_mode) or dzhq_first:
        t1 = threading.Thread(target=_watch_dzhq, daemon=True)
        t1.start(); threads.append(t1)
    if (use_nick and not seq_mode) or nick_first:
        t3 = threading.Thread(target=_watch_nick, daemon=True)
        t3.start(); threads.append(t3)
    # Even if a watcher is unexpectedly lost, never hold the HTTP request open
    # forever.
    race_event.wait(timeout=max(0.0, request_deadline - time.monotonic()))

    # Cleanup
    async def _cleanup():
        tg = acc_state["client"]
        for gid, key in ((DZHQ_GROUP, 'dzhq_sent_id'),):
            mid = req_entry.get(key)
            if mid:
                try:
                    await tg.delete_messages(gid, [mid])
                except:
                    pass
    try:
        asyncio.run_coroutine_threadsafe(_cleanup(), loop)
    except:
        pass

    # Remove from nick queue if still there
    with accounts_lock:
        accounts[acc_id]["pending"].pop(req_id, None)
        nlk = accounts[acc_id].get("nick_dm_lock")
        if nlk:
            with nlk:
                q = accounts[acc_id].get("nick_dm_queue", [])
                if req_id in q:
                    q.remove(req_id)

    ms  = int((time.time() - t0) * 1000)
    won = race_winner.get('source') is not None

    with accounts_lock:
        if acc_id in accounts:
            accounts[acc_id]['bypass_count'] += 1
            accounts[acc_id]['last_used'] = datetime.now(timezone.utc).isoformat()
            if won:
                accounts[acc_id]['success_count'] += 1
            else:
                accounts[acc_id]['fail_count'] += 1

    if not won:
        _flow_update(job_id, "failed", "All bypass bots failed or timed out", response_ms=f"{ms}ms")
        _trace("RESULT", f"failed for {link}: dzhq={req_entry.get('dzhq_fail', 'no response')} nick={req_entry.get('nick_fail', 'no response')}")
        return jsonify({
            "status":      False,
            "developer":   DEVELOPER,
            "message":     "All bots failed to bypass",
            "dzhq":        req_entry.get('dzhq_fail', 'no response'),
            "nick":        req_entry.get('nick_fail',  'no response'),
            "response_ms": f"{ms}ms",
        }), 422

    source = race_winner['source']
    data   = race_winner['data']
    acct   = acc_state.get("name", "unknown")
    _flow_update(job_id, "success", "Bypass successful", source=source)
    _trace("RESULT", f"success for {link}: source={source} data={data}")

    # Build unified response
    if source == "nick":
        if isinstance(data.get("bypassed"), str) and data.get("bypassed"):
            return jsonify(_single_bypass_payload(
                original=data.get("original") or link,
                bypassed=data["bypassed"],
                source="nick",
                response_ms=f"{ms}ms",
                account=acct,
                module="nick_bot",
            ))

        links = {}
        if data.get('original'): links["original"] = data['original']
        if data.get('bypassed'): links["bypassed"]  = data['bypassed']
        return jsonify({
            "status":      True,
            "developer":   DEVELOPER,
            "response_ms": f"{ms}ms",
            "source":      source,
            "account":     acct,
            "links":       links,
        })

    # source == "dzhq"
    r     = data[0]
    if (
        len(data) == 1
        and isinstance(r.get("bypassed"), str)
        and r.get("bypassed")
        and not any(r.get(key) for key in ("instant_dl", "telegram", "direct"))
    ):
        return jsonify(_single_bypass_payload(
            original=r.get("original") or link,
            bypassed=r["bypassed"],
            source="dzhq",
            response_ms=f"{ms}ms",
            account=acct,
            module="dzhq_bot",
        ))

    links = {}
    if r.get('original'):    links["original"]   = r['original']
    if r.get('bypassed'):    links["bypassed"]   = r['bypassed']
    if r.get('instant_dl'):  links["instant_dl"] = r['instant_dl']
    if r.get('telegram'):    links["telegram"]   = r['telegram']
    if r.get('direct'):      links["direct"]     = r['direct']

    resp = {
        "status":      True,
        "developer":   DEVELOPER,
        "response_ms": f"{ms}ms",
        "source":      "dzhq",
        "account":     acct,
    }
    if r.get('file_name') or r.get('file_size'):
        resp["file"] = {}
        if r.get('file_name'): resp["file"]["name"] = r['file_name']
        if r.get('file_size'): resp["file"]["size"] = r['file_size']
    resp["links"] = links
    if len(data) > 1:
        resp["batch"] = data
    return jsonify(resp)





# ==================== OSINT ROUTE — ADDITIVE ONLY ====================
@app.route('/osint', methods=['GET', 'POST'])
def osint_bridge():
    """Forward an allowlisted OSINT command to the configured Telegram group(s).

    This route is intentionally separate from /bypass. It has its own account
    round-robin pointer, so OSINT requests do not alter bypass routing order.
    The endpoint requires an API key and does not persist OSINT responses.

    Params (GET query or POST JSON):
      command: e.g. "/num 9876543210" (required, except for service info).
      group:   full group ID ("-100...") or 1-based index into OSINT_GROUPS;
               comma-separated list allowed in race mode. Default: fixed
               per-command group from OSINT_COMMAND_GROUPS, else first group.
      mode:    "single" or "race" (send to all/selected groups, first
               reply wins). Optional override — routing defaults to the
               /admin panel config, so callers may pass only {"command"}.
    """
    if not OSINT_ENABLED:
        return jsonify({
            "status": False,
            "developer": DEVELOPER,
            "message": "OSINT service is disabled",
        }), 503

    if not OSINT_API_KEY:
        return jsonify({
            "status": False,
            "developer": DEVELOPER,
            "message": "OSINT_API_KEY is not configured",
        }), 503

    supplied_key = (request.headers.get("X-API-Key") or "").strip()
    if not supplied_key or not secrets.compare_digest(supplied_key, OSINT_API_KEY):
        return jsonify({
            "status": False,
            "developer": DEVELOPER,
            "message": "Unauthorized",
        }), 401

    if request.method == 'GET':
        command = (request.args.get('command') or '').strip()
        group_param = (request.args.get('group') or '').strip()
        mode_param = (request.args.get('mode') or '').strip().lower()
    else:
        data = request.get_json(silent=True) or {}
        command = (data.get('command') or '').strip()
        group_param = str(data.get('group') or '').strip()
        mode_param = str(data.get('mode') or '').strip().lower()

    # No command -> service info for frontends (groups / commands / routing).
    if not command:
        return jsonify({
            "status": True,
            "developer": DEVELOPER,
            "service": "osint",
            "groups": OSINT_GROUPS,
            "allowed_commands": sorted(OSINT_ALLOWED_COMMANDS),
            "routing": _osint_settings_snapshot(),
            "effective": {
                cmd: {"mode": m, "groups": g}
                for cmd, (g, m, _, _) in (
                    (c, _resolve_osint_routing(c)) for c in sorted(OSINT_ALLOWED_COMMANDS)
                )
            },
            "modes": ["single", "race"],
            "timeout_sec": OSINT_TIMEOUT_SEC,
        })

    command_name, command_value = _osint_command_parts(command)
    if command_name is None:
        return jsonify({
            "status": False,
            "developer": DEVELOPER,
            "message": command_value,
        }), 400

    # Routing is admin-managed (/admin panel); explicit request params only
    # override it when provided, so plain {"command": "..."} just works.
    target_groups, mode, route_err, route_status = _resolve_osint_routing(
        command_name, group_param, mode_param)
    if route_err:
        return jsonify({
            "status": False,
            "developer": DEVELOPER,
            "message": route_err,
            "mode": mode,
            "groups": OSINT_GROUPS,
        }), route_status

    acc_id, acc_state = get_next_osint_active()
    if not acc_state:
        return jsonify({
            "status": False,
            "developer": DEVELOPER,
            "message": "No active Telegram account is available for OSINT",
        }), 503

    # One in-flight OSINT query per (account, group) prevents FIFO ambiguity.
    # Locks are collected under accounts_lock, then acquired without holding it.
    with accounts_lock:
        group_locks = [(g, _osint_group_lock(accounts[acc_id], g)) for g in sorted(set(target_groups))]

    held_locks = []
    for g, lk in group_locks:
        if not lk.acquire(timeout=1.0):
            for _, h in held_locks:
                h.release()
            return jsonify({
                "status": False,
                "developer": DEVELOPER,
                "message": f"Selected Telegram account is busy with another OSINT request (group {g})",
            }), 429
        held_locks.append((g, lk))

    t0 = time.time()
    req_id = secrets.token_hex(8)
    loop = acc_state.get("loop")
    target_arg = (command.split(None, 1)[1].strip() if len(command.split(None, 1)) == 2 else "")

    req = {
        "link": command,
        "ts": t0,
        "done": False,
        "osint_command_name": command_name,
        "osint_expected_target": target_arg,
        "osint_groups": [g for g, _ in group_locks],
        "osint_mode": mode,
        "osint_sent_id": None,
        "osint_sent_ids": {},
        "osint_winning_group": None,
        "last_osint_ts": None,
        "osint_event": threading.Event(),
        "osint_result": None,
    }

    try:
        with accounts_lock:
            accounts[acc_id]["pending"][req_id] = req
            for g, _ in group_locks:
                _osint_group_queue(accounts[acc_id], g).append(req_id)

        if not loop or not acc_state.get("client"):
            return jsonify({
                "status": False,
                "developer": DEVELOPER,
                "message": "Telegram account is not ready",
            }), 503

        async def _send_osint():
            for g, _ in group_locks:
                sent = await acc_state["client"].send_message(g, command)
                req["osint_sent_ids"][g] = getattr(sent, "id", None)
            ids = list(req["osint_sent_ids"].values())
            req["osint_sent_id"] = ids[0] if ids else None
            req["last_osint_ts"] = time.time()

        try:
            asyncio.run_coroutine_threadsafe(_send_osint(), loop).result(timeout=12)
        except Exception as exc:
            with accounts_lock:
                accounts[acc_id]["osint_fail_count"] += 1
                accounts[acc_id]["last_osint_used"] = datetime.now(timezone.utc).isoformat()
            return jsonify({
                "status": False,
                "developer": DEVELOPER,
                "message": f"Failed to send OSINT command: {exc}",
            }), 502

        event = req["osint_event"]
        if not event.wait(timeout=_osint_timeout_for(command_name)):
            with accounts_lock:
                accounts[acc_id]["osint_fail_count"] += 1
                accounts[acc_id]["last_osint_used"] = datetime.now(timezone.utc).isoformat()
            return jsonify({
                "status": False,
                "developer": DEVELOPER,
                "message": "OSINT responder timed out",
                "response_ms": f"{int((time.time() - t0) * 1000)}ms",
                "mode": mode,
                "groups": req.get("osint_groups"),
            }), 504

        result = req.get("osint_result") or {}
        ms = int((time.time() - t0) * 1000)
        with accounts_lock:
            accounts[acc_id]["osint_count"] += 1
            accounts[acc_id]["last_osint_used"] = datetime.now(timezone.utc).isoformat()
            if result.get("status") == "ok":
                accounts[acc_id]["osint_success_count"] += 1
            else:
                accounts[acc_id]["osint_fail_count"] += 1

        if result.get("status") != "ok":
            return jsonify({
                "status": False,
                "developer": DEVELOPER,
                "response_ms": f"{ms}ms",
                "source": "osint_group",
                "report": result.get("report"),
                "target": result.get("target"),
                "message": result.get("error") or "Could not parse OSINT response",
                "raw": result.get("raw", "")[:3000],
                "mode": mode,
                "group": req.get("osint_winning_group"),
            }), 502

        return jsonify({
            "status": True,
            "developer": DEVELOPER,
            "response_ms": f"{ms}ms",
            "source": "osint_group",
            "report": result.get("report"),
            "target": result.get("target"),
            "data": result.get("data"),
            "account": acc_state.get("name", acc_id),
            "mode": mode,
            "group": req.get("osint_winning_group"),
        })
    finally:
        req["done"] = True
        with accounts_lock:
            accounts.get(acc_id, {}).get("pending", {}).pop(req_id, None) if accounts.get(acc_id) else None
            queues = accounts.get(acc_id, {}).get("osint_group_queues", {}) if accounts.get(acc_id) else {}
            for g in req.get("osint_groups", []):
                q = queues.get(g, [])
                if req_id in q:
                    q.remove(req_id)
        for _, lk in held_locks:
            try:
                lk.release()
            except Exception:
                pass

# ==================== FLASK SERVER ====================
def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)


# ==================== STARTUP ====================
if __name__ == '__main__':
    try:
        load_accounts_from_file()
    except Exception as e:
        print(f"⚠️ Could not load saved accounts: {e}")

    # Do not auto-load a default Telegram session. The API and Admin Panel
    # must remain available even when there is no connected account; accounts
    # can be added or updated securely from /admin.
    if accounts:
        print(f"ℹ️ Saved accounts loaded: {len(accounts)}")
    else:
        print("ℹ️ No Telegram accounts configured. API/Admin Panel remain available; add one via /admin.")

    threading.Thread(target=run_flask, daemon=True).start()

    print(f"""
{'='*58}
  ✅ FELIX BYPASS API STARTED
  ⚡ Bypass:   http://localhost:{PORT}/bypass?link=URL
  📊 Admin:    http://localhost:{PORT}/admin
  🔑 Password: {ADMIN_PASSWORD}
  🤖 Bots:     DZHQ Group + Nick DM (2-way race)
  🅰️ Alex Bot: {'✅ ON — races Alex API' if bot_settings.get('alex_bot') else '❌ OFF'} ({ALEX_BOT})
  🔗 Alex API:  {'✅ Primary resolver enabled' if ALEX_BYPASS_API else '⚠️  Not configured'}
  💾 Accounts: {len(accounts)} total
{'='*58}
""")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
