# Jarvis

Assistente pessoal local: um buraco negro que ouve, com o sistema e o vault orbitando,
e uma página de notas diárias com ditado. O cérebro é o `claude` CLI (assinatura, sem
chave de API); a transcrição é o whisper.cpp; a reescrita do ditado é o Ollama. Tudo
roda em `127.0.0.1`.

Base: fork de jarvis-claude-code (98b1972), com a interface trocada.

## Rodar

```bash
./start.sh          # abre em http://localhost:8720
```

Configuração em `.env` (vault, permissão do Claude, porta, modelo do Ollama, whisper).

## Peças

| Arquivo | O que faz |
|---|---|
| `server.py` | servidor HTTP local; só aceita `localhost` e exige token nos POST |
| `runtime.py` | chama `claude -p` (permissão `default`, nunca `bypassPermissions`) |
| `sysinfo.py` | CPU, RAM (`/proc`), GPU/VRAM da AMD (sysfs), modelos do Ollama |
| `notes.py` | lista, lê e salva daily notes; recusa salvar se a nota mudou fora (Obsidian) |
| `tidy.py` | manda o trecho ditado ao Ollama e devolve Markdown |
| `voice.py` | áudio do navegador → ffmpeg → whisper.cpp |
| `ui/index.html` | a interface inteira (buraco negro + `#notas`) |

## Usar

- Tela principal: toque no buraco negro ou aperte Espaço, fale, e pare de falar.
  "notas diárias" abre o editor; o resto vai pro Claude.
- Notas (`#notas`): o buraco negro pequeno dita no cursor; "Ollama: organizar"
  reescreve só o trecho ditado (ou o selecionado); Ctrl+S salva.

## Falta

- whisper.cpp + modelo (sem ele a voz não transcreve; o texto digitado funciona).
- Ollama + modelo (sem ele "organizar" avisa que está desligado).
- Palavra de ativação "Jarvis" sempre ouvindo (openWakeWord). Hoje é tocar/Espaço.
