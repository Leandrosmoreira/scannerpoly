"""
debug_gamma.py — Mostra campos brutos da Gamma API para debugar URLs.
"""

import json
import os
import sys
import requests
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import config

now = datetime.now(timezone.utc)
end = now + timedelta(hours=2)

resp = requests.get(
    config.GAMMA_BASE + "/markets",
    params={
        "active": "true",
        "closed": "false",
        "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 5,
    },
    timeout=10,
)
markets = resp.json()

print(f"\n{len(markets)} mercados encontrados\n")

for m in markets[:3]:
    print("=" * 80)
    # Mostrar TODOS os campos que contem "slug", "url", "link", "event", "group"
    slug_fields = {}
    for k, v in m.items():
        kl = k.lower()
        if any(word in kl for word in ["slug", "url", "link", "event", "group", "id", "question"]):
            slug_fields[k] = v

    print(f"question: {m.get('question', '?')[:60]}")
    print(f"\nCampos relevantes:")
    for k, v in sorted(slug_fields.items()):
        val_str = str(v)
        if len(val_str) > 80:
            val_str = val_str[:77] + "..."
        print(f"  {k:30s} = {val_str}")

    print(f"\nTODOS os campos (keys):")
    print(f"  {sorted(m.keys())}")
    print()
