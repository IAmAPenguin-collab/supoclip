#!/bin/sh
# Bring the database up to the app's schema before the frontend starts, so a
# `git pull` with new migrations never leaves sign-up or other pages broken.
#
# New installs get their tables from init.sql, which Prisma has no record of.
# The first run on such a database records the migrations that exist today as
# applied (a baseline); after that `prisma migrate deploy` applies only new ones.
#
# AUTO_MIGRATE=false skips all of this.

cd /app || exit 0

if [ "${AUTO_MIGRATE:-true}" != "true" ]; then
  echo "[migrate] AUTO_MIGRATE=false; not touching the database"
  exit 0
fi

# `prisma db execute` prints no rows, but it fails on an error. Dividing by the
# table count turns "table missing" into a division-by-zero error.
table_exists() {
  echo "SELECT 1 / (SELECT count(*)::int FROM pg_tables WHERE schemaname = 'public' AND tablename = '$1');" \
    | pnpm exec prisma db execute --stdin --schema prisma/schema.prisma >/dev/null 2>&1
}

attempt=0
until echo "SELECT 1;" | pnpm exec prisma db execute --stdin --schema prisma/schema.prisma >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "[migrate] database not reachable after 60s; starting without migrating"
    exit 0
  fi
  sleep 2
done

if ! table_exists _prisma_migrations && table_exists users; then
  echo "[migrate] database was created from init.sql; recording existing migrations as applied"
  for dir in prisma/migrations/*/; do
    name=$(basename "$dir")
    pnpm exec prisma migrate resolve --applied "$name" --schema prisma/schema.prisma >/dev/null 2>&1 \
      && echo "[migrate]   baseline: $name" \
      || echo "[migrate]   could not record $name"
  done
fi

if pnpm exec prisma migrate deploy --schema prisma/schema.prisma; then
  echo "[migrate] database is up to date"
else
  # Keep the site up so the error is visible in the logs and the app.
  echo "[migrate] WARNING: prisma migrate deploy failed; see the error above"
fi
exit 0
