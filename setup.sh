#!/usr/bin/env bash
# ============================================================
#  Health Coach — one-time setup
#  Usage:
#    1. Put this script + the 5 project files in an empty folder
#    2. bash setup.sh
# ============================================================
set -euo pipefail

echo ""
echo "🏋️  Unified AI Health & Fitness Coach — setup"
echo "─────────────────────────────────────────────"

# --- 1. Verify the seed files are present -------------------
missing=0
for f in CLAUDE.md TASKS.md .env.example .gitignore canonical_schema_v0.1.sql; do
  if [[ ! -f "$f" ]]; then
    echo "  ❌ missing: $f"
    missing=1
  fi
done
if [[ $missing -eq 1 ]]; then
  echo ""
  echo "Put all seed files in this folder first, then re-run."
  exit 1
fi
echo "  ✅ all seed files present"

# --- 2. Folder structure ------------------------------------
mkdir -p schema/migrations docs/adr src/coach/{adapters,store,normalize,compute,coach,cli} tests/fixtures data
[[ -f canonical_schema_v0.1.sql ]] && mv -n canonical_schema_v0.1.sql schema/ 2>/dev/null || true
echo "  ✅ folder structure created"

# --- 3. Working files Claude Code will append to -------------
[[ -f DECISIONS_NEEDED.md ]] || cat > DECISIONS_NEEDED.md <<'EOF'
# Decisions Needed

> Claude Code appends here when it hits a **one-way door** it shouldn't decide
> alone. Each entry: what's blocked, the options, its recommendation, why it
> matters. Answer these first thing — they gate real work.

_(empty — nothing blocked yet)_
EOF

[[ -f SESSION_LOG.md ]] || cat > SESSION_LOG.md <<'EOF'
# Session Log

> Updated **continuously** during work (not only at the end — a session can be
> cut off by a usage limit before it gets to write a wrap-up).

_(no sessions yet)_
EOF
echo "  ✅ DECISIONS_NEEDED.md + SESSION_LOG.md created"

# --- 4. .env ------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "  ⚠️  .env created from template — FILL IN YOUR CREDENTIALS"
else
  echo "  ✅ .env already exists (left untouched)"
fi

# --- 5. Git -------------------------------------------------
if [[ ! -d .git ]]; then
  git init -q
  echo "  ✅ git initialized"
else
  echo "  ✅ git already initialized"
fi

# --- 6. Safety check: is .env actually ignored? --------------
if git check-ignore -q .env; then
  echo "  ✅ .env is gitignored (secrets safe)"
else
  echo "  ❌ WARNING: .env is NOT ignored. Fix .gitignore before committing!"
  exit 1
fi

# --- 7. Python sanity ---------------------------------------
if command -v python3 >/dev/null 2>&1; then
  echo "  ✅ $(python3 --version)"
else
  echo "  ⚠️  python3 not found — install Python 3.14 before the run"
fi

echo ""
echo "─────────────────────────────────────────────"
echo "Setup complete. Next:"
echo "  1. Fill in WHOOP_CLIENT_ID + WHOOP_CLIENT_SECRET in .env"
echo "     (developer.whoop.com → create app → redirect http://localhost:8080/callback)"
echo "  2. Open Claude Code in this folder"
echo "  3. Paste the kickoff prompt"
echo ""
