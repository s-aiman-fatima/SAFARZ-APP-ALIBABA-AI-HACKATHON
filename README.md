# Safarz

Safarz is a Karachi bus finder with a Groq-powered chat assistant. The assistant answers general questions and recommends buses using the route records in `backend/data/extracted_routes.json`.

## Run It

Requirements: Node.js 18 or newer.

```powershell
npm install
npm start
```

Open http://localhost:5000.

The Node server serves the web app and these APIs:

- `GET /api/health` - server and route-data status
- `GET /api/stops?q=Saddar` - stop search
- `GET /api/routes?from=Gulshan%20Chowrangi&to=Saddar` - bus recommendations
- `POST /api/chat` - general chat and bus questions

## Groq Setup

Create a new Groq API key, then copy the example environment file:

```powershell
Copy-Item .env.example .env
```

Set the key in `.env`:

```text
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-20b
```

Restart `npm start` after changing `.env`. Never commit `.env` or put a key in frontend code.

With a key, Groq answers general knowledge, math, coding, writing, casual, travel, and bus questions. Bus facts are supplied from the imported route data so the model cannot invent routes, stops, or fares. Without a key, bus questions still use the local grounded fallback.

## Example Questions

- `What is photosynthesis?`
- `Solve 25 times 4.`
- `What route does Masood take?`
- `Which bus goes from Gulshan Chowrangi to Saddar?`
- `Gulshan se Saddar kaunsi bus jati hai?`

## Flask Backend

The Flask service is an alternative SQL-backed implementation. Use it when you need the SQL database or Python API:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
python -m backend.app
```

It runs on `http://localhost:5000` by default. Do not run Flask and Node on the same port.

## Tests

```powershell
npm run check
python -m unittest backend.test_app -v
```

## Data and Security

Route data is stored in `backend/data/extracted_routes.json`. The guide data does not provide reliable schedules or fares, so answers label estimates and avoid inventing missing facts. Keep API keys in environment variables and rotate any key that has been exposed.
