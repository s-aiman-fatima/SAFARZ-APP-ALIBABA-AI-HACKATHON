# Safarz Data

- `raw/` contains the supplied route-guide JPEG pages.
- `extracted_routes.json` is the reviewed transcription used to seed SQL.
- `safarz.db` is generated locally and should not be committed.

To rebuild the local database from the extracted JSON:

```powershell
$env:RESET_DATABASE="1"
python -m backend.app
```

The process stays running because it is the Flask web server. Stop it with `Ctrl+C`; a terminal timeout or forced stop can display exit code `1` even when startup succeeded.