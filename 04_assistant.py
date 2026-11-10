"""Assistant vocal 100 % local : tu parles, Reachy Mini répond avec Gemma (Ollama).

Chaîne : micro -> Whisper (texte) -> Gemma via Ollama -> Piper (voix FR) -> haut-parleur.

    .venv/bin/python 04_assistant.py               # avec le robot (daemon lancé)
    .venv/bin/python 04_assistant.py --sans-robot  # micro/haut-parleur du PC, sans robot
    .venv/bin/python 04_assistant.py --modele qwen3.5:4b

Dis « regarde » ou « tu vois » : la photo de la caméra est envoyée à Gemma (vision).
Dis « au revoir » pour quitter.
"""

import argparse
import base64
import json
import math
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
import wave
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
from faster_whisper import WhisperModel
from piper import PiperVoice

ICI = Path(__file__).parent

# Plugin webrtcsrc compilé par install_wireless.sh (audio/caméra du Reachy Mini Wireless)
_GST_RS = "/opt/gst-plugins-rs/lib/x86_64-linux-gnu"
if os.path.isdir(_GST_RS) and _GST_RS not in os.environ.get("GST_PLUGIN_PATH", ""):
    os.environ["GST_PLUGIN_PATH"] = _GST_RS + os.pathsep + os.environ.get("GST_PLUGIN_PATH", "")
OLLAMA = "http://localhost:11434/api/chat"
RATE = 16000  # Whisper travaille en 16 kHz mono

SYSTEME = (
    "Tu es Reachy Mini, un petit robot de bureau sympa et curieux, fabriqué par Hugging Face. "
    "Tu parles français. Tes réponses sont lues à voix haute : 1 à 3 phrases courtes, "
    "pas de listes, pas de markdown, pas d'emoji. "
    "Tu as une tête qui bouge, deux antennes et un corps qui pivote. Pour bouger, écris un tag "
    "entre crochets au début d'une phrase. Émotions : [oui], [non], [content], [rire], [triste], "
    "[surpris], [colere], [peur], [reflechit], [perplexe], [curieux], [amour], [merci], [fier], "
    "[fatigue], [ennui], [timide], [degout], [oups], [soulage], [coucou] pour saluer. "
    "Mouvements : [gauche], [droite], [haut], [bas] pour regarder, [tourne] pivoter le corps, [danse]. "
    "Quand on te demande un mouvement, fais-le avec le tag, ne dis jamais que tu ne peux pas bouger. "
    "Utilise aussi les tags de toi-même pour montrer tes émotions, sans en abuser. "
    "Exemple : « [oui] Bien sûr, je hoche la tête ! »"
)
# Phrases que Whisper invente sur du bruit ou du silence
HALLUCINATIONS = ("sous-titrage", "sous-titres", "amara.org", "merci d'avoir regardé",
                  "abonnez-vous", "st' 501", "radio-canada")
MOTS_VISION = ("regarde", "tu vois", "qu'est-ce que tu vois", "devant toi")
MOTS_FIN = ("au revoir", "bonne nuit", "stop reachy")


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x.astype(np.float32)
    n = int(len(x) * dst / src)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


# ---------- Entrée / sortie audio ----------

class AudioPC:
    """Micro et haut-parleur du PC via PipeWire (pw-record / pw-play)."""

    def __init__(self):
        self.proc = None

    def ouvrir_micro(self):
        self.proc = subprocess.Popen(
            ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "f32", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def fermer_micro(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None

    def lire(self) -> np.ndarray:
        data = self.proc.stdout.read(1600 * 4)  # 100 ms
        return np.frombuffer(data, dtype=np.float32)

    def jouer(self, audio: np.ndarray, rate: int):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            ecrire_wav(f.name, audio, rate)
            subprocess.run(["pw-play", f.name], check=False)


def verifier_audio_robot(media):
    """Refuse de démarrer si le SDK retomberait sur le micro/haut-parleur du PC.

    Reachy Mini Lite (USB) : le SDK cherche la carte son « Reachy Mini Audio » et,
    s'il ne la trouve pas, prend en silence le micro par défaut du PC.
    Reachy Mini Wireless : le son passe par WebRTC depuis le robot, rien à vérifier.
    """
    from reachy_mini.media.audio_gstreamer import GStreamerAudio
    from reachy_mini.media.device_detection import get_audio_device

    if media.audio is None:
        raise SystemExit("Audio du robot indisponible. Wireless : lance install_wireless.sh "
                         "(plugin webrtcsrc). Lite : daemon lancé avec --no-media ?")
    if not isinstance(media.audio, GStreamerAudio):
        return
    manque = [nom for nom, t in (("micro", "Source"), ("haut-parleur", "Sink"))
              if get_audio_device(t) is None]
    if manque:
        raise SystemExit(
            f"{' et '.join(manque).capitalize()} du robot introuvable(s) : carte son "
            "« Reachy Mini Audio » absente. Vérifie le câble USB et l'alimentation du robot. "
            "(Pour tester avec le PC : --sans-robot)"
        )


class AudioRobot:
    """Micro et haut-parleur de Reachy Mini (la tête bouge en parlant)."""

    def __init__(self, mini):
        self.mini = mini
        self.media = mini.media
        verifier_audio_robot(self.media)
        self.rate_in = self.media.get_input_audio_samplerate()
        self.rate_out = self.media.get_output_audio_samplerate()
        self.media.start_playing()
        mini.enable_wobbling()

    def ouvrir_micro(self):
        self.media.start_recording()

    def fermer_micro(self):
        self.media.stop_recording()

    def lire(self) -> np.ndarray:
        s = self.media.get_audio_sample()
        if s is None:
            time.sleep(0.02)
            return np.zeros(0, dtype=np.float32)
        if s.ndim == 2:
            s = s.mean(axis=1)
        return resample(s, self.rate_in, RATE)

    def jouer(self, audio: np.ndarray, rate: int):
        self.media.push_audio_sample(resample(audio, rate, self.rate_out))
        time.sleep(len(audio) / rate + 0.2)


def ecrire_wav(path, audio: np.ndarray, rate: int):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def ecouter(audio, seuil: float, silence_fin: float = 0.9, max_s: float = 15.0,
            interruption: threading.Event | None = None) -> np.ndarray:
    """Attend que tu parles, enregistre jusqu'à un silence, renvoie l'audio 16 kHz.

    S'arrête aussi dès que `interruption` est levé (ex. on chatouille les antennes).
    """
    audio.ouvrir_micro()
    morceaux, parle, dernier_son, debut = [], False, 0.0, 0.0
    try:
        while True:
            if interruption is not None and interruption.is_set() and not parle:
                return np.zeros(0, dtype=np.float32)
            bloc = audio.lire()
            if len(bloc) == 0:
                continue
            bloc = bloc - bloc.mean()  # certains micros ont un décalage continu
            fort = np.sqrt(np.mean(bloc ** 2)) > seuil
            now = time.time()
            if fort and not parle:
                parle, debut = True, now
                morceaux = morceaux[-3:]  # garde ~300 ms avant le début
            if parle:
                morceaux.append(bloc)
                if fort:
                    dernier_son = now
                if now - dernier_son > silence_fin or now - debut > max_s:
                    break
            else:
                morceaux.append(bloc)
                morceaux = morceaux[-3:]
    finally:
        audio.fermer_micro()
    return np.concatenate(morceaux)


# ---------- Cerveau ----------

def phrases_gemma(modele: str, historique: list):
    """Stream la réponse d'Ollama et la découpe en phrases (pour parler plus vite)."""
    tampon, complet = "", ""
    with httpx.stream(
        "POST", OLLAMA, timeout=120,
        json={"model": modele, "messages": historique, "stream": True, "think": False,
              "options": {"temperature": 0.7}},
    ) as r:
        r.raise_for_status()
        for ligne in r.iter_lines():
            if not ligne:
                continue
            morceau = json.loads(ligne).get("message", {}).get("content", "")
            tampon += morceau
            complet += morceau
            while (m := re.search(r"[.!?…]\s", tampon)):
                yield tampon[: m.end()].strip()
                tampon = tampon[m.end():]
    if tampon.strip():
        yield tampon.strip()
    historique.append({"role": "assistant", "content": complet})


def nettoyer(texte: str) -> str:
    return re.sub(r"[*_#`>\[\]]", "", texte).strip()


# ---------- Gestes ----------

class DaemonRobot:
    """Appels REST au daemon du robot : les mouvements sont calculés SUR le robot, donc fluides
    même si le Wi-Fi est irrégulier (au lieu de 100 positions/s envoyées depuis le PC)."""

    def __init__(self, host: str):
        self.url = f"http://{host}:8000"

    def jouer(self, dataset: str, nom: str, stop=None, duree_max: float | None = None):
        """Joue un mouvement enregistré sur le robot et attend la fin (ou stop / duree_max)."""
        r = httpx.post(f"{self.url}/api/move/play/recorded-move-dataset/{dataset}/{nom}", timeout=15)
        r.raise_for_status()
        uuid = r.json()["uuid"]
        t0 = time.time()
        while True:
            time.sleep(0.1)
            en_cours = [m["uuid"] for m in httpx.get(f"{self.url}/api/move/running", timeout=5).json()]
            if uuid not in en_cours:
                return
            if (stop is not None and stop.is_set()) or (duree_max and time.time() - t0 > duree_max):
                self.arreter(uuid)
                return

    def goto(self, x=0.0, y=0.0, z=0.0, roll=0.0, pitch=0.0, yaw=0.0, antennes=None, duree=1.0) -> str:
        """Déplacement interpolé par le robot (m et rad). Ne bloque pas ; renvoie l'id du mouvement."""
        corps = {"head_pose": {"x": x, "y": y, "z": z, "roll": roll, "pitch": pitch, "yaw": yaw},
                 "antennas": antennes, "duration": duree, "interpolation": "minjerk"}
        return httpx.post(f"{self.url}/api/move/goto", json=corps, timeout=5).json()["uuid"]

    def arreter(self, uuid: str):
        try:
            httpx.post(f"{self.url}/api/move/stop", json={"uuid": uuid}, timeout=5)
        except Exception:
            pass  # déjà fini


class Gestes:
    def __init__(self, mini, host: str = "reachy-mini.local"):
        self.mini = mini
        self.daemon = DaemonRobot(host)
        if mini:
            from reachy_mini.utils import create_head_pose
            self.pose = create_head_pose

    def ecoute(self):
        if self.mini:
            self.mini.goto_target(head=self.pose(pitch=-8), antennas=[0.3, -0.3], duration=0.4)

    def reflechit(self):
        if self.mini:
            self.mini.goto_target(head=self.pose(roll=10, pitch=5), antennas=[-0.5, 0.5], duration=0.5)

    def neutre(self):
        if self.mini:
            self.mini.goto_target(head=self.pose(), antennas=[0, 0], duration=0.4)

    # Gestes que Gemma peut demander avec un tag [nom] dans sa réponse.
    # Chaque geste : liste de (tête, antennes, rotation du corps en rad, durée en s).
    def _sequences(self):
        p = self.pose
        return {
            "oui": [(p(pitch=15), None, None, 0.3), (p(pitch=-10), None, None, 0.3),
                    (p(pitch=15), None, None, 0.3), (p(), None, None, 0.3)],
            "non": [(p(yaw=25), None, None, 0.3), (p(yaw=-25), None, None, 0.3),
                    (p(yaw=25), None, None, 0.3), (p(), None, None, 0.3)],
            "content": [(None, [0.7, -0.7], None, 0.2), (None, [-0.7, 0.7], None, 0.2),
                        (None, [0.7, -0.7], None, 0.2), (None, [0, 0], None, 0.2)],
            "triste": [(p(pitch=20, z=-10, mm=True), [-1.0, 1.0], None, 1.0),
                       (p(), [0, 0], None, 1.0)],
            "surpris": [(p(z=15, pitch=-10, mm=True), [1.2, -1.2], None, 0.25),
                        (p(), [0, 0], None, 0.8)],
            "gauche": [(p(yaw=35), None, None, 0.6), (p(), None, None, 0.6)],
            "droite": [(p(yaw=-35), None, None, 0.6), (p(), None, None, 0.6)],
            "haut": [(p(pitch=-25), None, None, 0.6), (p(), None, None, 0.6)],
            "bas": [(p(pitch=25), None, None, 0.6), (p(), None, None, 0.6)],
            "coucou": [(p(roll=20), [0.8, 0], None, 0.3), (p(roll=-20), [0, -0.8], None, 0.3),
                       (p(roll=20), [0.8, 0], None, 0.3), (p(), [0, 0], None, 0.3)],
            "tourne": [(None, None, 0.9, 1.0), (None, None, -0.9, 1.5), (None, None, 0.0, 1.0)],
            # "danse" : voir danser() (bibliothèque reachy_mini_dances_library)
        }

    def danser(self, nom: str | None = None, duree: float = 10.0, bpm: float = 120.0, stop=None):
        """Danse de reachy_mini_dances_library : un mouvement précis, ou 3 au hasard enchaînés."""
        import random

        from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES

        noms = [n for n in AVAILABLE_MOVES if n != "headbanger_combo"]  # trop violent, exclu par la lib
        sequence = [nom] if nom else random.sample(noms, 3)
        par_mouvement = duree / len(sequence)
        try:  # joué par le robot lui-même : fluide
            for n in sequence:
                if stop is not None and stop.is_set():
                    break
                self.daemon.jouer(DATASET_DANSES, n, stop=stop, duree_max=par_mouvement)
            self.neutre()
            return
        except Exception as e:
            print(f"   (danse sur le robot impossible, envoi depuis le PC : {e})")
        for n in sequence:
            fn, params, _ = AVAILABLE_MOVES[n]
            t0 = time.time()
            while (t := time.time() - t0) < par_mouvement:
                if stop is not None and stop.is_set():
                    break
                o = fn(t * bpm / 60.0, **params)  # temps en battements
                self.mini.set_target(
                    head=self.pose(*o.position_offset, *o.orientation_offset, degrees=False),
                    antennas=o.antennas_offset,
                )
                time.sleep(0.01)  # 100 Hz, comme la démo officielle
        self.neutre()

    _bibliotheque = None  # émotions enregistrées, chargées une seule fois

    @classmethod
    def bibliotheque(cls):
        """85 émotions enregistrées par Pollen Robotics (téléchargées une fois, puis en cache)."""
        if cls._bibliotheque is None:
            from reachy_mini.motion.recorded_move import RecordedMoves
            cls._bibliotheque = RecordedMoves(DATASET_EMOTIONS)
        return cls._bibliotheque

    def emotion(self, nom: str, son=None, stop=None) -> bool:
        """Joue une émotion : un tag français (« content ») ou un nom brut (« laughing1 »).

        son : fonction appelée avec le chemin du son de l'émotion (None = pas de son).
        Renvoie False si l'émotion n'existe pas.
        """
        biblio = self.bibliotheque()
        dispo = biblio.list_moves()
        candidats = [n for n in EMOTIONS.get(nom, (nom,)) if n in dispo]
        if not candidats:
            return False
        choix = random.choice(candidats)
        move = biblio.get(choix)
        if son and move.sound_path:
            son(move.sound_path)
        try:  # joué par le robot lui-même : fluide
            self.daemon.jouer(DATASET_EMOTIONS, choix, stop=stop)
            self.neutre()
            return True
        except Exception as e:
            print(f"   (émotion sur le robot impossible, envoi depuis le PC : {e})")
        tete, antennes, corps = move.evaluate(0.0)
        self.mini.goto_target(head=tete, antennas=antennes, body_yaw=corps, duration=0.4)
        t0 = time.time()
        while (t := time.time() - t0) < move.duration:
            if stop is not None and stop.is_set():
                break
            tete, antennes, corps = move.evaluate(t)
            self.mini.set_target(head=tete, antennas=antennes, body_yaw=corps)
            time.sleep(0.01)
        self.neutre()
        return True

    def faire(self, nom: str):
        if not self.mini:
            print(f"   (geste {nom})")
            return
        if nom == "danse":
            try:
                self.danser()
            except Exception as e:
                print(f"   (danse ratée : {e})")
            return
        if nom in EMOTIONS:
            try:
                if self.emotion(nom):
                    return
            except Exception as e:  # pas de réseau au 1er lancement, etc. : geste fait main
                print(f"   (émotion {nom} indisponible : {e})")
        etapes = self._sequences().get(nom)
        if not etapes:
            return
        try:
            for tete, antennes, corps, duree in etapes:
                self.mini.goto_target(head=tete, antennas=antennes, body_yaw=corps, duration=duree)
        except Exception as e:  # un geste raté ne doit pas couper la conversation
            print(f"   (geste {nom} raté : {e})")


DATASET_EMOTIONS = "pollen-robotics/reachy-mini-emotions-library"
DATASET_DANSES = "pollen-robotics/reachy-mini-dances-library"  # les mêmes danses, côté robot
# Tag français -> émotions enregistrées (choix inspiré de reachy_mini_conversation_app)
EMOTIONS = {
    "oui": ("yes1", "understanding2"),
    "non": ("no1",),
    "content": ("cheerful1", "enthusiastic1", "success1"),
    "rire": ("laughing1", "laughing2"),
    "triste": ("sad1", "sad2", "downcast1"),
    "surpris": ("surprised1", "surprised2", "amazed1"),
    "colere": ("rage1", "irritated1", "irritated2"),
    "peur": ("scared1", "fear1"),
    "reflechit": ("thoughtful1", "thoughtful2"),
    "perplexe": ("confused1", "uncertain1"),
    "curieux": ("curious1", "inquiring1"),
    "amour": ("loving1",),
    "merci": ("grateful1",),
    "fier": ("proud1", "proud2"),
    "fatigue": ("exhausted1", "tired1"),
    "ennui": ("boredom1", "boredom2"),
    "timide": ("shy1",),
    "degout": ("disgusted1",),
    "oups": ("oops1", "oops2"),
    "soulage": ("relief1", "relief2"),
    "coucou": ("welcoming1", "welcoming2"),
}
GESTES = list(EMOTIONS) + ["gauche", "droite", "haut", "bas", "tourne", "danse"]


class Repos:
    """Mouvements au repos pendant que le robot écoute : il respire et regarde autour de lui."""

    SEUIL_CHATOUILLE = math.radians(25)  # écart antenne / consigne qui veut dire « on me touche »

    DEMI_SOUFFLE = 2.25  # s : on inspire, puis on expire (respiration de 4,5 s)
    AMPLITUDE_ANTENNES = 0.15  # rad, balancement des antennes au repos

    def __init__(self, mini, sur_chatouille=None, host: str = "reachy-mini.local"):
        self.mini = mini
        self.actif = threading.Event()
        self.regard = 0.0
        self.sur_chatouille = sur_chatouille
        self.daemon = DaemonRobot(host)
        self.mouvement = None  # id du goto en cours côté robot
        if mini:
            from reachy_mini.utils import create_head_pose
            self.pose = create_head_pose
            threading.Thread(target=self._boucle, daemon=True).start()

    def activer(self):
        if not self.mini or self.actif.is_set():
            return
        self.regard = 0.0
        try:
            self.mini.goto_target(head=self.pose(pitch=-5), antennas=[0, 0], duration=0.5)
        except Exception:
            pass
        self.actif.set()

    def pause(self):
        self.actif.clear()
        if self.mouvement:  # coupe la respiration en cours pour laisser la place au geste suivant
            self.daemon.arreter(self.mouvement)
            self.mouvement = None

    def _boucle(self):
        cible, prochain = 0.0, time.time() + random.uniform(4, 8)
        ecarte, derniere_chatouille, debut_repos = 0, 0.0, time.time()
        precedentes, figees = None, 0
        inspire, prochain_souffle = True, 0.0
        while True:
            if not self.actif.is_set():
                ecarte = 0
                self.actif.wait()
                debut_repos = time.time()  # laisse les antennes se poser après un geste
                prochain_souffle = 0.0
            now = time.time()
            try:
                # Un demi-souffle = UN déplacement interpolé par le robot (fluide), pas 45 envois Wi-Fi
                if now >= prochain_souffle:
                    if now > prochain:  # de temps en temps, jette un œil ailleurs (ou revient au centre)
                        cible = random.choice([0.0, 0.0, random.uniform(-25, 25)])
                        prochain = now + random.uniform(4, 10)
                    self.regard = cible
                    s = 1 if inspire else -1
                    a = self.AMPLITUDE_ANTENNES * s
                    if self.actif.is_set():
                        self.mouvement = self.daemon.goto(
                            z=0.003 * s, pitch=math.radians(-5 + 1.5 * s), yaw=math.radians(self.regard),
                            antennes=[a, -a], duree=self.DEMI_SOUFFLE)
                    inspire = not inspire
                    prochain_souffle = now + self.DEMI_SOUFFLE
                # Antenne poussée par un doigt : elle s'écarte de sa position de repos pendant ≥ 0,3 s.
                # La lecture des moteurs peut rester figée (moteurs coupés, robot qui décroche) :
                # une valeur qui ne bouge plus du tout ou hors limites n'est pas un doigt.
                if self.sur_chatouille:
                    reelles = list(self.mini.get_current_joint_positions()[1])
                    figees = figees + 1 if reelles == precedentes else 0
                    precedentes = reelles
                    fiable = figees < 3 and all(abs(r) < math.pi for r in reelles)
                    # le balancement normal va jusqu'à ±AMPLITUDE_ANTENNES : on le retire de l'écart
                    ecart = max(abs(r) for r in reelles) - self.AMPLITUDE_ANTENNES
                    ecarte = ecarte + 1 if fiable and ecart > self.SEUIL_CHATOUILLE else 0
                    if (ecarte >= 6 and now - debut_repos > 1.5
                            and now - derniere_chatouille > 3):
                        derniere_chatouille, ecarte = now, 0
                        self.sur_chatouille()
            except Exception:
                pass
            time.sleep(0.05)


# ---------- Commandes vocales directes (sans passer par Gemma : instantané) ----------

def normaliser(texte: str) -> str:
    """minuscules, sans accents ni ponctuation : « Réveille-toi ! » -> « reveille toi »"""
    t = unicodedata.normalize("NFD", texte.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


# Gestes déclenchés par une phrase courte. (motif, geste, réponse)
COMMANDES_GESTES = [
    (r"\bdanse\b|fais (une|la) danse", "danse", "C'est parti !"),
    (r"hoche la tete|dis oui", "oui", "Oui !"),
    (r"secoue la tete|dis non", "non", "Non !"),
    (r"regarde (a|vers la) gauche", "gauche", ""),
    (r"regarde (a|vers la) droite", "droite", ""),
    (r"regarde en haut|leve la tete", "haut", ""),
    (r"regarde en bas|baisse la tete", "bas", ""),
    (r"\bsalue\b|fais coucou|dis bonjour", "coucou", "Coucou !"),
    (r"tourne toi|fais un tour|\bpivote\b", "tourne", ""),
    (r"bouge (tes|les) antennes", "content", ""),
]
MOTS_DODO = r"\bva (dormir|te coucher)\b|\bdodo\b|\bendors toi\b|\bmode veille\b|\btais toi\b|\bchut\b"
MOTS_REVEIL = r"reveille toi|\bdebout\b|\bc est l heure\b"
MOTS_OUBLI = r"oublie tout|nouvelle conversation|on recommence a zero"
REPLIQUES_CHATOUILLE = ["Hihi, ça chatouille !", "Arrête, je suis super chatouilleux !",
                        "Encore, encore !", "Hé, mes antennes sont sensibles !", "Hahaha, stop !"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modele", default="gemma4:e4b")
    ap.add_argument("--sans-robot", action="store_true", help="micro/haut-parleur du PC")
    ap.add_argument("--host", default="reachy-mini.local",
                    help="adresse du robot Wireless (ex. 192.168.43.120 si .local ne marche pas)")
    ap.add_argument("--whisper", default="small", help="tiny, base, small, medium…")
    ap.add_argument("--whisper-gpu", action="store_true", help="Whisper sur la carte graphique")
    ap.add_argument("--seuil", type=float, default=0.02, help="sensibilité du micro")
    ap.add_argument("--immobile", action="store_true", help="pas de mouvements au repos")
    ap.add_argument("--volume", type=float, default=2.5,
                    help="puissance de la voix : 1 = normal, 2.5 = fort, 4 = très fort")
    args = ap.parse_args()

    print("Chargement de Whisper et de la voix…")
    stt = WhisperModel(args.whisper,
                       device="cuda" if args.whisper_gpu else "cpu",
                       compute_type="float16" if args.whisper_gpu else "int8")
    voix = PiperVoice.load(ICI / "voix" / "fr_FR-siwis-medium.onnx")

    mini = None
    if args.sans_robot:
        audio = AudioPC()
    else:
        from reachy_mini import ReachyMini
        mini = ReachyMini(host=args.host, timeout=15.0)
        mini.enable_motors()  # wake_up() ne les rallume pas : après un redémarrage du robot ils sont coupés
        mini.wake_up()
        audio = AudioRobot(mini)
    gestes = Gestes(mini, args.host)
    chatouille = threading.Event()
    repos = Repos(None if args.immobile else mini, sur_chatouille=chatouille.set, host=args.host)
    if mini:  # charge les émotions en fond pour que la 1re ne fasse pas attendre
        threading.Thread(target=Gestes.bibliotheque, daemon=True).start()

    def dire(texte: str):
        texte = nettoyer(texte)
        if not texte:
            return
        print(f"🤖 {texte}")
        chunks = list(voix.synthesize(texte))
        son = np.concatenate([c.audio_float_array for c in chunks])
        if args.volume != 1.0:
            # gain + saturation douce : plus fort sans grésiller (la voix est déjà à 100 % en crête)
            son = np.tanh(son * args.volume) / np.tanh(args.volume)
        audio.jouer(son.astype(np.float32), chunks[0].sample_rate)

    # Mesure le bruit ambiant 1 s pour régler le seuil de détection de voix
    audio.ouvrir_micro()
    blocs, t0 = [], time.time()
    while time.time() - t0 < 1.0:
        b = audio.lire()
        if len(b):
            blocs.append(np.sqrt(np.mean((b - b.mean()) ** 2)))
    audio.fermer_micro()
    bruit = float(np.median(blocs)) if blocs else 0.0
    seuil = max(args.seuil, bruit * 3)
    print(f"Bruit ambiant {bruit:.3f}, seuil {seuil:.3f}")
    if bruit > 0.2:
        print("⚠️  Micro très bruité ou mauvais micro sélectionné (vérifie la source dans les réglages son).")

    historique = [{"role": "system", "content": SYSTEME}]
    etat = {"endormi": False}

    def commande(texte: str) -> bool:
        """Exécute une commande vocale directe. Renvoie True si la phrase en était une."""
        t = normaliser(texte)
        if etat["endormi"]:
            if re.search(MOTS_REVEIL, t):
                etat["endormi"] = False
                if mini:
                    mini.enable_motors()
                    mini.wake_up()
                dire("Me revoilà ! Je t'écoute.")
            return True  # endormi : on ignore tout le reste
        if re.search(MOTS_DODO, t):
            dire("D'accord, je fais une sieste. Dis « réveille-toi » quand tu veux.")
            etat["endormi"] = True
            if mini:
                mini.goto_sleep()
            return True
        if re.search(MOTS_OUBLI, t):
            historique[:] = historique[:1]
            gestes.faire("surpris")
            dire("C'est fait, j'ai tout oublié. On repart de zéro !")
            return True
        if re.search(r"quelle heure", t):
            n = datetime.now()
            dire(f"Il est {n.hour} heures {n.minute:02d}." if n.minute else f"Il est {n.hour} heures pile.")
            return True
        if re.search(r"quel jour|quelle date|on est le combien", t):
            n = datetime.now()
            dire(f"On est {JOURS[n.weekday()]} {n.day} {MOIS[n.month - 1]} {n.year}.")
            return True
        if len(t.split()) <= 8:  # phrase courte = ordre, pas une question à Gemma
            for motif, nom, reponse in COMMANDES_GESTES:
                if re.search(motif, t):
                    print(f"   💃 [{nom}]")
                    geste = threading.Thread(target=gestes.faire, args=(nom,), daemon=True)
                    geste.start()
                    if reponse:
                        dire(reponse)
                    geste.join()
                    return True
        return False

    dire("Salut ! Je t'écoute.")

    try:
        while True:
            if not etat["endormi"]:
                if args.immobile:
                    gestes.ecoute()
                else:
                    repos.activer()
            print("💤 Je dors (dis « réveille-toi »)…" if etat["endormi"] else "🎤 Parle…")
            enreg = ecouter(audio, seuil, interruption=chatouille)
            repos.pause()
            if chatouille.is_set():
                chatouille.clear()
                print("   🪶 chatouille !")
                reaction = threading.Thread(target=gestes.faire,
                                            args=(random.choice(["rire", "content", "amour"]),), daemon=True)
                reaction.start()
                dire(random.choice(REPLIQUES_CHATOUILLE))
                reaction.join()
                continue
            if len(enreg) == 0:
                continue
            segments, _ = stt.transcribe(enreg, language="fr", vad_filter=True)
            # écarte les segments où Whisper n'est pas sûr qu'il y ait de la parole
            texte = " ".join(s.text for s in segments
                             if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0).strip()
            if not texte or any(h in texte.lower() for h in HALLUCINATIONS):
                continue
            print(f"🧑 {texte}")

            if any(m in texte.lower() for m in MOTS_FIN):
                dire("Au revoir, à bientôt !")
                gestes.faire("coucou")
                break
            if commande(texte):
                continue

            message = {"role": "user", "content": texte}
            if mini and any(m in texte.lower() for m in MOTS_VISION):
                jpeg = mini.media.get_frame_jpeg()
                if jpeg:
                    message["images"] = [base64.b64encode(jpeg).decode()]
            historique.append(message)

            gestes.reflechit()
            en_cours = None
            for phrase in phrases_gemma(args.modele, historique):
                for nom in re.findall(r"\[(\w+)\]", phrase):
                    nom = nom.lower()
                    if nom in GESTES:
                        if en_cours:
                            en_cours.join()
                        print(f"   💃 [{nom}]")
                        en_cours = threading.Thread(target=gestes.faire, args=(nom,), daemon=True)
                        en_cours.start()
                dire(re.sub(r"\[\w+\]", "", phrase))
            if en_cours:
                en_cours.join()
            historique[:] = historique[:1] + historique[-12:]  # mémoire courte
            # les images prennent de la place : on ne les garde pas dans l'historique
            for m in historique:
                m.pop("images", None)
            gestes.neutre()
    except KeyboardInterrupt:
        pass
    finally:
        if mini:
            mini.disable_wobbling()
            mini.goto_sleep()
            mini.__exit__(None, None, None)


if __name__ == "__main__":
    main()
