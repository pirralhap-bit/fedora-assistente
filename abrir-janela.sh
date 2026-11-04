#!/bin/sh
# Abre a janela da Fedora assim que o servidor responde (no login ele pode demorar alguns segundos).
for i in $(seq 60); do curl -sf -o /dev/null http://localhost:8720/ && break; sleep 1; done
exec brave-browser --app=http://localhost:8720/
