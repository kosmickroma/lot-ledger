# LotLedger

Dallas off-market property analysis tool. Draw an area on the map, get every parcel inside it color-coded by type, cross-referenced against DCAD and active Redfin listings. Export to CSV for Excel.

## Stack
- **Backend**: FastAPI (Python)
- **Database**: Supabase (PostgreSQL + PostGIS)
- **Frontend**: Leaflet.js + Leaflet.draw
- **Hosting**: Render

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Environment
```bash
cp .env.example .env
# Fill in your Supabase URL and key before Phase 2+
```

### 3. Run locally
```bash
uvicorn api.main:app --reload
```

Open `http://localhost:8000`

### 4. Health check
```bash
curl http://localhost:8000/health
```

## Deployment
Configured for Render web service deployment via `render.yaml`.
