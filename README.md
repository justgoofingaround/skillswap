# SkillSwap

A campus skill-exchange marketplace. Teach someone for an hour, earn a credit, spend it learning anything from anyone. Swapping is free for students; universities and companies pay for private communities.

## Features

| Feature | What it does |
|---|---|
| **AI need matcher** | A student describes a problem in their own words ("My spreadsheet has thousands of rows and I need a weekly summary by category before Friday"). An LLM (Groq `openai/gpt-oss-120b`, or Claude if configured) works out the skill (pivot tables, covered by Excel), the session length and the deadline. The server then ranks real peers who teach it, checks their open time slots before the deadline, and shows **why each match came up**. The student books a slot right there. |
| Skill profiles | What I can teach, what I want to learn, weekly availability (morning / afternoon / evening, per weekday). |
| Marketplace | Search and skill filters. Ranks people who teach what you want. Tags direct swaps. |
| 3-way swap finder | Finds loops (A teaches B, B teaches C, C teaches A) where no one-to-one trade exists. |
| Credit wallet | 2 welcome credits. 1 hour taught = 1 credit. Credits are held when you book, returned if declined or cancelled. Balance is computed from session history, never stored. |
| Booking | Request (with time slot) → teacher accepts or declines → learner marks done, rates and reviews → teacher is paid. |
| Trust rules (server-side) | Only the learner can confirm a session. No self-booking. A teacher earns at most 3 credits per week from the same learner. A time slot can't be double-booked. Free accounts have at most 3 active bookings. |
| Campus dashboard | Members, completed swaps, hours taught, % of swaps across majors, average rating, most taught, most wanted, and skill gaps (wanted but not offered). This is what a university buys. |
| Plans | Student $0, Plus $6.99/mo, University $6,000/yr, Company $3/employee/mo, credit packs. |
| Accounts | Join with name and skills, no password. A login code (Profile page) signs you in on another device. Share button shows a QR code to the site. |

### How the AI matcher works

1. `POST /api/match` with the student's words.
2. The LLM gets the words, today's date and the list of skills peers actually offer. It returns the specific skills, the matching catalog skill, keywords, a session title, suggested hours and a deadline date ("before Friday" becomes Thursday).
3. The server scores every peer, and each point comes with a human-readable reason:
   - teaches the catalog skill (strongest) or a related keyword,
   - has an open slot before the deadline (or a warning if only after),
   - rating and sessions taught,
   - wants something the student teaches (a future swap back).
4. The top 3 come back with reasons and open slots; each slot is a one-tap booking.

Provider order: Claude (`claude-opus-5-5`, low effort, JSON-schema output, server-side refusal fallbacks) if `ANTHROPIC_API_KEY` is set; otherwise Groq (`GROQ_MODEL`, default `openai/gpt-oss-120b`, JSON mode, low reasoning effort) if `GROQ_API_KEY` is set; otherwise keyword matching, labeled "Keyword match (AI is offline)" in the UI. Any AI error also falls back to keywords. Time slots that already ended today are never offered.

## Project layout

```
public/index.html   the whole web app (no build step)
public/app.css      styles
api/index.py        the API, a Vercel Python function (also used locally)
run_local.py        local dev server: python3 run_local.py -> http://localhost:8787
vercel.json         routes /api/* to api/index.py
requirements.txt    psycopg (Postgres), anthropic (Claude)
```

Database: Postgres when `DATABASE_URL` or `POSTGRES_URL` is set, otherwise a local SQLite file. Tables are created and migrated automatically on first request.

## Deploying on Vercel

1. Import this repo at https://vercel.com/new. Framework preset: **Other**.
2. Project → **Storage** → Create Database → **Neon (Postgres)** → Connect. This sets `DATABASE_URL`.
3. Project → **Settings → Environment Variables** → add `GROQ_API_KEY` (or `ANTHROPIC_API_KEY`).
4. **Redeploy** so the variables take effect.

## Demo script (3 minutes)

1. Teammates join on their phones (Share → QR). Each lists skills and availability. Set one up to teach **Excel** with an evening free before Friday.
2. On the Market page, tap **Try** in "Tell us what you need help with" and submit.
3. Show the judges: Claude names **pivot tables** without the student knowing the term, the deadline, and **why this match** for each peer.
4. Tap a time slot → **Send request**. Teacher accepts on their phone. Learner marks done and rates. Show the wallet move.
5. Open **Campus**: completed swaps, hours, cross-major %, skill gaps.

## Test status

Verified locally against SQLite: join, login code, profile with availability, booking with slots, double-booking and out-of-availability refusals, credit hold and transfer, teacher-can't-confirm rule, weekly cap logic, campus stats, and the keyword fallback for the matcher.

Verified live: the Groq call (gpt-oss-120b) mapped the spreadsheet prompt to pivot tables → Excel with the right deadline in under a second.

**Not yet verified:** the Postgres path and the live Claude call. Neither the Postgres driver nor the Anthropic SDK could be installed on the build machine; both are installed by Vercel at deploy time. The first deploy with `DATABASE_URL` and `ANTHROPIC_API_KEY` set is the real test of those paths.
