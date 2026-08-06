#!/usr/bin/env bash
# Pull the host's newest backup down to the laptop archive (ADR-0019 §5).
#
# Runs ON THE LAPTOP, not on the host. The direction matters: the laptop reaches
# out and takes a copy, so the host never holds a credential that can write to
# the archive. A host that gets compromised cannot then destroy the backups.
#
#   Usage:  deploy/pull-backup.sh coach@coach.example.com ~/archive/fitness-ai
#
# Suggested laptop cron (08:00 daily, after the host's 02:30 backup):
#   0 8 * * * /path/to/repo/deploy/pull-backup.sh coach@host ~/archive/fitness-ai

set -euo pipefail

HOST="${1:?usage: pull-backup.sh <user@host> <local-archive-dir>}"
ARCHIVE="${2:?usage: pull-backup.sh <user@host> <local-archive-dir>}"
REMOTE_DIR="${REMOTE_DIR:-fitness-ai/data/backups}"

mkdir -p "$ARCHIVE"

# Newest snapshot on the host, by mtime.
newest="$(ssh "$HOST" "ls -t ${REMOTE_DIR}/*.db 2>/dev/null | head -1")"
if [[ -z "$newest" ]]; then
  echo "no backups found on ${HOST}:${REMOTE_DIR}" >&2
  exit 1
fi

name="$(basename "$newest")"
dest="${ARCHIVE}/${name}"

if [[ -e "$dest" ]]; then
  echo "already have ${name} — nothing to do"
  exit 0
fi

# Download to a .part and rename only on success, so an interrupted transfer
# never leaves a truncated file sitting in the archive looking like a backup.
scp -q "${HOST}:${newest}" "${dest}.part"
mv "${dest}.part" "$dest"
echo "pulled ${name} ($(du -h "$dest" | cut -f1))"

# Prove it, immediately, on the copy that is actually being kept. Verifying the
# snapshot on the host would test the host's disk; the archive copy is the one
# that has to survive, and the transfer is a thing that can corrupt it.
if command -v coach >/dev/null 2>&1; then
  coach db rehearse-restore "$dest"
else
  echo "note: 'coach' not on PATH — run 'coach db rehearse-restore ${dest}' to verify" >&2
fi
