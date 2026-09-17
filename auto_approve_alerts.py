#!/usr/bin/env python3
"""
Standalone auto-approve worker for CastBoard alert requests — runs on a
GitHub Actions schedule instead of needing the desktop admin panel open.

This is a deliberately independent reimplementation of admin_panel.py's
own auto-approve logic (_process_auto_approvals, alert_appearance_from_
request, _find_referenced_alert_index, github_publish_file, mark_resolved/
sync_resolved_ids) — not an import of it, since admin_panel.py pulls in
tkinter and a great deal of desktop-only UI code that has no place (and
won't even import) in a headless CI runner. The actual approval RULES
here are copied to match exactly; if those rules ever change in
admin_panel.py, this file needs the same change made by hand, or the two
will quietly drift apart.

One real architectural difference from the desktop version, not just a
port: admin_panel.py keeps its own local copy of every device's alerts
in a file on that computer, and publishes FROM that local copy. A GitHub
Actions runner starts completely fresh every single run with no
persistent local storage at all — so this script instead fetches each
relevant device's CURRENT alerts directly from GitHub on demand, applies
the change, and publishes straight back. GitHub itself is already the
one persistent source of truth here, so there's no local copy to keep
in sync with it in the first place.

Requires GITHUB_TOKEN in the environment (set from a GitHub Actions
secret in the workflow — see auto-approve-alerts.yml).
"""

import base64
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

GITHUB_DATA_REPO_OWNER = "itsolutions770-eng"
GITHUB_DATA_REPO_NAME = "castboard-data"

ALERT_REQUESTS_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/1liphL6PVLqTVsg_2fYmN57gC6OkBD0R0Uk9RC1xOhLs"
    "/gviz/tq?tqx=out:csv&gid=0"
)

# Published by the admin panel whenever a device's auto-approve toggle
# changes (see admin_panel.py's on_auto_approve_toggled) — this script
# reads that shared list rather than any local config, since a GitHub
# Actions runner has no access to any particular computer's settings.
AUTO_APPROVE_DEVICES_PATH = "config/auto_approve_alert_devices.json"


def log(message):
    print(message, flush=True)


def github_get_raw(repo_path):
    """Fetches a file's raw current content from castboard-data — used
    for both a device's current alerts and the resolved-ids list.
    Returns None if the file doesn't exist yet or can't be reached;
    callers treat that the same as "empty" rather than erroring, since
    a file not existing yet is a completely normal, expected state here
    (a device with no alerts yet, or nothing resolved yet)."""
    url = (
        f"https://raw.githubusercontent.com/{GITHUB_DATA_REPO_OWNER}/{GITHUB_DATA_REPO_NAME}"
        f"/main/{repo_path}?t={int(time.time())}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.read().decode("utf-8")
    except Exception:
        return None


def github_publish_file(token, repo_path, content_bytes, commit_message):
    """Identical contract and behavior to admin_panel.py's own
    github_publish_file — see that function's own docstring for the
    full explanation of the sha/optimistic-concurrency dance. Returns
    (True, "") on success or (False, reason) on failure; never raises."""
    api_url = f"https://api.github.com/repos/{GITHUB_DATA_REPO_OWNER}/{GITHUB_DATA_REPO_NAME}/contents/{repo_path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "CastBoard-Auto-Approve-Worker",
    }

    existing_sha = None
    try:
        get_req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(get_req, timeout=15) as response:
            existing_sha = json.loads(response.read().decode("utf-8")).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False, f"Couldn't check existing file (HTTP {e.code})"
    except Exception as e:
        return False, f"Couldn't reach GitHub: {e}"

    body = {
        "message": commit_message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if existing_sha:
        body["sha"] = existing_sha

    try:
        put_req = urllib.request.Request(
            api_url, data=json.dumps(body).encode("utf-8"), headers=headers, method="PUT"
        )
        with urllib.request.urlopen(put_req, timeout=30) as response:
            if response.status not in (200, 201):
                return False, f"Unexpected response (HTTP {response.status})"
        return True, ""
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "GitHub rejected the token."
        return False, f"GitHub publish failed (HTTP {e.code})"
    except Exception as e:
        return False, f"Couldn't reach GitHub: {e}"


def _find_column(row_lower, *starts_with_options):
    """Identical to admin_panel.py's own _find_column — see that
    function's docstring for why prefix matching is used here."""
    for key, value in row_lower.items():
        for option in starts_with_options:
            if key.startswith(option):
                return (value or "").strip()
    return ""


def load_alert_requests(resolved_ids):
    """Same source and column mapping as admin_panel.py's own
    load_alert_requests — see that function for the full field list.
    Takes the already-fetched resolved_ids set directly, rather than a
    config dict, since this script has no local config at all."""
    try:
        with urllib.request.urlopen(ALERT_REQUESTS_CSV_URL, timeout=15) as response:
            content = response.read().decode("utf-8")
    except Exception:
        return []

    requests_list = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        row_lower = {(k or "").strip().lower(): v for k, v in row.items()}
        device_id = _find_column(row_lower, "device id")
        if not device_id:
            continue
        requested_at = _find_column(row_lower, "timestamp")
        request_id = f"{device_id}_{requested_at}"
        if request_id in resolved_ids:
            continue
        requests_list.append({
            "device_id": device_id,
            "request_type": _find_column(row_lower, "request type"),
            "message": _find_column(row_lower, "message"),
            "time": _find_column(row_lower, "time ("),
            "repeats": _find_column(row_lower, "repeats"),
            "one_time_date": _find_column(row_lower, "one-time date"),
            "repeat_every": _find_column(row_lower, "repeat every"),
            "repeat_until": _find_column(row_lower, "repeat until"),
            "notes": _find_column(row_lower, "additional notes"),
            "requested_at": requested_at,
            "_request_id": request_id,
            "duration_seconds": _find_column(row_lower, "how long"),
            "box_size": _find_column(row_lower, "box size"),
            "text_size_sp": _find_column(row_lower, "text size"),
            "background_color": _find_column(row_lower, "background color"),
            "background_opacity": _find_column(row_lower, "background opacity"),
            "text_color": _find_column(row_lower, "text color"),
            "blink": _find_column(row_lower, "blink"),
            "transition": _find_column(row_lower, "entrance style"),
        })
    return requests_list


def alert_appearance_from_request(req, fallback):
    """Identical to admin_panel.py's own alert_appearance_from_request —
    copied verbatim; see that function's docstring."""
    def _int_or(value, default):
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return default

    def _hex_or(value, default):
        value = (value or "").strip()
        return value if re.match(r"^#[0-9A-Fa-f]{6}$", value) else default

    valid_box_sizes = {"full", "large", "medium", "small"}
    valid_transitions = {
        "none", "fade", "slide", "slide_bottom", "slide_left",
        "slide_right", "spin", "bounce", "zoom",
    }
    box_size = req.get("box_size", "").strip().lower()
    if box_size not in valid_box_sizes:
        box_size = fallback.get("box_size", "full")
    transition = req.get("transition", "").strip().lower()
    if transition not in valid_transitions:
        transition = fallback.get("transition", "fade")
    blink_raw = req.get("blink", "").strip().lower()
    blink = {"yes": True, "no": False}.get(blink_raw, fallback.get("blink", False))
    return {
        "duration_seconds": _int_or(req.get("duration_seconds"), fallback.get("duration_seconds", 15)),
        "background_color": _hex_or(req.get("background_color"), fallback.get("background_color", "#C9A24B")),
        "background_opacity": max(0, min(100, _int_or(
            req.get("background_opacity"), fallback.get("background_opacity", 100)
        ))),
        "text_color": _hex_or(req.get("text_color"), fallback.get("text_color", "#FFFFFF")),
        "text_size_sp": _int_or(req.get("text_size_sp"), fallback.get("text_size_sp", 32)),
        "box_size": box_size,
        "blink": blink,
        "transition": transition,
    }


def find_referenced_alert_index(device_alerts, notes):
    """Same matching logic as admin_panel.py's own
    _find_referenced_alert_index, adapted to take the device's alert
    list directly rather than looking it up from self.alerts_by_device."""
    match = re.search(r'Referring to existing alert: \u201c(.*)\u201d at (\d{2}:\d{2})', notes)
    if not match:
        return None
    ref_message, ref_time = match.group(1), match.group(2)
    for i, alert in enumerate(device_alerts):
        if alert.get("message") == ref_message and alert.get("time") == ref_time:
            return i
    return None


def get_device_alerts(device_id):
    raw = github_get_raw(f"alerts/{device_id}.json")
    if raw is None:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def publish_device_alerts(token, device_id, alerts):
    content_bytes = json.dumps(alerts, indent=2).encode("utf-8")
    return github_publish_file(
        token, f"alerts/{device_id}.json", content_bytes,
        f"Auto-approve: update alerts for {device_id}"
    )


def mark_resolved(token, resolved_ids_cache, request_id):
    """Publishes the updated resolved-ids list immediately — not
    batched until the end of the run — so a crash partway through
    still leaves already-processed requests correctly marked, rather
    than risking the same request (and a brand-new alert, for a "New
    alert" request specifically) being applied a second time on the
    next scheduled run. Same reasoning as admin_panel.py's own
    mark_resolved; this is the same safety property, just enforced by
    a different mechanism since there's no "local" half to fall back
    on here at all."""
    resolved_ids_cache.add(request_id)
    ok, reason = github_publish_file(
        token, "resolved/alert_requests.json",
        json.dumps(sorted(resolved_ids_cache)[-500:]).encode("utf-8"),
        "Mark alert request resolved (auto-approve worker)",
    )
    if not ok:
        log(f"  WARNING: resolved-id publish failed ({reason}) — this request may be reprocessed next run.")


def main():
    # Deliberately CASTBOARD_TOKEN, not GITHUB_TOKEN — see the workflow
    # file's own comment on this same naming choice.
    token = os.environ.get("CASTBOARD_TOKEN", "").strip()
    if not token:
        log("No CASTBOARD_TOKEN in environment — nothing this script can do. Exiting.")
        sys.exit(1)

    auto_approve_raw = github_get_raw(AUTO_APPROVE_DEVICES_PATH)
    auto_approve_devices = set(json.loads(auto_approve_raw)) if auto_approve_raw else set()
    if not auto_approve_devices:
        log("No devices have auto-approve enabled — nothing to do.")
        return

    resolved_raw = github_get_raw("resolved/alert_requests.json")
    resolved_ids = set(json.loads(resolved_raw)) if resolved_raw else set()

    requests = load_alert_requests(resolved_ids)
    relevant = [r for r in requests if r.get("device_id", "").strip() in auto_approve_devices]
    log(f"Fetched {len(requests)} pending request(s), {len(relevant)} for auto-approve devices.")
    if not relevant:
        return

    repeat_map = {"15 minutes": 15, "30 minutes": 30, "1 hour": 60, "2 hours": 120}
    applied_count = 0
    failed_count = 0

    for req in relevant:
        device_id = req["device_id"].strip()
        request_type = req.get("request_type", "")
        log(f"Processing: device={device_id} type={request_type} message={req.get('message', '')!r}")

        if request_type == "New alert":
            defaults = {
                "duration_seconds": 15, "background_color": "#C9A24B", "background_opacity": 100,
                "text_color": "#FFFFFF", "text_size_sp": 32, "box_size": "full",
                "blink": False, "transition": "fade",
            }
            device_alerts = get_device_alerts(device_id)
            new_alert = {
                "id": os.urandom(4).hex(),
                "message": req.get("message", ""),
                "time": req.get("time", ""),
                "recurrence": "daily" if req.get("repeats") == "Every day" else req.get("one_time_date", ""),
                "enabled": True,
                "repeat_interval_minutes": repeat_map.get(req.get("repeat_every", ""), 0),
                "repeat_until": req.get("repeat_until", "") or "23:59",
                **alert_appearance_from_request(req, defaults),
            }
            device_alerts.append(new_alert)
            ok, reason = publish_device_alerts(token, device_id, device_alerts)
            if not ok:
                log(f"  FAILED to publish: {reason} — will retry next run.")
                failed_count += 1
                continue
            mark_resolved(token, resolved_ids, req["_request_id"])
            applied_count += 1
            log("  Applied and published.")

        elif request_type in ("Change existing", "Remove"):
            device_alerts = get_device_alerts(device_id)
            match_idx = find_referenced_alert_index(device_alerts, req.get("notes", ""))
            if match_idx is None:
                log("  Couldn't confidently match the referenced alert — leaving pending for manual review.")
                continue
            original_alert = device_alerts[match_idx]
            if request_type == "Remove":
                del device_alerts[match_idx]
            else:
                device_alerts[match_idx] = {
                    **original_alert,
                    "message": req.get("message", ""),
                    "time": req.get("time", ""),
                    "recurrence": "daily" if req.get("repeats") == "Every day" else req.get("one_time_date", ""),
                    "repeat_interval_minutes": repeat_map.get(req.get("repeat_every", ""), 0),
                    "repeat_until": req.get("repeat_until", "") or "23:59",
                    **alert_appearance_from_request(req, original_alert),
                }
            ok, reason = publish_device_alerts(token, device_id, device_alerts)
            if not ok:
                log(f"  FAILED to publish: {reason} — will retry next run.")
                failed_count += 1
                continue
            mark_resolved(token, resolved_ids, req["_request_id"])
            applied_count += 1
            log("  Applied and published.")
        else:
            log(f"  Unrecognized request type {request_type!r} — leaving pending.")

    log(f"Done. Applied {applied_count}, failed {failed_count}.")


if __name__ == "__main__":
    main()
