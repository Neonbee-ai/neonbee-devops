#!/usr/bin/env bash
#
# Internal service-key drift audit.
#
# THE PROBLEM THIS EXISTS FOR
# ---------------------------
# Services authenticate to each other with shared `*_INTERNAL_KEY` secrets. On
# GitHub Free — which this org is on — organisation secrets are NOT available to
# private repositories, and every repo here is private. So each key has to be
# set on EVERY repo that reads it, individually.
#
# Nothing enforces that. The moment a service becomes a new consumer of another
# service's internal API, its repo needs the secret added by hand. Miss it and
# the caller gets 401s — or, on the fail-soft paths that are the norm here,
# absolutely nothing: the call is skipped and the feature is silently inert.
# Real examples found by this script on first run: support-be reading
# PROJECTS_INTERNAL_KEY and flow-be reading CRM_SYNC_INTERNAL_KEY, both dead for
# an unknown length of time, neither producing a single error.
#
# WHAT IT DOES
#   1. Greps every service's source for `*_INTERNAL_KEY` / `*_INTERNAL_SERVICE_KEY`
#      environment reads — the code is the source of truth for who needs what.
#      Keys used only as a `a || b` FALLBACK are excluded: their absence is the
#      intended state, and reporting them would train people to ignore this.
#   2. Asks GitHub which repos actually hold each secret.
#   3. Reports every repo that reads a key but does not hold it.
#
#   --fix   additionally copies the canonical value from the production VM to
#           the repos missing it. Propagation, never rotation: the existing
#           value is preserved, so there is no window where a rotated sender
#           talks to an un-rotated receiver.
#
# EXIT CODES:  0 = no drift   1 = drift found   2 = could not complete
#
# Usage:
#   scripts/audit-internal-service-keys.sh              # report only
#   scripts/audit-internal-service-keys.sh --fix        # report + propagate
#
set -uo pipefail

ORG="Neonbee-ai"
PROD_VM="root@68.183.86.248"
PROD_APP_ROOT="/github/neonbee"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/neonbee_ci_deploy}"
# Lives in neonbee-devops (the shared ops repo) but audits the sibling service
# checkouts, so default to the workspace that contains them. Override with
# WORKSPACE=/path/to/SO360 V.2 if this repo is cloned elsewhere.
ROOT="${WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

FIX=0
[[ "${1:-}" == "--fix" ]] && FIX=1

command -v gh >/dev/null || { echo "gh CLI not found"; exit 2; }
gh auth status >/dev/null 2>&1 || { echo "gh not authenticated — run: gh auth login"; exit 2; }

echo "Internal service-key drift audit — org=$ORG"
[[ $FIX -eq 1 ]] && echo "MODE: --fix (propagate existing values; never rotates)" \
                 || echo "MODE: report only"
echo

# --- 1. Which repo reads which key, according to the code -------------------
# Deliberately excludes tests and build output: a spec that mentions a key does
# not mean the running service needs it.
declare -A NEEDS   # "repo|KEY" -> 1
mapfile -t SERVICE_DIRS < <(find "$ROOT" -maxdepth 3 -type d -name "src" \
  -path "*/so360-*" -not -path "*/node_modules/*" -not -path "*/dist/*" 2>/dev/null)

for src in "${SERVICE_DIRS[@]}"; do
  repo="$(basename "$(dirname "$src")")"
  # Only repos that actually exist on the org can hold secrets.
  # A key that only ever appears on the RIGHT of a `||` is a documented
  # FALLBACK — its absence is the intended state, not drift. Collect those
  # first and subtract them, otherwise the report cries wolf and gets ignored,
  # which is worse than no report at all.
  fallbacks="$(grep -rhoE "process\.env\.[A-Z0-9_]+ *\|\| *process\.env\.[A-Z0-9_]*INTERNAL(_SERVICE)?_KEY[A-Z0-9_]*" "$src" \
                 --include="*.ts" --include="*.js" 2>/dev/null \
               | sed -E 's/.*\|\| *process\.env\.//' | sort -u)"
  while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    grep -qx "$key" <<<"$fallbacks" && continue
    NEEDS["$repo|$key"]=1
  done < <(grep -rhoE "process\.env\.[A-Z0-9_]*INTERNAL(_SERVICE)?_KEY[A-Z0-9_]*" "$src" \
             --include="*.ts" --include="*.js" 2>/dev/null \
           | grep -v "\.spec\." \
           | sed 's/process\.env\.//' | sort -u)
done

if [[ ${#NEEDS[@]} -eq 0 ]]; then
  echo "No internal-key reads found — nothing to audit."
  exit 0
fi

# --- 2. What each repo actually holds ---------------------------------------
declare -A HAS
declare -A REPO_SEEN
declare -A REPO_EXISTS
for entry in "${!NEEDS[@]}"; do
  repo="${entry%%|*}"
  [[ -n "${REPO_SEEN[$repo]:-}" ]] && continue
  REPO_SEEN[$repo]=1
  # Some directories (e.g. a module's parent folder) carry a stray src/ but are
  # not repos of their own. Asking GitHub is the only reliable discriminator.
  if ! gh repo view "$ORG/$repo" --json name >/dev/null 2>&1; then
    REPO_EXISTS[$repo]=0
    continue
  fi
  REPO_EXISTS[$repo]=1
  while IFS= read -r s; do
    [[ -n "$s" ]] && HAS["$repo|$s"]=1
  done < <(gh secret list -R "$ORG/$repo" --json name -q '.[].name' 2>/dev/null)
done

# --- 3. Report ---------------------------------------------------------------
declare -a DRIFT
for entry in "${!NEEDS[@]}"; do
  repo="${entry%%|*}"
  [[ "${REPO_EXISTS[$repo]:-0}" -eq 1 ]] || continue
  [[ -z "${HAS[$entry]:-}" ]] && DRIFT+=("$entry")
done

if [[ ${#DRIFT[@]} -eq 0 ]]; then
  echo "✅ No drift: every repo that reads an internal key holds it."
  exit 0
fi

echo "❌ Drift found — ${#DRIFT[@]} repo/key pair(s) read a key they do not hold:"
printf '%s\n' "${DRIFT[@]}" | sort | while IFS='|' read -r repo key; do
  printf '   %-28s %s\n' "$repo" "$key"
done
echo

if [[ $FIX -eq 0 ]]; then
  echo "Re-run with --fix to propagate the canonical values from production."
  exit 1
fi

# --- 4. Fix: propagate the canonical value ----------------------------------
# The canonical value is whatever the existing holders already run with. Read it
# from the prod VM rather than a local .env: a local value is a DEV value and
# setting it as a production secret is indistinguishable from a bogus key.
echo "Propagating from $PROD_VM ..."
[[ -f "$SSH_KEY" ]] || { echo "SSH key not found at $SSH_KEY (override with SSH_KEY=…)"; exit 2; }

read_canonical() {  # $1 = KEY -> prints value, or empty
  local key="$1"
  # -n is load-bearing: without it ssh reads the caller's STDIN and swallows
  # the rest of the piped drift list, so only the first repo was ever fixed.
  timeout 40 ssh -n -i "$SSH_KEY" -o StrictHostKeyChecking=no -o BatchMode=yes "$PROD_VM" \
    "grep -rhs '^$key=' $PROD_APP_ROOT/*/.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\n'" 2>/dev/null
}

declare -A CANON
rc=0
TMP_DRIFT="$(mktemp)"; printf '%s\n' "${DRIFT[@]}" | sort > "$TMP_DRIFT"
while IFS='|' read -r repo key; do
  val="${CANON[$key]:-}"
  if [[ -z "$val" ]]; then
    val="$(read_canonical "$key")"
    CANON[$key]="$val"
  fi
  if [[ -z "$val" ]]; then
    echo "   ⚠️  $key — no value found in production; cannot propagate. Set it manually."
    continue
  fi
  if printf %s "$val" | gh secret set "$key" -R "$ORG/$repo" >/dev/null 2>&1; then
    echo "   ✅ $repo ← $key (${#val} chars)"
  else
    echo "   ❌ $repo ← $key FAILED"
    rc=1
  fi
done < "$TMP_DRIFT"
rm -f "$TMP_DRIFT"

echo
echo "Done. The value only reaches a running service on its next deploy —"
echo "re-run each affected repo's latest workflow, e.g.:"
echo "   gh run rerun \$(gh run list -R $ORG/<repo> --branch main --limit 1 --json databaseId -q '.[0].databaseId') -R $ORG/<repo>"
exit $rc
