# Reachy Mini

SDK `reachy-mini` 1.13.0, Python 3.12, venv dans `.venv`.

## Installation (une fois)

```bash
bash install.sh
```

Le script installe GStreamer (sudo), ajoute ton user au groupe `dialout`, crée `.venv` et installe le SDK + simulateur MuJoCo.

Reachy Mini **Wireless** : lancer aussi `bash install_wireless.sh` (compile le plugin GStreamer `webrtcsrc`, nécessaire pour le micro, le haut-parleur et la caméra du robot). Le PC et le robot doivent être sur le même Wi-Fi : le Wi-Fi de l'école (IONIS) bloque, utiliser un partage de connexion.

## Interface web (le plus simple)

```bash
cd ~/reachy-mini && .venv/bin/python interface.py
```

Ouvre http://localhost:8765 : curseurs tête / antennes / corps, gestes, réveil / veille, moteurs,
« bouger à la main », volume, faire parler le robot, lancer / arrêter l'assistant vocal et voir son journal.
Ne lance pas `lancer.sh` en même temps que l'assistant de l'interface (sinon deux voix).

Émotions : 85 mouvements enregistrés par Pollen Robotics (dataset Hugging Face
`pollen-robotics/reachy-mini-emotions-library`, téléchargé une fois dans `~/.cache/huggingface`).
Gemma les déclenche avec des tags (`[rire]`, `[triste]`, `[colere]`…). Danses : `reachy_mini_dances_library`.
Au repos, l'assistant fait respirer le robot et le fait regarder autour de lui (`--immobile` pour couper).
Chatouilles : pendant qu'il écoute, pousser une antenne du doigt (> 25° pendant 0,2 s) le fait rire
(`Repos.SEUIL_CHATOUILLE` dans `04_assistant.py`). La tête n'a pas de capteur fiable pour ça.

## Lancer à la main

Deux terminaux :

1. Le daemon (le "cerveau" qui parle aux moteurs) :
   - robot branché en USB (version Lite) : `.venv/bin/reachy-mini-daemon`
   - sans robot, en simulation : `.venv/bin/reachy-mini-daemon --sim`
   - version Wireless : rien à lancer, le daemon tourne sur le robot (`reachy-mini.local`)
2. Un script : `.venv/bin/python 01_bonjour.py`

Dashboard web du daemon : http://localhost:8000

## Scripts

| Fichier | Ce qu'il fait |
|---|---|
| `01_bonjour.py` | Oui/non de la tête, antennes, rotation du corps |
| `02_suivi_visage.py` | La tête suit ton visage |
| `03_photo.py` | Photo depuis la caméra du robot |
| `04_assistant.py` | Assistant vocal local : Whisper → Gemma (Ollama) → voix Piper FR |

### Assistant vocal

Tout tourne sur le PC, rien ne part sur internet. Ollama doit tourner (`ollama serve`).

```bash
.venv/bin/python 04_assistant.py --sans-robot      # test avec le micro/haut-parleur du PC
.venv/bin/python 04_assistant.py                   # avec le robot (daemon lancé)
.venv/bin/python 04_assistant.py --modele qwen3.5:4b
```

- Dis « regarde » / « tu vois » : Gemma reçoit la photo de la caméra.
- Dis « au revoir » pour quitter.
- `--seuil 0.05` si le micro se déclenche tout seul, `--whisper base` pour plus de vitesse.
- Voix : `voix/fr_FR-siwis-medium.onnx` (Piper, téléchargée depuis Hugging Face).

## API utile

- `mini.goto_target(head=create_head_pose(x, y, z, roll, pitch, yaw, mm=True), antennas=[d, g], body_yaw=rad, duration=s)`
- `mini.wake_up()` / `mini.goto_sleep()`
- `mini.look_at_image(u, v)` : regarder un pixel de la caméra
- `mini.start_head_tracking()` : suivi de visage intégré
- `mini.media.get_frame()` (numpy BGR), `get_frame_jpeg()`, `play_sound(fichier)`, `start_recording()` + `get_audio_sample()` (micro)
- `mini.start_recording()` / `stop_recording()` : enregistrer un mouvement
- `mini.disable_motors()` / `enable_gravity_compensation()` : bouger la tête à la main
- Apps : `ReachyMiniApp` + `reachy-mini-app-assistant` pour créer une app publiable sur un Space Hugging Face
