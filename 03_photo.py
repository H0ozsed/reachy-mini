"""Prend une photo avec la caméra du robot et l'enregistre dans photo.jpg."""

from pathlib import Path

from reachy_mini import ReachyMini

with ReachyMini() as mini:
    jpeg = mini.media.get_frame_jpeg()
    if jpeg is None:
        raise SystemExit("Pas d'image : caméra indisponible (daemon lancé avec --no-media ?)")
    Path("photo.jpg").write_bytes(jpeg)
    print("Photo enregistrée : photo.jpg")
