#!/usr/bin/env bash
#
# openshelf DB 每日備份（BR-160000 的配套措施）
#
# 背景：SQLite DB 已從 NAS(NFS) 搬到 repo 內 data/db/（本地 ext4），
# 因此不再落在 NAS 的備份範圍內。本 script 把它備份回 NAS。
#
# 為什麼用 `sqlite3 .backup` 而不是 cp：
#   cp 會在寫入進行到一半時抓到不一致的快照（尤其 WAL 模式下 -wal 尚未 checkpoint）。
#   `.backup` 走 SQLite 的 Online Backup API，產出的是交易一致的快照，
#   且不需要停止服務。
#
# 判準：本 script 的每一步都必須讓「失敗」與「什麼都沒做」產生不同的輸出。
#   - 缺席態與失敗態不得共用同一個輸出
#   - 不用管線取 $?（會取到管線末端的退出碼）
#   - 備份完成後驗證產物真的可讀（integrity_check + 資料筆數），
#     否則「備份檔存在」會被誤當成「備份可還原」

set -uo pipefail

SRC="${OPENSHELF_DB:-/home/pkcs12/projects/openshelf/data/db/openshelf.sqlite}"
JOBS_SRC="$(dirname "$SRC")/download_jobs.json"
DEST_DIR="${OPENSHELF_BACKUP_DIR:-/nas/openshelf/db-backup}"
KEEP_DAYS="${OPENSHELF_BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$DEST_DIR/openshelf-$STAMP.sqlite"
JOBS_DEST="$DEST_DIR/download_jobs-$STAMP.json"

log() { printf '%s [backup-db] %s\n' "$(date -Is)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

# --- 前置檢查：每一項都必須能區分「不存在」與「檢查沒跑」 ---
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 not on PATH"
[ -r "$SRC" ] || die "source DB not readable: $SRC"

mkdir -p "$DEST_DIR" || die "cannot create dest dir: $DEST_DIR"
[ -w "$DEST_DIR" ] || die "dest dir not writable (NAS down?): $DEST_DIR"

# --- 備份本體 ---
log "backing up $SRC -> $DEST"
sqlite3 "$SRC" ".backup '$DEST'"
BACKUP_RC=$?
[ "$BACKUP_RC" -eq 0 ] || die "sqlite3 .backup failed rc=$BACKUP_RC"
[ -s "$DEST" ] || die "backup file is empty or missing: $DEST"

# --- 驗證產物真的可還原（不只是「檔案存在」）---
INTEG="$(sqlite3 "$DEST" 'PRAGMA integrity_check;')"
INTEG_RC=$?
[ "$INTEG_RC" -eq 0 ] || die "integrity_check could not run rc=$INTEG_RC"
[ "$INTEG" = "ok" ] || die "integrity_check failed: $INTEG"

ROWS="$(sqlite3 "$DEST" 'SELECT COUNT(*) FROM work;')"
ROWS_RC=$?
[ "$ROWS_RC" -eq 0 ] || die "row count could not run rc=$ROWS_RC"
# 0 rows 是可疑的（空殼），但不是必然錯誤 —— 出聲而不中止
[ "$ROWS" -gt 0 ] || log "WARNING: backup has 0 rows in work table — verify this is expected"

SRC_ROWS="$(sqlite3 "$SRC" 'SELECT COUNT(*) FROM work;')"
[ "$ROWS" = "$SRC_ROWS" ] || log "WARNING: row count drift src=$SRC_ROWS backup=$ROWS (writes during backup are normal)"

# --- 下載佇列狀態（與 DB 同目錄，遺失會失去佇列）---
if [ -r "$JOBS_SRC" ]; then
  cp -p "$JOBS_SRC" "$JOBS_DEST"
  JOBS_RC=$?
  [ "$JOBS_RC" -eq 0 ] || log "WARNING: download_jobs.json copy failed rc=$JOBS_RC"
else
  log "note: $JOBS_SRC not present, skipping"
fi

log "OK size=$(stat -c %s "$DEST") integrity=$INTEG rows=$ROWS"

# --- 保留期輪替 ---
DELETED="$(find "$DEST_DIR" -maxdepth 1 -name 'openshelf-*.sqlite' -mtime "+$KEEP_DAYS" -print -delete | wc -l)"
find "$DEST_DIR" -maxdepth 1 -name 'download_jobs-*.json' -mtime "+$KEEP_DAYS" -delete
log "rotation: removed $DELETED backup(s) older than $KEEP_DAYS days"

exit 0
