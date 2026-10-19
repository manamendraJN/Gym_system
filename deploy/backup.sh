#!/usr/bin/env bash
# Nightly backup of the database and uploaded files.  Run as the "gym" user from cron, e.g.:
#   30 2 * * *  /srv/gym/app/deploy/backup.sh >> /var/log/gym-backup.log 2>&1
# Backups contain personal and financial data: keep them on an encrypted volume and copy them OFF the server.
set -euo pipefail

APP_DIR=${APP_DIR:-/srv/gym/app}
BACKUP_DIR=${BACKUP_DIR:-/srv/gym/backups}
KEEP_DAYS=${KEEP_DAYS:-14}
STAMP=$(date +%Y%m%d-%H%M%S)

set -a; source /srv/gym/.env; set +a
mkdir -p "$BACKUP_DIR"
umask 077

if [ "${DB_ENGINE:-sqlite}" = "postgresql" ]; then
  PGPASSWORD="$DB_PASSWORD" pg_dump -h "${DB_HOST:-127.0.0.1}" -p "${DB_PORT:-5432}" -U "$DB_USER" -Fc "$DB_NAME" > "$BACKUP_DIR/db-$STAMP.dump"
else
  sqlite3 "${SQLITE_PATH:-$APP_DIR/db.sqlite3}" ".backup '$BACKUP_DIR/db-$STAMP.sqlite3'"
fi

# Uploaded files: public media and private documents (certificates, expense receipts)
tar -czf "$BACKUP_DIR/files-$STAMP.tar.gz" -C "$APP_DIR" media private_media 2>/dev/null || true

# Remove backups older than KEEP_DAYS
find "$BACKUP_DIR" -type f \( -name 'db-*' -o -name 'files-*' \) -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) backup complete: $STAMP"

# Restore (PostgreSQL):   pg_restore --clean --if-exists -h HOST -U USER -d gym_system db-<stamp>.dump
# Restore (files):        tar -xzf files-<stamp>.tar.gz -C /srv/gym/app
# After restoring:        python manage.py migrate && python manage.py check
