#!/usr/bin/env python3
"""
Hevy → Intervals.icu Sync Service
==================================
Syncs strength workouts from Hevy to Intervals.icu as WeightTraining activities.

Two modes:
  1. Webhook  — FastAPI endpoint that Hevy calls on workout completion
  2. Backfill — CLI command to sync all historical workouts

Setup:
  1. pip install fastapi uvicorn httpx python-dotenv
  2. Copy .env.example → .env and fill in your keys
  3. uvicorn hevy_intervals_sync:app --host 0.0.0.0 --port 8400
  4. In Hevy Settings → Developer → Webhook URL: https://your-server:8400/webhook/hevy
     Authorization header: Bearer <your WEBHOOK_SECRET from .env>

Author: github.com/sebdenes
"""

import os
import json
import time
import logging
import hashlib
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse

# ─── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

HEVY_API_KEY = os.getenv("HEVY_API_KEY", "")
HEVY_BASE_URL = os.getenv("HEVY_BASE_URL", "https://api.hevyapp.com")

INTERVALS_API_KEY = os.getenv("INTERVALS_API_KEY", "")
INTERVALS_ATHLETE_ID = os.getenv("INTERVALS_ATHLETE_ID", "0")  # "0" = self
INTERVALS_BASE_URL = os.getenv("INTERVALS_BASE_URL", "https://intervals.icu")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # protects the webhook endpoint

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ─── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hevy-icu-sync")

# ─── Hevy API Client ───────────────────────────────────────────────────────────


class HevyClient:
    """Lightweight wrapper around the Hevy public API v1."""

    def __init__(self, api_key: str, base_url: str = HEVY_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.headers = {"api-key": api_key, "accept": "application/json"}

    def get_workouts(self, page: int = 1, page_size: int = 10) -> dict:
        """GET /v1/workouts — paginated list of workouts (newest first)."""
        url = f"{self.base_url}/v1/workouts"
        params = {"page": page, "pageSize": page_size}
        r = httpx.get(url, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_workout(self, workout_id: str) -> dict:
        """GET /v1/workouts/{id} — single workout with full exercise detail."""
        url = f"{self.base_url}/v1/workouts/{workout_id}"
        r = httpx.get(url, headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_workout_count(self) -> int:
        """GET /v1/workouts/count — total number of workouts."""
        url = f"{self.base_url}/v1/workouts/count"
        r = httpx.get(url, headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json().get("workout_count", 0)

    def get_workout_events(self, since: str, page: int = 1, page_size: int = 10) -> dict:
        """GET /v1/workout_events — new/updated workouts since ISO timestamp."""
        url = f"{self.base_url}/v1/workout_events"
        params = {"since": since, "page": page, "pageSize": page_size}
        r = httpx.get(url, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()


# ─── Intervals.icu API Client ──────────────────────────────────────────────────


class IntervalsClient:
    """Lightweight wrapper around the Intervals.icu REST API."""

    def __init__(
        self,
        api_key: str,
        athlete_id: str = "0",
        base_url: str = INTERVALS_BASE_URL,
    ):
        self.base_url = base_url.rstrip("/")
        self.athlete_id = athlete_id
        # Intervals.icu uses Basic auth: API_KEY:<key>
        self.auth = ("API_KEY", api_key)

    def create_manual_activity(self, payload: dict) -> dict:
        """POST /api/v1/athlete/{id}/activities/manual"""
        url = f"{self.base_url}/api/v1/athlete/{self.athlete_id}/activities/manual"
        r = httpx.post(url, auth=self.auth, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_activities(self, oldest: str, newest: str) -> list:
        """GET /api/v1/athlete/{id}/activities — list activities in date range."""
        url = f"{self.base_url}/api/v1/athlete/{self.athlete_id}/activities"
        params = {"oldest": oldest, "newest": newest}
        r = httpx.get(url, auth=self.auth, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_athlete_timezone(self) -> str:
        """GET /api/v1/athlete/{id} — return the athlete's timezone string."""
        url = f"{self.base_url}/api/v1/athlete/{self.athlete_id}"
        r = httpx.get(url, auth=self.auth, timeout=30)
        r.raise_for_status()
        return r.json().get("timezone", "UTC")

    def activity_exists(self, external_id: str) -> bool:
        """Check if an activity with the given external_id already exists.
        Uses a date-range search + filter. Not perfectly efficient but safe."""
        # We search a wide window and filter — Intervals.icu doesn't have
        # a direct external_id lookup endpoint.
        # For production, maintain a local SQLite ledger instead (see SyncLedger).
        return False  # defer to ledger


# ─── Data Transformer ──────────────────────────────────────────────────────────

# Map Hevy workout_type strings to Intervals.icu activity types
HEVY_TO_ICU_TYPE = {
    "weight_training": "WeightTraining",
    "traditional_strength_training": "WeightTraining",
    "functional_strength_training": "WeightTraining",
    "strength_training": "WeightTraining",
    "cardio": "Workout",
    "stretching": "Yoga",
    "yoga": "Yoga",
    "pilates": "Yoga",
    "cycling": "Ride",
    "running": "Run",
    "walking": "Walk",
    "hiit": "Workout",
    "other": "Workout",
}

# Map exercise name keywords to muscle group tags
MUSCLE_GROUP_KEYWORDS = {
    "Chest": ["bench press", "chest press", "pec ", "fly", "flye", "push up", "pushup", "incline press", "decline press"],
    "Back": ["row", "pull up", "pullup", "pulldown", "lat ", "deadlift", "back extension"],
    "Shoulders": ["shoulder press", "lateral raise", "overhead press", "military press", "front raise", "rear delt", "shrug"],
    "Biceps": ["bicep", "curl", "hammer curl", "preacher"],
    "Triceps": ["tricep", "triceps", "skull crusher", "pushdown", "dip"],
    "Legs": ["squat", "leg press", "leg curl", "leg extension", "lunge", "calf", "hip abduct", "hip adduct", "glute", "hamstring", "quad"],
    "Core": ["crunch", "plank", "ab ", "abs", "sit up", "situp", "oblique", "core"],
}


def detect_muscle_groups(exercises: list) -> list[str]:
    """Detect muscle groups from exercise titles."""
    groups = set()
    for ex in exercises:
        title = ex.get("title", "").lower()
        for group, keywords in MUSCLE_GROUP_KEYWORDS.items():
            if any(kw in title for kw in keywords):
                groups.add(group)
    return sorted(groups)


def format_exercise_description(exercises: list) -> str:
    """Convert Hevy exercise list to a readable Markdown-ish description
    that looks great in the Intervals.icu activity notes."""

    lines = []
    total_volume_kg = 0.0

    for ex in exercises:
        title = ex.get("title", "Unknown Exercise")
        notes = ex.get("notes", "")
        sets = ex.get("sets", [])

        set_lines = []
        ex_volume = 0.0

        for s in sets:
            set_type = s.get("set_type", "normal")
            prefix = ""
            if set_type == "warmup":
                prefix = "W "
            elif set_type == "dropset":
                prefix = "D "
            elif set_type == "failure":
                prefix = "F "

            weight = s.get("weight_kg")
            reps = s.get("reps")
            duration = s.get("duration_seconds")
            distance = s.get("distance_meters")
            rpe = s.get("rpe")

            parts = []
            if weight is not None and weight > 0:
                parts.append(f"{weight:.1f}kg")
            if reps is not None and reps > 0:
                parts.append(f"{reps} reps")
            if duration is not None and duration > 0:
                mins, secs = divmod(int(duration), 60)
                parts.append(f"{mins}:{secs:02d}")
            if distance is not None and distance > 0:
                parts.append(f"{distance:.0f}m")
            if rpe is not None and rpe > 0:
                parts.append(f"RPE {rpe:.0f}")

            set_str = f"  {prefix}{' × '.join(parts)}" if parts else "  (bodyweight)"
            set_lines.append(set_str)

            # Volume = weight × reps
            if weight and reps:
                ex_volume += weight * reps

        total_volume_kg += ex_volume

        header = f"▸ {title}"
        if ex_volume > 0:
            header += f"  [{ex_volume:.0f} kg]"
        if notes:
            header += f"\n  📝 {notes}"
        lines.append(header)
        lines.extend(set_lines)
        lines.append("")  # blank line between exercises

    # Summary line at top
    summary = f"Total volume: {total_volume_kg:.0f} kg  |  {len(exercises)} exercises"
    return summary + "\n" + "─" * 40 + "\n\n" + "\n".join(lines)


def estimate_training_load(total_kg: float, moving_time_sec: int, exercises: list) -> int:
    """Estimate training load for strength training.

    Based on the TrainingPeaks tonnage method:
      TSS = Tonnage / estimated_RPE * scaling_factor

    When actual RPE is not available, we estimate RPE from set density
    (sets per minute). The scaling factor is calibrated so that a typical
    moderate 60-min session (~20,000 kg, RPE 6) produces load ~50-60.

    Reference points:
      - Light (10,000 kg, easy pace): ~25-35
      - Moderate (20,000 kg, steady): ~45-60
      - Heavy (30,000+ kg, intense): ~65-85
    """
    if moving_time_sec <= 0:
        return 0

    duration_min = moving_time_sec / 60

    # Count working sets (excluding warmups)
    total_sets = sum(
        1 for ex in exercises for s in ex.get("sets", [])
        if s.get("set_type", "normal") != "warmup"
        and (s.get("weight_kg") or s.get("reps"))
    )

    # Estimate RPE from set density (sets/min):
    #   ~0.3 sets/min → RPE 4 (easy)
    #   ~0.5 sets/min → RPE 6 (moderate)
    #   ~0.7+ sets/min → RPE 8 (hard)
    set_density = total_sets / duration_min if duration_min > 0 else 0.5
    estimated_rpe = min(10, max(3, round(2 + set_density * 8)))

    # Tonnage-based load: scale so ~20,000 kg at RPE 6 ≈ 50 load
    # Formula: tonnage / (rpe * scaling), where scaling ≈ 65
    if total_kg > 0:
        load = total_kg / (estimated_rpe * 65)
    else:
        # Bodyweight-only: fall back to set-count method
        load = total_sets * 1.2 + len(exercises) * 0.5

    return round(min(max(load, 10), 120))


def hevy_workout_to_icu_payload(workout: dict, athlete_tz: str = "UTC") -> dict:
    """Transform a Hevy workout object into an Intervals.icu manual activity payload."""

    workout_id = workout.get("id", "unknown")
    title = workout.get("title", "Strength Workout")
    description = workout.get("description", "")
    start_time = workout.get("start_time", "")
    end_time = workout.get("end_time", "")
    exercises = workout.get("exercises", [])

    # Calculate moving time in seconds
    moving_time = 0
    if start_time and end_time:
        try:
            st = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            et = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            moving_time = int((et - st).total_seconds())
        except (ValueError, TypeError):
            pass

    # Map type
    icu_type = "WeightTraining"

    # Build description
    exercise_desc = format_exercise_description(exercises)
    full_description = exercise_desc
    if description:
        full_description = f"{description}\n\n{exercise_desc}"

    # Calculate total volume
    total_kg = 0.0
    for ex in exercises:
        for s in ex.get("sets", []):
            w = s.get("weight_kg") or 0
            r = s.get("reps") or 0
            total_kg += w * r

    # Convert UTC start time to athlete's local timezone
    start_local = start_time
    if start_local:
        try:
            dt = datetime.fromisoformat(start_local.replace("Z", "+00:00"))
            local_dt = dt.astimezone(ZoneInfo(athlete_tz))
            start_local = local_dt.strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError, KeyError):
            # Fallback: strip timezone (old behavior)
            try:
                dt = datetime.fromisoformat(start_local.replace("Z", "+00:00"))
                start_local = dt.strftime("%Y-%m-%dT%H:%M:%S")
            except (ValueError, TypeError):
                pass

    payload = {
        "type": icu_type,
        "name": title,
        "description": full_description,
        "start_date_local": start_local,
        "external_id": f"hevy_{workout_id}",
        "trainer": True,
    }

    if moving_time > 0:
        payload["moving_time"] = moving_time
        payload["elapsed_time"] = moving_time

    if total_kg > 0:
        payload["kg_lifted"] = round(total_kg)

    # Use RPE from sets if available, otherwise estimate load directly
    rpe_values = [
        s.get("rpe") for ex in exercises for s in ex.get("sets", [])
        if s.get("rpe") and s["rpe"] > 0
    ]
    if rpe_values:
        payload["icu_rpe"] = round(sum(rpe_values) / len(rpe_values))
    elif moving_time > 0:
        # No RPE data — estimate training load from volume/duration
        load = estimate_training_load(total_kg, moving_time, exercises)
        if load > 0:
            payload["icu_training_load"] = load

    return payload


# ─── Sync Ledger (SQLite) ──────────────────────────────────────────────────────

import sqlite3

DB_PATH = os.getenv("SYNC_DB_PATH", "hevy_icu_sync.db")


class SyncLedger:
    """Tracks which Hevy workouts have been synced to Intervals.icu."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS synced_workouts (
                    hevy_workout_id TEXT PRIMARY KEY,
                    icu_activity_id TEXT,
                    synced_at       TEXT NOT NULL,
                    hevy_updated_at TEXT,
                    checksum        TEXT
                )
            """)
            conn.commit()

    def is_synced(self, hevy_workout_id: str, checksum: str = "") -> bool:
        """Check if a workout has already been synced (and hasn't changed)."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT checksum FROM synced_workouts WHERE hevy_workout_id = ?",
                (hevy_workout_id,),
            ).fetchone()
            if row is None:
                return False
            if checksum and row[0] != checksum:
                return False  # workout was updated
            return True

    def record_sync(
        self,
        hevy_workout_id: str,
        icu_activity_id: str,
        hevy_updated_at: str = "",
        checksum: str = "",
    ):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO synced_workouts
                   (hevy_workout_id, icu_activity_id, synced_at, hevy_updated_at, checksum)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    hevy_workout_id,
                    icu_activity_id,
                    datetime.now(timezone.utc).isoformat(),
                    hevy_updated_at,
                    checksum,
                ),
            )
            conn.commit()

    def get_sync_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM synced_workouts").fetchone()
            return row[0] if row else 0


def workout_checksum(workout: dict) -> str:
    """Deterministic hash of a workout to detect updates."""
    raw = json.dumps(workout, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ─── Core Sync Logic ───────────────────────────────────────────────────────────


def sync_single_workout(
    workout: dict,
    hevy: HevyClient,
    icu: IntervalsClient,
    ledger: SyncLedger,
    force: bool = False,
    athlete_tz: str = "UTC",
) -> Optional[str]:
    """Sync one Hevy workout → Intervals.icu. Returns ICU activity ID or None."""

    workout_id = str(workout.get("id", ""))
    if not workout_id:
        log.warning("Workout has no ID, skipping")
        return None

    cs = workout_checksum(workout)

    if not force and ledger.is_synced(workout_id, cs):
        log.debug(f"Workout {workout_id} already synced, skipping")
        return None

    payload = hevy_workout_to_icu_payload(workout, athlete_tz=athlete_tz)
    log.info(
        f"Syncing workout {workout_id}: \"{payload.get('name')}\" "
        f"({payload.get('start_date_local')})"
    )

    try:
        result = icu.create_manual_activity(payload)
        icu_id = result.get("id", "unknown")
        ledger.record_sync(
            hevy_workout_id=workout_id,
            icu_activity_id=icu_id,
            hevy_updated_at=workout.get("updated_at", ""),
            checksum=cs,
        )
        log.info(f"✓ Synced → Intervals.icu activity {icu_id}")
        return icu_id
    except httpx.HTTPStatusError as e:
        log.error(f"✗ Failed to sync workout {workout_id}: {e.response.status_code} {e.response.text}")
        return None
    except Exception as e:
        log.error(f"✗ Unexpected error syncing {workout_id}: {e}")
        return None


def backfill_all(force: bool = False):
    """Sync all historical Hevy workouts to Intervals.icu."""

    if not HEVY_API_KEY or not INTERVALS_API_KEY:
        log.error("Missing API keys. Set HEVY_API_KEY and INTERVALS_API_KEY in .env")
        return

    hevy = HevyClient(HEVY_API_KEY)
    icu = IntervalsClient(INTERVALS_API_KEY, INTERVALS_ATHLETE_ID)
    ledger = SyncLedger()

    # Fetch athlete timezone once for all workouts
    try:
        athlete_tz = icu.get_athlete_timezone()
        log.info(f"Athlete timezone: {athlete_tz}")
    except Exception:
        athlete_tz = "UTC"
        log.warning("Could not fetch athlete timezone, using UTC")

    total = hevy.get_workout_count()
    log.info(f"Hevy reports {total} total workouts. Starting backfill...")

    page = 1
    page_size = 10  # Hevy API max per page
    synced = 0
    skipped = 0

    while True:
        data = hevy.get_workouts(page=page, page_size=page_size)
        workouts = data.get("workouts", [])

        if not workouts:
            break

        for w in workouts:
            result = sync_single_workout(w, hevy, icu, ledger, force=force, athlete_tz=athlete_tz)
            if result:
                synced += 1
            else:
                skipped += 1

            # Be polite to both APIs
            time.sleep(0.5)

        page_count = data.get("page_count", 1)
        log.info(f"Page {page}/{page_count} done ({synced} synced, {skipped} skipped)")

        if page >= page_count:
            break
        page += 1

    log.info(f"Backfill complete: {synced} synced, {skipped} skipped. "
             f"Ledger total: {ledger.get_sync_count()}")


def sync_latest(count: int = 5, force: bool = False):
    """Sync the most recent Hevy workouts to Intervals.icu.

    Quick sync for manual use — only fetches the first page of workouts
    (most recent first) and syncs any that are new or updated.
    """

    if not HEVY_API_KEY or not INTERVALS_API_KEY:
        log.error("Missing API keys. Set HEVY_API_KEY and INTERVALS_API_KEY in .env")
        return

    hevy = HevyClient(HEVY_API_KEY)
    icu = IntervalsClient(INTERVALS_API_KEY, INTERVALS_ATHLETE_ID)
    ledger = SyncLedger()

    try:
        athlete_tz = icu.get_athlete_timezone()
    except Exception:
        athlete_tz = "UTC"

    data = hevy.get_workouts(page=1, page_size=count)
    workouts = data.get("workouts", [])

    if not workouts:
        log.info("No workouts found in Hevy")
        return

    synced = 0
    for w in workouts:
        result = sync_single_workout(w, hevy, icu, ledger, force=force, athlete_tz=athlete_tz)
        if result:
            synced += 1
        time.sleep(0.3)

    if synced:
        log.info(f"Synced {synced} new workout(s)")
    else:
        log.info("All recent workouts already synced — nothing to do")


# ─── FastAPI App (Webhook Mode) ────────────────────────────────────────────────

app = FastAPI(
    title="Hevy → Intervals.icu Sync",
    description="Receives Hevy webhooks and syncs strength workouts to Intervals.icu",
    version="1.0.0",
)


@app.on_event("startup")
async def startup():
    if not HEVY_API_KEY:
        log.warning("HEVY_API_KEY not set — webhook will fetch workout but needs the key")
    if not INTERVALS_API_KEY:
        log.warning("INTERVALS_API_KEY not set — cannot push to Intervals.icu")
    log.info("Hevy → Intervals.icu sync service started")
    log.info(f"Ledger: {SyncLedger().get_sync_count()} workouts previously synced")


@app.get("/health")
async def health():
    """Health check endpoint."""
    ledger = SyncLedger()
    return {
        "status": "healthy",
        "synced_workouts": ledger.get_sync_count(),
        "hevy_configured": bool(HEVY_API_KEY),
        "intervals_configured": bool(INTERVALS_API_KEY),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/webhook/hevy")
async def hevy_webhook(request: Request, authorization: Optional[str] = Header(None)):
    """Webhook endpoint that Hevy calls when a workout is completed.

    Hevy webhook sends the workout data in the body. We also re-fetch from the
    API to get the full exercise detail (webhooks may be partial).
    """

    # ── Auth check ──
    if WEBHOOK_SECRET:
        expected = f"Bearer {WEBHOOK_SECRET}"
        if authorization != expected:
            log.warning(f"Unauthorized webhook attempt from {request.client.host}")
            raise HTTPException(status_code=401, detail="Unauthorized")

    # ── Parse body ──
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    log.info(f"Received Hevy webhook: {json.dumps(body, default=str)[:200]}...")

    # Hevy webhook payload structure varies — it may contain the workout
    # directly or a notification. We handle both patterns.
    workout = None

    # Pattern 1: body is the workout itself (has "id" and "exercises")
    if "id" in body and "exercises" in body:
        workout = body

    # Pattern 2: body has a "workout" key
    elif "workout" in body:
        workout = body["workout"]

    # Pattern 3: body has a "workout_id" — need to fetch from API
    elif "workout_id" in body:
        workout_id = body["workout_id"]
        log.info(f"Webhook contains workout_id={workout_id}, fetching from Hevy API...")
        try:
            hevy = HevyClient(HEVY_API_KEY)
            result = hevy.get_workout(workout_id)
            workout = result.get("workout", result)
        except Exception as e:
            log.error(f"Failed to fetch workout {workout_id} from Hevy: {e}")
            raise HTTPException(status_code=502, detail="Failed to fetch workout from Hevy")

    # Pattern 4: body has an "event" type (e.g. workout_created)
    elif "event" in body:
        event_type = body.get("event", "")
        log.info(f"Hevy event type: {event_type}")
        if "workout" in event_type.lower() or "workout" in body:
            # Try to extract workout_id from the event data
            data = body.get("data", body)
            workout_id = data.get("workout_id") or data.get("id")
            if workout_id:
                try:
                    hevy = HevyClient(HEVY_API_KEY)
                    result = hevy.get_workout(workout_id)
                    workout = result.get("workout", result)
                except Exception as e:
                    log.error(f"Failed to fetch workout from event: {e}")

    if not workout:
        log.warning("Could not extract workout from webhook payload")
        # Return 200 to prevent Hevy from retrying
        return JSONResponse({"status": "ignored", "reason": "no workout data found"})

    # ── Sync ──
    hevy = HevyClient(HEVY_API_KEY)
    icu = IntervalsClient(INTERVALS_API_KEY, INTERVALS_ATHLETE_ID)
    ledger = SyncLedger()

    try:
        athlete_tz = icu.get_athlete_timezone()
    except Exception:
        athlete_tz = "UTC"

    icu_id = sync_single_workout(workout, hevy, icu, ledger, athlete_tz=athlete_tz)

    if icu_id:
        return JSONResponse({"status": "synced", "icu_activity_id": icu_id})
    else:
        return JSONResponse({"status": "skipped", "reason": "already synced or error"})


@app.post("/sync/backfill")
async def trigger_backfill(authorization: Optional[str] = Header(None)):
    """Manually trigger a backfill of all Hevy workouts.
    Protected by the same WEBHOOK_SECRET."""

    if WEBHOOK_SECRET:
        expected = f"Bearer {WEBHOOK_SECRET}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    import threading

    thread = threading.Thread(target=backfill_all, daemon=True)
    thread.start()
    return JSONResponse({
        "status": "backfill_started",
        "message": "Backfill running in background. Check logs for progress.",
    })


@app.get("/sync/status")
async def sync_status():
    """Return sync statistics."""
    ledger = SyncLedger()
    hevy = HevyClient(HEVY_API_KEY)

    try:
        hevy_count = hevy.get_workout_count()
    except Exception:
        hevy_count = -1

    synced_count = ledger.get_sync_count()

    return {
        "hevy_total_workouts": hevy_count,
        "synced_workouts": synced_count,
        "pending": max(0, hevy_count - synced_count) if hevy_count >= 0 else "unknown",
    }


# ─── CLI Entry Point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    force = "--force" in sys.argv

    if cmd == "sync":
        sync_latest(force=force)
    elif cmd == "backfill":
        backfill_all(force=force)
    elif cmd == "serve":
        import uvicorn
        port = int(os.getenv("PORT", "8400"))
        uvicorn.run(app, host="0.0.0.0", port=port)
    elif cmd == "status":
        hevy = HevyClient(HEVY_API_KEY)
        ledger = SyncLedger()
        try:
            hevy_count = hevy.get_workout_count()
        except Exception:
            hevy_count = -1
        synced_count = ledger.get_sync_count()
        pending = max(0, hevy_count - synced_count) if hevy_count >= 0 else "?"
        print(f"Hevy workouts:   {hevy_count}")
        print(f"Synced:          {synced_count}")
        print(f"Pending:         {pending}")
    else:
        print("""
Hevy → Intervals.icu Sync Service
==================================

Usage:
  python hevy_intervals_sync.py sync [--force]       Sync recent workouts (quick)
  python hevy_intervals_sync.py backfill [--force]   Sync all historical workouts
  python hevy_intervals_sync.py status               Show sync statistics
  python hevy_intervals_sync.py serve                Start webhook server

Schedule with cron (e.g. every 30 minutes):
  */30 * * * * cd /path/to/hevy2intervals && python3 hevy_intervals_sync.py sync

Or run the webhook server with uvicorn:
  uvicorn hevy_intervals_sync:app --host 0.0.0.0 --port 8400
""")
