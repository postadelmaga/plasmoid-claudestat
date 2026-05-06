#!/usr/bin/env python3
# Parses local Gemini CLI data (~/.gemini/) and outputs JSON for the plasmoid
# Mirrors the structure of local_stats.py for Claude

import json, os, glob, sys
from datetime import datetime, timedelta, timezone

gemini_dir = os.path.expanduser("~/.gemini")
tmp_dir = os.path.join(gemini_dir, "tmp")
projects_file = os.path.join(gemini_dir, "projects.json")
accounts_file = os.path.join(gemini_dir, "google_accounts.json")

now_utc = datetime.now(timezone.utc)
now_ms = now_utc.timestamp() * 1000
now_local = datetime.now()

local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
today_ts = local_midnight.timestamp() * 1000
week_ts = (local_midnight - timedelta(days=7)).timestamp() * 1000
month_ts = (local_midnight - timedelta(days=30)).timestamp() * 1000

HOURLY_WINDOW = 12
hourly_cutoff_ts = (now_utc - timedelta(hours=HOURLY_WINDOW)).timestamp() * 1000

# --- Account & tier ---
account = ""
auth_type = "oauth-personal"
try:
    with open(accounts_file) as f:
        acc = json.load(f)
    if isinstance(acc, dict):
        account = acc.get("email", acc.get("active", ""))
    elif isinstance(acc, list) and acc:
        account = acc[0] if isinstance(acc[0], str) else acc[0].get("email", acc[0].get("active", ""))
except:
    pass

try:
    with open(os.path.join(gemini_dir, "settings.json")) as f:
        settings = json.load(f)
    auth_type = settings.get("security", {}).get("auth", {}).get("selectedType", "oauth-personal")
except:
    pass

# Tier limits (requests per day)
TIER_LIMITS = {
    "oauth-personal": {"label": "Free", "requests_per_day": 1000},
    "oauth-workspace-standard": {"label": "Standard", "requests_per_day": 1500},
    "oauth-workspace-enterprise": {"label": "Enterprise", "requests_per_day": 2000},
    "api-key": {"label": "API Key", "requests_per_day": 250},
}
tier_info = TIER_LIMITS.get(auth_type, TIER_LIMITS["oauth-personal"])
daily_req_limit = tier_info["requests_per_day"]
tier_label = tier_info["label"]

# --- Accumulators ---
tok_today = {"input": 0, "output": 0, "cached": 0, "thoughts": 0, "tool": 0, "total": 0}
tok_week = {"input": 0, "output": 0, "cached": 0, "thoughts": 0, "tool": 0, "total": 0}
tok_month = {"input": 0, "output": 0, "cached": 0, "thoughts": 0, "tool": 0, "total": 0}
tok_all = {"input": 0, "output": 0, "cached": 0, "thoughts": 0, "tool": 0, "total": 0}

daily_token_map = {}
fine_token_map = {}
models_used = {}
prompts = {"today": 0, "week": 0, "month": 0, "total": 0}
requests = {"today": 0, "week": 0, "month": 0, "total": 0}  # gemini responses = API requests
session_count = 0
recent_sessions = []

FINE_BUCKET_MIN = 5

RATE_WINDOW_SHORT = 5 * 60 * 1000
RATE_WINDOW_LONG = 30 * 60 * 1000
rate_cutoff_ts = now_ms - RATE_WINDOW_LONG
recent_events = []  # [(ts_ms, inp, out)]

def _parse_iso_ts(ts_str):
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() * 1000
    except:
        return 0

def add_tokens(ts_ms, tokens):
    """Add token counts to accumulators by timestamp."""
    inp = tokens.get("input", 0)
    out = tokens.get("output", 0)
    cached = tokens.get("cached", 0)
    thoughts = tokens.get("thoughts", 0)
    tool = tokens.get("tool", 0)
    total = tokens.get("total", 0)

    tok_all["input"] += inp; tok_all["output"] += out; tok_all["cached"] += cached
    tok_all["thoughts"] += thoughts; tok_all["tool"] += tool; tok_all["total"] += total

    if ts_ms >= month_ts:
        tok_month["input"] += inp; tok_month["output"] += out; tok_month["cached"] += cached
        tok_month["thoughts"] += thoughts; tok_month["tool"] += tool; tok_month["total"] += total
        if ts_ms >= week_ts:
            tok_week["input"] += inp; tok_week["output"] += out; tok_week["cached"] += cached
            tok_week["thoughts"] += thoughts; tok_week["tool"] += tool; tok_week["total"] += total
            if ts_ms >= today_ts:
                tok_today["input"] += inp; tok_today["output"] += out; tok_today["cached"] += cached
                tok_today["thoughts"] += thoughts; tok_today["tool"] += tool; tok_today["total"] += total

    # Rate tracking
    if ts_ms >= rate_cutoff_ts:
        recent_events.append((ts_ms, inp + cached + thoughts + tool, out))

    # Daily bucket
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000)
        day = dt.strftime("%Y-%m-%d")
        if day not in daily_token_map:
            daily_token_map[day] = {"input": 0, "output": 0}
        daily_token_map[day]["input"] += inp + cached + thoughts + tool
        daily_token_map[day]["output"] += out

        # Fine bucket (5-min)
        if ts_ms >= hourly_cutoff_ts:
            minute = (dt.minute // FINE_BUCKET_MIN) * FINE_BUCKET_MIN
            fine_key = f"{day} {dt.hour:02d}:{minute:02d}"
            if fine_key not in fine_token_map:
                fine_token_map[fine_key] = {"input": 0, "output": 0}
            fine_token_map[fine_key]["input"] += inp + cached + thoughts + tool
            fine_token_map[fine_key]["output"] += out
    except:
        pass

# --- Parse all chat sessions ---
try:
    for chat_file in glob.glob(os.path.join(tmp_dir, "*/chats/session-*.json*")):
        try:
            with open(chat_file) as f:
                if chat_file.endswith(".jsonl"):
                    lines = f.readlines()
                    if not lines: continue
                    try:
                        header = json.loads(lines[0])
                    except:
                        header = {}
                    messages = []
                    for line in lines[1:]:
                        try:
                            messages.append(json.loads(line))
                        except:
                            continue
                    
                    sess_start = header.get("startTime", "")
                    sess_updated = header.get("lastUpdated", "")
                    sess_id = header.get("sessionId", "")
                else:
                    session = json.load(f)
                    messages = session.get("messages", [])
                    sess_start = session.get("startTime", "")
                    sess_updated = session.get("lastUpdated", "")
                    sess_id = session.get("sessionId", "")

            if not messages:
                continue

            session_count += 1
            sess_tokens = 0
            sess_model = ""
            sess_project = ""

            # Extract project name from path
            parts = chat_file.split("/chats/")
            if parts:
                sess_project = os.path.basename(parts[0])

            for msg in messages:
                msg_type = msg.get("type", "")
                ts_str = msg.get("timestamp", "")
                ts_ms = _parse_iso_ts(ts_str) if ts_str else 0

                if msg_type == "user":
                    prompts["total"] += 1
                    if ts_ms >= today_ts: prompts["today"] += 1
                    if ts_ms >= week_ts: prompts["week"] += 1
                    if ts_ms >= month_ts: prompts["month"] += 1
                    continue

                if msg_type == "gemini":
                    requests["total"] += 1
                    if ts_ms >= today_ts: requests["today"] += 1
                    if ts_ms >= week_ts: requests["week"] += 1
                    if ts_ms >= month_ts: requests["month"] += 1

                if msg_type != "gemini":
                    continue

                tokens = msg.get("tokens")
                if not tokens:
                    continue

                model = msg.get("model", "")
                if model:
                    sess_model = model
                    if model not in models_used:
                        models_used[model] = {"input": 0, "output": 0, "total": 0, "cached": 0, "thoughts": 0}
                    models_used[model]["input"] += tokens.get("input", 0)
                    models_used[model]["output"] += tokens.get("output", 0)
                    models_used[model]["total"] += tokens.get("total", 0)
                    models_used[model]["cached"] += tokens.get("cached", 0)
                    models_used[model]["thoughts"] += tokens.get("thoughts", 0)

                sess_tokens += tokens.get("total", 0)
                add_tokens(ts_ms, tokens)

            # Add to recent sessions
            if sess_tokens > 0:
                start_ts = _parse_iso_ts(sess_start)
                updated_ts = _parse_iso_ts(sess_updated)
                duration_min = (updated_ts - start_ts) / 60000 if start_ts and updated_ts else 0
                recent_sessions.append({
                    "id": sess_id[:8],
                    "tokens": sess_tokens,
                    "model": sess_model,
                    "timestamp": sess_updated or sess_start,
                    "duration_min": round(duration_min, 1),
                    "project": sess_project,
                })
        except:
            pass
except:
    pass

recent_sessions.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

# --- Build daily chart (last 8 days) ---
daily_tokens = []
for i in range(7, -1, -1):
    d = (now_local - timedelta(days=i)).strftime("%Y-%m-%d")
    entry = daily_token_map.get(d, {"input": 0, "output": 0})
    daily_tokens.append({"day": d, "input": entry["input"], "output": entry["output"]})

# --- Build fine chart (12h) ---
fine_tokens = []
base_dt = now_local.replace(second=0, microsecond=0)
base_minute = (base_dt.minute // FINE_BUCKET_MIN) * FINE_BUCKET_MIN
base_dt = base_dt.replace(minute=base_minute)
total_buckets = (HOURLY_WINDOW * 60) // FINE_BUCKET_MIN
for i in range(total_buckets - 1, -1, -1):
    dt_b = base_dt - timedelta(minutes=i * FINE_BUCKET_MIN)
    fine_key = f"{dt_b.strftime('%Y-%m-%d')} {dt_b.hour:02d}:{dt_b.minute:02d}"
    label = f"{dt_b.hour:02d}:{dt_b.minute:02d}"
    entry = fine_token_map.get(fine_key, {"input": 0, "output": 0})
    fine_tokens.append({"t": label, "input": entry["input"], "output": entry["output"]})

# --- Active sessions (check for running gemini processes) ---
active_sessions = []
active_count = 0
try:
    import subprocess
    result = subprocess.run(["pgrep", "-af", "gemini"], capture_output=True, text=True, timeout=2)
    seen_pids = set()
    for line in result.stdout.strip().split("\n"):
        if not line or "pgrep" in line or "gemini_local_stats" in line or "gemini_stats" in line:
            continue
        if "/usr/bin/gemini" not in line and "gemini" not in line.lower():
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
            if not os.path.exists(f"/proc/{pid}"):
                continue
            # Skip child processes — only count the parent (ppid != another gemini pid)
            ppid_file = f"/proc/{pid}/stat"
            with open(ppid_file) as pf:
                stat_fields = pf.read().split()
                ppid = int(stat_fields[3])
            if ppid in seen_pids:
                continue  # child of another gemini process
            seen_pids.add(pid)
            active_count += 1
            # Collect this pid + all child pids for I/O polling
            all_pids = [pid]
            try:
                children = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=1)
                for cline in children.stdout.strip().split("\n"):
                    if cline.strip():
                        all_pids.append(int(cline.strip()))
            except:
                pass
            active_sessions.append({"pid": pid, "pids": all_pids, "cmd": parts[1] if len(parts) > 1 else ""})
        except:
            pass
except:
    pass

# --- Throughput rates ---
def calc_rate(window_ms, extract_fn):
    cutoff_r = now_ms - window_ms
    filtered = [(ts, extract_fn(inp, out)) for ts, inp, out in recent_events if ts >= cutoff_r]
    total = sum(v for _, v in filtered)
    if total == 0: return 0.0
    earliest = min(ts for ts, _ in filtered)
    span_h = max(now_ms - earliest, 60_000) / 3_600_000
    return total / span_h

if recent_events:
    rate_output_5m = calc_rate(RATE_WINDOW_SHORT, lambda i, o: o)
    rate_output_30m = calc_rate(RATE_WINDOW_LONG, lambda i, o: o)
    rate_all_5m = calc_rate(RATE_WINDOW_SHORT, lambda i, o: i + o)
    rate_all_30m = calc_rate(RATE_WINDOW_LONG, lambda i, o: i + o)
else:
    rate_output_5m = rate_output_30m = rate_all_5m = rate_all_30m = 0.0

print(json.dumps({
    "account": account,
    "tier": tier_label,
    "auth_type": auth_type,
    "quota": {
        "requests_today": requests["today"],
        "requests_limit": daily_req_limit,
    },
    "sessions": {"active": active_count, "total": session_count},
    "prompts": prompts,
    "requests": requests,
    "tokens": {
        "today": tok_today,
        "week": tok_week,
        "month": tok_month,
        "total": tok_all,
    },
    "throughput": {
        "rate_output_5m": round(rate_output_5m),
        "rate_output_30m": round(rate_output_30m),
        "rate_all_5m": round(rate_all_5m),
        "rate_all_30m": round(rate_all_30m),
    },
    "daily_tokens": daily_tokens,
    "fine_tokens": fine_tokens,
    "recent_sessions": recent_sessions[:8],
    "active_sessions": active_sessions,
    "models_used": models_used,
}))
