"""Enregistre la position des antennes pour régler la détection de chatouilles.

    .venv/bin/python -u calibrer_gratouille.py

Compte à rebours, 5 s sans toucher, 10 s à toucher / bouger doucement les antennes, 5 s sans toucher.
Résultat dans gratouille.csv.
"""

import csv
import time

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

PHASES = [(5, "repos1", "NE TOUCHE PAS le robot"),
          (10, "touche", "TOUCHE / BOUGE DOUCEMENT SES ANTENNES maintenant !"),
          (5, "repos2", "Arrête, ne touche plus")]

mini = ReachyMini(host="reachy-mini.local", timeout=10, media_backend="no_media")
mini.acquire_media()  # le mode no_media a fait lâcher micro/caméra au robot : on les lui rend
mini.wake_up()
mini.goto_target(head=create_head_pose(), antennas=[0, 0], duration=1.0)
for i in (3, 2, 1):
    print(f"Prépare-toi… {i}", flush=True)
    time.sleep(1)

lignes = []
for duree, phase, consigne in PHASES:
    print(f"\n>>> {consigne} ({duree} s)", flush=True)
    fin = time.time() + duree
    while time.time() < fin:
        _, (ant_d, ant_g) = mini.get_current_joint_positions()
        lignes.append([time.time(), phase, ant_d, ant_g])
        time.sleep(0.02)

with open("gratouille.csv", "w", newline="") as f:
    csv.writer(f).writerows([["t", "phase", "antD", "antG"], *lignes])
print(f"\nOK, {len(lignes)} mesures enregistrées dans gratouille.csv", flush=True)
