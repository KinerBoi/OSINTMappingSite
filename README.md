# OSINT Conflict Tracker — Backend

Python FastAPI server that authenticates with ACLED, fetches live conflict events, and serves them as GeoJSON to your frontend map.

## Quick Start

### 1. Install dependencies
```bash
cd osint-backend
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
cp .env.example .env
```
Edit `.env` and fill in your ACLED login (the same email/password you use at acleddata.com):
```
ACLED_EMAIL=your_email@example.com
ACLED_PASSWORD=your_password_here
```

### 3. Run the server
```bash
python main.py
```
Server starts at `http://localhost:8000`

### 4. Test it
Open in your browser:
- `http://localhost:8000/` — Status page
- `http://localhost:8000/events` — GeoJSON conflict events
- `http://localhost:8000/conflicts` — Conflict zone summaries
- `http://localhost:8000/status` — Detailed server status

## API Endpoints

### GET /events
Returns conflict events as a GeoJSON FeatureCollection. This is what your map consumes.

Query params:
- `days` (1-30, default 7) — How many days of history
- `limit` (1-5000, default 2000) — Max events
- `country` — Filter by country name (e.g. `?country=Iran`)
- `level` — Filter by severity (`critical`, `high`, `moderate`)

### GET /conflicts
Returns conflict zones grouped by country for the sidebar Conflict Feed.

### POST /refresh
Manually trigger a data refresh.

## How It Works

1. **On startup**, the server logs into ACLED using your credentials (cookie-based auth)
2. **Every hour** (configurable), it fetches the latest conflict events
3. **Data is cached** in memory so your frontend gets instant responses
4. **CORS is enabled** so your HTML frontend can call the API from any origin

## Connecting to Your Frontend

In your map's HTML file, replace the static CONFLICTS and events data with fetch calls:

```javascript
// Fetch live conflict events for the map
const eventsResp = await fetch('http://localhost:8000/events');
const eventsGeoJSON = await eventsResp.json();
map.getSource('events').setData(eventsGeoJSON);

// Fetch conflict summaries for the sidebar feed
const conflictsResp = await fetch('http://localhost:8000/conflicts');
const { conflicts } = await conflictsResp.json();
// Use 'conflicts' array to build the sidebar feed
```