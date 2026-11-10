"""Interface web locale pour piloter Reachy Mini : http://localhost:8765

    .venv/bin/python interface.py
    .venv/bin/python interface.py --host 192.168.0.117

Bouger la tête / antennes / corps, gestes, veille, moteurs, volume,
faire parler le robot, lancer ou arrêter l'assistant vocal.
"""

import argparse
import importlib.util
import io
import math
import os
import signal
import subprocess
import tempfile
import threading
import webbrowser
from pathlib import Path

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

ICI = Path(__file__).parent
LOG_ASSISTANT = ICI / "assistant.log"

# Réutilise les gestes et la voix de l'assistant
_spec = importlib.util.spec_from_file_location("assistant", ICI / "04_assistant.py")
assistant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assistant)

from reachy_mini import ReachyMini  # noqa: E402  (après le réglage GST_PLUGIN_PATH de l'assistant)
from reachy_mini.utils import create_head_pose  # noqa: E402

app = FastAPI()
ROBOT = {"host": "reachy-mini.local", "mini": None, "gestes": None}
VERROU = threading.Lock()
VOIX = {"piper": None}
PROC = {"assistant": None}


def daemon_url(chemin: str) -> str:
    return f"http://{ROBOT['host']}:8000{chemin}"


def robot() -> ReachyMini:
    """Connexion au robot (sans son ni caméra : l'assistant vocal les garde)."""
    with VERROU:
        if ROBOT["mini"] is None:
            try:
                mini = ReachyMini(host=ROBOT["host"], timeout=10.0, media_backend="no_media")
                # En mode no_media, le SDK demande au robot de lâcher son micro et sa caméra.
                # On les lui rend tout de suite, sinon l'assistant vocal ne peut plus s'y connecter.
                mini.acquire_media()
                ROBOT["mini"] = mini
                ROBOT["gestes"] = assistant.Gestes(mini, ROBOT["host"])
            except Exception as e:
                raise HTTPException(503, f"Robot introuvable : {e}")
        return ROBOT["mini"]


def avec_robot(fn):
    """Exécute fn(mini) ; si la connexion est tombée, reconnecte une fois."""
    for essai in range(2):
        mini = robot()
        try:
            return fn(mini)
        except (ConnectionError, TimeoutError) as e:
            ROBOT["mini"] = None
            if essai:
                raise HTTPException(503, f"Connexion perdue : {e}")


def en_fond(fn, **kwargs) -> threading.Thread:
    """Lance fn dans un thread ; si la connexion tombe, la prochaine action reconnecte."""
    def cible():
        try:
            fn(**kwargs)
        except (ConnectionError, TimeoutError) as e:
            print(f"Connexion perdue pendant un mouvement : {e}")
            ROBOT["mini"] = None
            if ROBOT.get("repos"):
                ROBOT["repos"].pause()
                ROBOT["repos"] = None
    t = threading.Thread(target=cible, daemon=True)
    t.start()
    return t


# ---------- API ----------

class Pose(BaseModel):
    x: float = 0
    y: float = 0
    z: float = 0
    roll: float = 0
    pitch: float = 0
    yaw: float = 0
    antenne_droite: float = 0  # degrés
    antenne_gauche: float = 0
    corps: float = 0  # degrés


@app.post("/api/pose")
def pose(p: Pose):
    tete = create_head_pose(x=p.x, y=p.y, z=p.z, roll=p.roll, pitch=p.pitch, yaw=p.yaw, mm=True)
    antennes = [math.radians(p.antenne_droite), math.radians(p.antenne_gauche)]
    avec_robot(lambda m: m.set_target(head=tete, antennas=antennes, body_yaw=math.radians(p.corps)))
    return {"ok": True}


@app.post("/api/geste/{nom}")
def geste(nom: str):
    if nom not in assistant.GESTES:
        raise HTTPException(404, "geste inconnu")
    robot()
    en_fond(ROBOT["gestes"].faire, nom=nom)
    return {"ok": True}


DANSE = {"stop": threading.Event(), "thread": None}


@app.get("/api/danses")
def danses():
    from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES
    return [{"nom": n, "description": meta.get("description", "")}
            for n, (_, _, meta) in AVAILABLE_MOVES.items() if n != "headbanger_combo"]


@app.post("/api/danse/{nom}")
def danse(nom: str, duree: float = 10.0):
    """nom = un mouvement de la bibliothèque, ou « aleatoire » pour 3 mouvements enchaînés."""
    robot()
    if DANSE["thread"] and DANSE["thread"].is_alive():
        DANSE["stop"].set()
        DANSE["thread"].join()
    DANSE["stop"].clear()
    DANSE["thread"] = en_fond(ROBOT["gestes"].danser, nom=None if nom == "aleatoire" else nom,
                              duree=duree, stop=DANSE["stop"])
    return {"ok": True}


@app.post("/api/danse_stop")
def danse_stop():
    """Arrête la danse ou l'émotion en cours."""
    DANSE["stop"].set()
    return {"ok": True}


def son_robot(chemin) -> None:
    """Envoie un fichier son au robot et le joue sur son haut-parleur."""
    chemin = Path(chemin)
    httpx.post(daemon_url("/api/media/sounds/upload"),
               files={"file": (chemin.name, chemin.read_bytes())}, timeout=15).raise_for_status()
    httpx.post(daemon_url("/api/media/play_sound"), json={"file": chemin.name}, timeout=10)


@app.get("/api/emotions")
def emotions():
    return {"tags": list(assistant.EMOTIONS),
            "toutes": sorted(assistant.Gestes.bibliotheque().list_moves())}


@app.post("/api/emotion/{nom}")
def emotion(nom: str, son: bool = True):
    robot()
    if DANSE["thread"] and DANSE["thread"].is_alive():
        DANSE["stop"].set()
        DANSE["thread"].join()
    DANSE["stop"].clear()
    DANSE["thread"] = en_fond(ROBOT["gestes"].emotion, nom=nom,
                              son=son_robot if son else None, stop=DANSE["stop"])
    return {"ok": True}


@app.post("/api/repos/{etat}")
def repos(etat: str):
    robot()
    if ROBOT.get("repos") is None:
        ROBOT["repos"] = assistant.Repos(ROBOT["mini"], host=ROBOT["host"])
    if etat == "on":
        ROBOT["repos"].activer()
    else:
        ROBOT["repos"].pause()
    return {"repos": ROBOT["repos"].actif.is_set()}


ACTIONS = {
    "reveil": lambda m: (m.enable_motors(), m.wake_up()),
    "dodo": lambda m: m.goto_sleep(),
    "neutre": lambda m: m.goto_target(head=create_head_pose(), antennas=[0, 0], body_yaw=0.0, duration=0.8),
    "moteurs_on": lambda m: (m.disable_gravity_compensation(), m.enable_motors()),
    "moteurs_off": lambda m: m.disable_motors(),
    "main_libre": lambda m: m.enable_gravity_compensation(),
}


@app.post("/api/action/{nom}")
def action(nom: str):
    if nom not in ACTIONS:
        raise HTTPException(404, "action inconnue")
    avec_robot(ACTIONS[nom])
    return {"ok": True}


class Volume(BaseModel):
    volume: int


@app.post("/api/volume")
def volume(v: Volume):
    r = httpx.post(daemon_url("/api/volume/set"), json={"volume": max(0, min(100, v.volume))}, timeout=5)
    return r.json()


class Texte(BaseModel):
    texte: str


@app.post("/api/dire")
def dire(t: Texte):
    """Synthèse Piper sur le PC, envoi du WAV au robot, lecture par son haut-parleur."""
    texte = assistant.nettoyer(t.texte)
    if not texte:
        raise HTTPException(400, "texte vide")
    if VOIX["piper"] is None:
        VOIX["piper"] = assistant.PiperVoice.load(ICI / "voix" / "fr_FR-siwis-medium.onnx")
    chunks = list(VOIX["piper"].synthesize(texte))
    son = np.concatenate([c.audio_float_array for c in chunks])
    son = np.tanh(son * 2.5) / np.tanh(2.5)  # même volume que l'assistant
    buf = io.BytesIO()
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        assistant.ecrire_wav(f.name, son, chunks[0].sample_rate)
        buf.write(Path(f.name).read_bytes())
    nom = "interface_dire.wav"
    httpx.post(daemon_url("/api/media/sounds/upload"),
               files={"file": (nom, buf.getvalue(), "audio/wav")}, timeout=15).raise_for_status()
    r = httpx.post(daemon_url("/api/media/play_sound"), json={"file": nom}, timeout=10)
    r.raise_for_status()
    return {"ok": True}


@app.post("/api/assistant/{cmd}")
def assistant_cmd(cmd: str):
    proc = PROC["assistant"]
    actif = proc is not None and proc.poll() is None
    if cmd == "start" and not actif:
        LOG_ASSISTANT.write_text("")
        PROC["assistant"] = subprocess.Popen(
            ["bash", str(ICI / "lancer.sh"), "--host", ROBOT["host"]],
            cwd=ICI, stdout=LOG_ASSISTANT.open("a"), stderr=subprocess.STDOUT,
            start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    elif cmd == "stop" and actif:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    return {"actif": PROC["assistant"] is not None and PROC["assistant"].poll() is None}


@app.post("/api/app_robot/stop")
def stop_app_robot():
    httpx.post(daemon_url("/api/apps/stop-current-app"), timeout=15)
    return {"ok": True}


@app.get("/api/etat")
def etat():
    res = {"host": ROBOT["host"], "joignable": False}
    try:
        s = httpx.get(daemon_url("/api/daemon/status"), timeout=3).json()
        res.update(joignable=True, version=s.get("version"), etat=s.get("state"),
                   ip=s.get("wlan_ip"), moteurs=s.get("backend_status", {}).get("motor_control_mode"))
        res["volume"] = httpx.get(daemon_url("/api/volume/current"), timeout=3).json().get("volume")
        a = httpx.get(daemon_url("/api/apps/current-app-status"), timeout=3).json()
        res["app_robot"] = a["info"]["name"] if a and a.get("state") == "running" else None
    except Exception:
        pass
    proc = PROC["assistant"]
    res["assistant"] = proc is not None and proc.poll() is None
    lignes = LOG_ASSISTANT.read_text(errors="ignore").splitlines() if LOG_ASSISTANT.exists() else []
    res["log"] = lignes[-40:]
    return res


@app.get("/", response_class=HTMLResponse)
def page():
    return (ICI / "interface.html").read_text()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="reachy-mini.local")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    ROBOT["host"] = args.host
    url = f"http://localhost:{args.port}"
    print(f"Interface : {url}  (Ctrl+C pour arrêter)")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        proc = PROC["assistant"]
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGINT)


if __name__ == "__main__":
    main()
