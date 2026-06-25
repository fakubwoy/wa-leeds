# WA Lead Finder — CRM Edition

Instagram lead finder for WhatsApp businesses, with built-in CRM and outreach tracking.

## Features

- **12 pre-configured business niches** (sweets, fish export, travel agents, etc.)
- **Scan tab** — find Instagram accounts by hashtag/keyword, auto-scored into tiers
- **CRM tab** — all hot leads auto-saved; track outreach status per lead
- **Stats tab** — pipeline overview (contacted, responded, response rate, closed deals)
- Gemini 2.5 Flash AI validation on hot leads

## Project structure

```
main.py             ← Flask app (scan + CRM API)
templates/
  index.html        ← 3-tab frontend (Scan / CRM / Stats)
requirements.txt
Procfile
railway.toml
.env.example
crm.db              ← SQLite CRM (auto-created on first run, persists locally)
```

## Local setup

```bash
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# fill in GEMINI_API_KEY

python main.py
# → http://localhost:5000
```

## Deploy to Railway

1. Push this repo to GitHub
2. New project → Deploy from GitHub repo
3. Add environment variables:
   - `GEMINI_API_KEY`
   - `DB_PATH=/data/crm.db`  ← use Railway volume for persistence
4. Add a Railway Volume mounted at `/data`

## Outreach workflow

1. **Scan** → pick a category → Start Scan
2. Hot leads (Tier 1, AI-validated) are **auto-saved** to the CRM
3. Go to **CRM tab** → open a lead → click **Update**
4. Set status: Contacted → Followed Up → Responded → Closed
5. Click the 💬 WA button to open WhatsApp directly
6. Use **Stats tab** to monitor conversion funnel

## Business categories built-in

1. Sweets & Pickles
2. Aquaculture & Sea Fish Export
3. Travel Agents
4. Beauty / Hair / Body Care
5. Cakes & Dessert Bakers
6. Personalised Gift Shops
7. Event Planners & Decorators
8. Home Interior & Furniture
9. Homemade Dairy Products
10. Homemade Cosmetics & Soaps
11. Therapists & Online Doctors
12. Gym Trainers, MUA & Dieticians

## Notes

- On Railway the filesystem resets on redeploy — use a Volume for `DB_PATH`
- Don't run more than 1 scan at a time; Instagram rate-limits aggressively
- Use a throwaway Instagram account (the scraper doesn't log in, but IPs can be flagged)
- Max 2–3 scans/day per IP