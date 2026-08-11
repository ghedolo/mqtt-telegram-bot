#!/usr/bin/env bash
# query.sh — estrae letture di un sensore sopra/sotto una soglia dal DB SQLite,
# senza toccare il bot in esecuzione: usa un container usa-e-getta (docker run --rm)
# che monta ./data in sola lettura e apre il DB in mode=ro.
#
# Uso:
#   ./query.sh <SENSORE> <SOGLIA> [OP] [LIMIT]
#
#   OP    operatore di confronto: > >= < <= = !=   (default: >)
#   LIMIT numero massimo di righe (default: 0 = tutte)
#
# Esempi:
#   ./query.sh DEI-P2_T 30            # value > 30
#   ./query.sh DEI-P2_T 30 '>=' 100   # value >= 30, max 100 righe
#   ./query.sh DEI-P2_T 5 '<'         # value < 5
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Uso: $0 <SENSORE> <SOGLIA> [OP: > >= < <= = !=] [LIMIT]" >&2
  exit 2
fi

SENSOR="$1"
THRESHOLD="$2"
OP="${3:->}"
LIMIT="${4:-0}"

# operatore consentito (whitelist: evita injection nell'operatore)
case "$OP" in
  '>'|'>='|'<'|'<='|'='|'!=') ;;
  *) echo "Operatore non valido: '$OP' (ammessi: > >= < <= = !=)" >&2; exit 2 ;;
esac

# directory dati = ./data accanto a questo script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"

if [[ ! -f "$DATA_DIR/sensors.db" ]]; then
  echo "DB non trovato: $DATA_DIR/sensors.db" >&2
  exit 1
fi

# sensore e soglia passati come env var -> query parametrizzata (no SQL injection)
docker run --rm \
  -v "$DATA_DIR:/data:ro" \
  -e SENSOR="$SENSOR" \
  -e THRESHOLD="$THRESHOLD" \
  -e OP="$OP" \
  -e LIMIT="$LIMIT" \
  python:3.12-slim \
  python -c '
import os, sqlite3, sys

sensor    = os.environ["SENSOR"]
threshold = float(os.environ["THRESHOLD"])
op        = os.environ["OP"]
limit     = int(os.environ["LIMIT"])

con = sqlite3.connect("file:/data/sensors.db?mode=ro", uri=True)

sql = (
    "SELECT datetime(ts, \"unixepoch\", \"localtime\") AS ts_local, value "
    "FROM readings "
    f"WHERE sensor = ? AND value {op} ? "
    "ORDER BY ts DESC"
)
if limit > 0:
    sql += f" LIMIT {limit}"

rows = con.execute(sql, (sensor, threshold)).fetchall()

print(f"# {sensor}: value {op} {threshold}  ({len(rows)} letture)")
for ts_local, value in rows:
    print(f"{ts_local}\t{value}")
'
