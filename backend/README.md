# Safarz Flask API

This directory contains the optional Flask and SQLAlchemy backend. The recommended local app is the Node server in the repository root because it serves the frontend and chat API together.

## Start Flask

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
python -m backend.app
```

The API starts at `http://localhost:5000`. Set `PORT` or `DATABASE_URL` in `.env` when needed. The default database is `backend/data/safarz.db`.

## API

- `GET /api/health`
- `GET /api/stops?q=Saddar`
- `GET /api/routes?from=Gulshan%20Chowrangi&to=Saddar`
- `POST /api/chat` with `{ "message": "What route does Masood take?" }`

The chat assistant supports general questions through Groq. Bus questions are grounded in the seeded route records and can match stop names, route codes, and bus names. If Groq is unavailable, bus questions use a deterministic local answer.

## Environment

Copy `.env.example` to `.env` and set `GROQ_API_KEY` for AI answers. Keep `.env` private. Set `GEOCODING_ENABLED=0` to disable optional OpenStreetMap and OSRM lookups.

## Tests

```powershell
python -m unittest backend.test_app -v
```
