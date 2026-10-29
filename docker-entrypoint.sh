#!/bin/sh
# Migrate, then run ONE app process: the scheduler and its lock live in the
# process, so never run several workers or replicas for one customer.
set -e
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*' --workers 1
