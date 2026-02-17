# PropScan v3

Property feature analysis with integrated satellite scanning.

## Deploy

1. Push to GitHub
2. Render → Web Service → connect repo
3. Runtime: Python 3 | Region: Singapore
4. Build: `pip install -r requirements.txt`
5. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Add env var: `ADMIN_PASS` = your password

## Admin

Visit `/admin` — trigger satellite scans, manage coupons, upload data.

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/regions | List regions |
| POST | /api/scan | Scan region (serves real or generated data) |
| POST | /api/redeem | Redeem coupon code |
| GET | /api/export | CSV export |
