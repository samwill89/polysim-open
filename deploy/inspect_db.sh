#!/bin/sh
ls -la /data/
python -c "
import sqlite3
db = sqlite3.connect('/data/polysim.db')
t = db.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
m = db.execute('SELECT COUNT(*) FROM markets').fetchone()[0]
mt = db.execute(\"SELECT MAX(timestamp) FROM trades\").fetchone()[0]
print(f'trades: {t}')
print(f'markets: {m}')
print(f'latest trade: {mt}')
"
