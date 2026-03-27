# Hevy → Intervals.icu Sync Service

One-file Python service that syncs your Hevy strength workouts into Intervals.icu as `WeightTraining` activities — with full exercise detail, training load estimation, and automatic timezone handling.

## What it does

- **Webhook mode**: Hevy pings your server whenever you finish a workout → it appears in Intervals.icu within seconds
- **Backfill mode**: One command to sync all your historical Hevy workouts
- **Deduplication**: SQLite ledger tracks synced workouts by ID + checksum, so re-runs and retries are safe
- **Rich descriptions**: Each exercise with sets, weights, reps, RPE, and total volume — formatted for easy reading in Intervals.icu
- **Training load estimation**: Calculates load from tonnage and set density (or from your RPE if logged in Hevy)
- **Timezone-aware**: Automatically detects your timezone from your Intervals.icu profile — no manual configuration needed

## Architecture

```
┌──────────┐   webhook    ┌────────────────────┐   POST /activities/manual   ┌──────────────┐
│   Hevy   │ ──────────── │  hevy_intervals_   │ ───────────────────────────│ Intervals.icu │
│   App    │              │  sync.py (FastAPI)  │                            │               │
└──────────┘              │                     │                            └──────────────┘
                          │  SQLite ledger      │
                          │  (deduplication)    │
                          └────────────────────┘
```

## Quick Start

### Prerequisites

- **Hevy Pro** account — [get your API key](https://hevy.com/settings?developer)
- **Intervals.icu** account (free) — [get your API key](https://intervals.icu/settings) under Developer Settings
- **Python 3.10+**

### 1. Clone and install

```bash
git clone https://github.com/sebdenes/Hevy2Intervals.git
cd Hevy2Intervals
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your values:

| Variable | Where to find it |
|----------|-----------------|
| `HEVY_API_KEY` | [Hevy Developer Settings](https://hevy.com/settings?developer) |
| `INTERVALS_API_KEY` | [Intervals.icu Settings → Developer](https://intervals.icu/settings) |
| `INTERVALS_ATHLETE_ID` | Same page as above (e.g. `i12345`), or `0` for yourself |

### 3. Sync your workouts

```bash
# Sync your most recent workouts
python hevy_intervals_sync.py sync

# Or sync your entire Hevy history
python hevy_intervals_sync.py backfill
```

That's it! Check your Intervals.icu calendar — your workouts should be there.

## CLI Commands

| Command | Description |
|---------|-------------|
| `python hevy_intervals_sync.py sync` | Sync recent workouts (checks last 5, skips already synced) |
| `python hevy_intervals_sync.py backfill` | Sync all historical workouts |
| `python hevy_intervals_sync.py status` | Show how many workouts are synced vs pending |
| `python hevy_intervals_sync.py sync --force` | Re-sync even if already synced |
| `python hevy_intervals_sync.py backfill --force` | Re-sync entire history |

### Automate with cron

To keep things in sync automatically, add a cron job:

```bash
# Sync every 30 minutes
*/30 * * * * cd /path/to/Hevy2Intervals && python3 hevy_intervals_sync.py sync >> /tmp/hevy-sync.log 2>&1
```

## Data Mapping

| Hevy | Intervals.icu | Notes |
|------|--------------|-------|
| `workout.title` | `activity.name` | |
| `workout.start_time / end_time` | `start_date_local` + `moving_time` | Converted to your local timezone (auto-detected) |
| `workout.exercises[]` | Formatted in `description` | See example below |
| `"hevy_{workout.id}"` | `external_id` | Used for deduplication |
| Calculated total volume | `kg_lifted` | |
| Set density + tonnage | `icu_training_load` | Estimated training load (see below) |
| Average RPE from sets | `icu_rpe` | Only when you log RPE in Hevy |
| All workouts | `type: "WeightTraining"` | |
| — | `trainer: true` | Marked as indoor activity |

### Example description in Intervals.icu

```
Total volume: 8,450 kg  |  5 exercises
────────────────────────────────────────

▸ Bench Press (Barbell)  [3200 kg]
  80.0kg × 10 reps
  85.0kg × 8 reps
  90.0kg × 6 reps  RPE 9
  80.0kg × 8 reps

▸ Incline Dumbbell Press  [1920 kg]
  30.0kg × 12 reps
  32.5kg × 10 reps
  32.5kg × 10 reps
  ...
```

## Training Load Estimation

Since Hevy doesn't capture HR or power, the service estimates training load using a **tonnage + set-density heuristic** inspired by the [TrainingPeaks strength TSS method](https://www.trainingpeaks.com/blog/cycling-strength-training-tss/) and [Fast Talk Labs coaching recommendations](https://www.fasttalklabs.com/videos/the-craft-of-coaching-live-qa-how-to-calculate-tss-for-strength/):

1. **Set density** (sets per minute) estimates RPE when no RPE data is logged in Hevy
2. **Tonnage** (total kg lifted) is divided by the estimated RPE to produce a load value
3. If you log RPE on your sets in Hevy, the actual average RPE is used instead

| Session type | Typical load |
|-------------|-------------|
| Light (15 sets, ~10k kg) | 25–35 |
| Moderate (25 sets, ~20k kg) | 40–55 |
| Heavy (40 sets, ~35k kg) | 65–80 |

### Important: Intervals.icu Training Load Settings

By default, Intervals.icu sets **WeightTraining fitness contribution (CTL) to 0%**. This means your strength training load counts toward **fatigue (ATL)** but not **fitness (CTL)**. On the calendar, this appears as "Load 0 (46)" — where 46 is the actual stored load value.

To include strength load in your fitness calculation:

1. Go to [Intervals.icu Settings](https://intervals.icu/settings)
2. Scroll to **"Fitness and fatigue for different activity types"**
3. Set **WeightTraining** fitness percentage to your preference (e.g. 50% or 100%)

> **Note for endurance athletes**: Many coaches recommend keeping strength at 0% fitness / 100% fatigue, since gym work adds systemic fatigue but doesn't build sport-specific (aerobic) fitness. Adjust based on your training philosophy.

## Webhook Server (optional)

If you want real-time sync (workout appears in Intervals.icu seconds after you finish in Hevy), you can run the webhook server on a VPS.

### Running the server

```bash
python hevy_intervals_sync.py serve
# or
uvicorn hevy_intervals_sync:app --host 0.0.0.0 --port 8400
```

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check with sync stats |
| `GET` | `/sync/status` | Hevy total vs synced count |
| `POST` | `/webhook/hevy` | Hevy webhook receiver |
| `POST` | `/sync/backfill` | Trigger backfill (background) |

### Configure Hevy webhook

1. Go to [Hevy Developer Settings](https://hevy.com/settings?developer)
2. Set **Webhook URL**: `https://your-server/webhook/hevy`
3. Set **Authorization header**: `Bearer <your WEBHOOK_SECRET from .env>`

### Docker deployment

A `Dockerfile` and `docker-compose.yml` are included for easy deployment:

```bash
# Build and run locally
docker compose up -d

# Check logs
docker compose logs -f
```

For production, a `docker-compose.prod.yml` is provided that pulls a pre-built image. See `scripts/setup-vps.sh` and `scripts/deploy.sh` for a full VPS deployment flow using Docker + Caddy (auto-HTTPS).

### Makefile targets

```
make sync          Sync recent workouts (local)
make backfill      Backfill all historical workouts (local)
make status        Show sync statistics
make build         Build Docker image
make up            Start services locally (docker compose)
make down          Stop services
make logs          Tail container logs
make deploy        Deploy to VPS
make setup-vps     First-time VPS setup (requires DOMAIN=)
```

## Known Limitations

- **No structured exercise data**: Intervals.icu has no data model for individual exercises, sets, reps, or weights. All exercise detail goes into the description field as formatted text. This is a platform limitation, not a bug.
- **Training load is estimated**: Without HR/power data, load is heuristic-based. Log RPE in Hevy for better accuracy.
- **Hevy webhook format**: The exact webhook payload format isn't fully documented. The service handles multiple known patterns and falls back to fetching the workout by ID from the API.

## Contributing

Issues and PRs welcome! If you find a bug or have a feature request, open an issue on GitHub.

## License

MIT — free to use, modify, and share.
