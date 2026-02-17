# PropScan

Satellite imagery analysis tool for property feature detection across Australian suburbs.

## Deploy to Render

1. Push to GitHub
2. Render → New Web Service → connect repo
3. Runtime: **Python 3** | Region: **Singapore**
4. Build: `pip install -r requirements.txt`
5. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Plan: **Free**

## Local Development

```
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://localhost:8000

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/regions` | List regions |
| POST | `/api/scan` | Scan region |
| POST | `/api/purchase` | Unlock report |
| GET | `/api/refs` | Reference data |
| GET | `/api/export` | CSV export |
| GET | `/api/stats` | Statistics |

## Stack

- FastAPI + Uvicorn
- Leaflet.js + ESRI imagery
- SQLite
