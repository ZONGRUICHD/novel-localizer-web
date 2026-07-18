# OpenAPI contract

`openapi.json` is generated deterministically from FastAPI by `backend/scripts/export_openapi.py`. The frontend runs `pnpm generate:api` to create `src/api/schema.d.ts`; CI fails when either artifact drifts from the implementation.
