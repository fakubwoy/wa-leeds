# WA Lead Finder — instagrapi edition

Zero Apify cost. Uses Instagram's private API directly via `instagrapi`.

## Project structure

```
main.py             ← Flask app (drop-in replacement for Apify version)
requirements.txt
Procfile            ← for Railway / gunicorn
railway.toml
templates/
  index.html        ← frontend (unchanged from original)
static/             ← any static assets
.env.example        ← copy to .env for local dev
```

## Local setup

```bash
pip install -r requirements.txt

cp .env.example .env
# fill in IG_USERNAME, IG_PASSWORD, GEMINI_API_KEY

python main.py
# → http://localhost:5000
```

## Deploy to Railway

1. Push this repo to GitHub
2. New project → Deploy from GitHub repo
3. Add environment variables in Railway dashboard:
   - `IG_USERNAME`
   - `IG_PASSWORD`
   - `GEMINI_API_KEY`
4. Railway auto-detects `Procfile` and deploys

## Important notes

### Rate limits
- `instagrapi` adds 0.8–1.8s delay between profile fetches automatically
- Don't run more than 1 scan at a time
- Hashtag scraping fetches ~30–80 posts per tag by default
- For 50 leads target: expect ~5–10 min run time

### Session persistence
On first run the app logs in and saves `ig_session.json`.
On Railway, the filesystem resets each deploy — the app just re-logs in automatically.
If you get login challenges, delete `ig_session.json` and restart.

### If Instagram blocks the account
- Use a throwaway account (not your main)
- Don't run scans too frequently (max 2–3/day)
- If challenged, the app logs the error — create a fresh throwaway

### Gemini cost
Still ~$0.00 per run (2.5 Flash is essentially free at this volume).
The only cost is Railway dyno time (~$5/month on hobby plan).
