#!/usr/bin/env bash
# Runs every module's self-test: ./tools/testa.sh  (atalhos and tidy need Ollama with the model loaded, i.e. the window open)
cd "$(dirname "$0")/.." || exit 1
falhou=0
for m in listener vad speech modelo tidy atalhos; do
  if out=$(.venv/bin/python3 "$m.py" 2>&1); then echo "ok     $m"; else echo "FALHOU $m"; echo "$out" | tail -5; falhou=1; fi
done
exit $falhou
