# PropScan v4

Dynamic location search + radius-based scanning + customer auth + admin dashboard.

## API Endpoints

### Public
- `GET /api/suggest?q=manly` — Autocomplete location search
- `GET /api/geocode?q=2095` — Geocode location to lat/lng
- `GET /api/featured` — Featured regions for quick-select
- `GET /api/radius-options` — Available scan radii
- `POST /api/scan` — Scan any location `{lat, lng, suburb, state, postcode, radius_km}`
- `POST /api/redeem` — Redeem coupon code `{region_key, coupon}`
- `GET /api/export?region_key=...` — CSV download

### Auth
- `POST /api/auth/register` — `{email, password, name, company}`
- `POST /api/auth/login` — `{email, password}`
- `GET /api/auth/me` — Current user
- `POST /api/auth/logout`

### Admin (`/admin`)
- Password-protected dashboard
- Trigger satellite scans for any location
- Manage coupons, view members, upload DB

## Deploy
Push to GitHub → Render auto-deploys.
Set `ADMIN_PASS` env var in Render dashboard.
