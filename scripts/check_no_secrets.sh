#!/usr/bin/env bash
# CI guard: reject PEM / private-key material in tracked files. Wire into CI as required.
set -euo pipefail

fail=0

# tracked key files by extension
keyfiles=$(git ls-files -- '*.pem' '*.key' '*.p12' '*.pfx' || true)
if [ -n "$keyfiles" ]; then
  echo "FAIL: tracked key files:"; echo "$keyfiles"; fail=1
fi

# private-key headers anywhere in tracked content
hdr=$(git grep -lI -E 'BEGIN (RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY' -- . ':(exclude)scripts/check_no_secrets.sh' 2>/dev/null || true)
if [ -n "$hdr" ]; then
  echo "FAIL: private-key material in tracked files:"; echo "$hdr"; fail=1
fi

# these env files are tracked on purpose: two templates and the config of the isolated e2e stack
unexpected_env=$(git ls-files -- '.env*' '**/.env*' | grep -vE '^(\.env\.example|\.env\.e2e|ops/identity/\.env\.example)$' || true)
if [ -n "$unexpected_env" ]; then
  echo "FAIL: tracked env files:"; echo "$unexpected_env"; fail=1
fi

# credential shapes that carry their own prefix, so a leak is recognisable without guessing at entropy
tokens=$(git grep -lIE 'AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*KEY-----' -- . ':(exclude)scripts/check_no_secrets.sh' 2>/dev/null || true)
if [ -n "$tokens" ]; then
  echo "FAIL: credential-shaped strings in tracked files:"; echo "$tokens"; fail=1
fi

[ "$fail" -eq 0 ] && echo "no tracked secrets"
exit "$fail"
