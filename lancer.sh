#!/usr/bin/env bash
# Lance l'assistant vocal de Reachy Mini.
#   ./lancer.sh                 # normal
#   ./lancer.sh --modele qwen3.5:4b
# Relance tout seul si la connexion Wi-Fi avec le robot coupe. Ctrl+C pour arrêter.
cd "$(dirname "$(readlink -f "$0")")"

HOST="reachy-mini.local"
for i in "$@"; do [ "$prev" = "--host" ] && HOST="$i"; prev="$i"; done

# 1. Ollama (Gemma)
if ! curl -s -m3 http://localhost:11434/api/tags >/dev/null; then
  echo "❌ Ollama ne répond pas. Lance-le avec : ollama serve"
  exit 1
fi

# 2. Robot
echo "🔎 Recherche du robot ($HOST)…"
until curl -s -m3 "http://$HOST:8000/api/daemon/status" >/dev/null; do
  echo "   Robot introuvable. Vérifie qu'il est allumé et sur le même Wi-Fi que le PC (partage de connexion)."
  echo "   Nouvel essai dans 5 s (Ctrl+C pour quitter)…"
  sleep 5
done
echo "✅ Robot trouvé."

# 3. Assistant, relancé s'il plante
trap 'echo; echo "👋 Arrêt."; exit 0' INT
while true; do
  .venv/bin/python -u 04_assistant.py "$@" 2>&1 \
    | grep --line-buffered -vE "WARNING|Warning|self.media_manager|No Reachy Mini Audio USB|INFO:|^Traceback|^  File|^    "
  code=${PIPESTATUS[0]}
  [ "$code" -eq 0 ] && break  # « au revoir »
  echo "⚠️  Connexion perdue avec le robot. Relance dans 3 s…"
  sleep 3
  until curl -s -m3 "http://$HOST:8000/api/daemon/status" >/dev/null; do sleep 3; done
done
