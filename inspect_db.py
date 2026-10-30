"""Quick look at what's stored. Usage: python inspect_db.py [--odd]"""
import sqlite3
import sys

c = sqlite3.connect("jobs.db")
c.row_factory = sqlite3.Row
if "--where" in sys.argv:   # python inspect_db.py --where "california"  -> stored jobs whose location contains the text
    term = sys.argv[sys.argv.index("--where") + 1].lower()
    hits = [r for r in c.execute("SELECT company,title,location FROM jobs") if term in (r["location"] or "").lower()]
    print(f"{len(hits)} stored job(s) with '{term}' in the location")
    for r in hits[:15]:
        print(f"  {r['company'][:14]:14s} {r['title'][:45]:45s} | {(r['location'] or '')[:55]}")
    sys.exit()
if "--odd" in sys.argv:
    for r in c.execute("SELECT company,title,location FROM jobs WHERE lower(title) LIKE '%senior%' OR lower(title) LIKE '%staff%' "
                       "OR lower(title) LIKE '%lead%'"):
        print(f"{r['company'][:14]:14s} {r['title'][:70]} | {(r['location'] or '')[:30]}")
    sys.exit()
rows = c.execute("SELECT company,title,location,pay_min,pay_max,posted,quick_score FROM jobs "
                 "ORDER BY quick_score DESC LIMIT 14").fetchall()
for r in rows:
    print(f"{r['quick_score']:5.1f} {r['company'][:14]:14s} {r['title'][:46]:46s} | {(r['location'] or '')[:28]:28s} | "
          f"{r['pay_min']}-{r['pay_max']} | {r['posted']}")
total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
paid = c.execute("SELECT COUNT(*) FROM jobs WHERE pay_min IS NOT NULL").fetchone()[0]
print(f"\nstored: {total} | pay known: {paid}")
