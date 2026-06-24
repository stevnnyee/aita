# AITA Video Pipeline

Turns top r/AmItheAsshole posts into vertical short-form videos (TikTok/YouTube
Shorts) and uploads them to YouTube — automatically, twice a day.

**Flow:** fetch Reddit post → GPT-4o script → ElevenLabs voiceover → captions →
FFmpeg video → YouTube upload, with a copy dropped in `output/ready/` for manual
TikTok posting.

## Prerequisites

- Python 3.10+ (3.9 works but is past EOL)
- **FFmpeg** on your PATH (`ffmpeg` and `ffprobe`): `brew install ffmpeg`
- API keys: Reddit, OpenAI, ElevenLabs, and Google/YouTube OAuth

## Setup

```bash
# 1. Virtual env + dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt        # includes runtime deps + pytest

# 2. Configure
cp .env.example .env                        # then fill in your keys

# 3. Add at least one file to each:
#    assets/backgrounds/   (e.g. gameplay .mp4)
#    assets/music/         (e.g. background .mp3)

# 4. One-time YouTube OAuth (opens a browser, saves credentials/youtube_token.json)
python -m app.services.youtube_uploader --auth
```

Required keys live in `.env` (see `.env.example` for the full list): `REDDIT_*`,
`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, plus the YouTube
OAuth client JSON at `credentials/client_secrets.json`.

## Run the app

```bash
# Start the API + scheduler (auto-runs the pipeline at 9:00 and 18:00 ET).
# Use ONE worker — the scheduler is per-process.
uvicorn app.main:app --host 0.0.0.0 --port 8000

curl http://localhost:8000/health
curl -X POST http://localhost:8000/pipeline/run     # trigger a run now
curl http://localhost:8000/posts                    # list processed posts
curl http://localhost:8000/posts/<reddit_id>        # one post + youtube_url
```

| Endpoint              | Method | Purpose                          |
|-----------------------|--------|----------------------------------|
| `/health`             | GET    | Health check                     |
| `/pipeline/run`       | POST   | Trigger a pipeline run           |
| `/posts`              | GET    | List processed posts (`skip`, `limit`) |
| `/posts/{reddit_id}`  | GET    | Single post + YouTube URL        |

## Run the pipeline directly (no server)

```bash
python -m app.pipeline                  # full run: generate + upload + record
python -m app.pipeline --skip-upload    # dev: build video only, no upload, no DB row
```

Output for each run lands in `output/<reddit_id>/` (`voiceover.mp3`,
`alignment.json`, `captions.ass`, `final.mp4`) and a posting-ready copy in
`output/ready/<reddit_id>.mp4`.

## Run individual stages (for testing)

```bash
python -m app.services.reddit_scraper                       # list eligible posts
python -m app.services.script_generator --sample            # GPT-4o script from a sample post
python -m app.services.voiceover --sample                   # script -> voiceover
python -m app.services.captions --sample                    # full chain -> captions
python -m app.services.video_assembler --reddit-id sample   # assemble final.mp4
python -m app.services.youtube_uploader --upload output/sample/final.mp4 --title "AITA test"
```

## Maintenance

A post is recorded once claimed, so failed/interrupted runs can leave rows that
block re-processing. Clean them up manually (not automatic — retrying re-spends
API credits):

```bash
python -m app.maintenance --reset-failed     # delete 'upload_failed' rows so posts retry
python -m app.maintenance --list-stuck       # review orphaned 'uploading' rows (check YouTube first)
```

`uploading` rows are reported, never auto-deleted: the video may already be live,
so deleting the row could cause a duplicate upload. Inspect on YouTube, then
delete the row to retry or set its `status` to `completed`.

## Tests

```bash
pytest          # 145 tests, ~0.5s, no network/API calls
```

## Project layout

```
app/
  config.py            Settings (.env), directory + secret validation
  database.py          SQLAlchemy engine/session
  models.py            UsedPost table
  schemas.py           API response models
  pipeline.py          Orchestrator (run_pipeline)
  scheduler.py         APScheduler cron (9:00 / 18:00)
  main.py              FastAPI app + lifespan
  maintenance.py       Cleanup/retry helpers
  services/            reddit_scraper, script_generator, voiceover, captions,
                       video_assembler, youtube_uploader
  utils/               logging, ffmpeg helpers
tests/                 pytest suite
assets/                backgrounds/ + music/  (you supply these)
output/                generated artifacts + ready/ posting queue
credentials/           YouTube OAuth client secrets + token
```
