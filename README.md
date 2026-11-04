# Fedora

Assistente pessoal local (diga “Ok, Fedora”): um buraco negro que ouve, com o sistema e o vault orbitando,
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
| `listener.py` | ouvido sempre ligado: microfone (PipeWire) → detecção de fala → whisper; só reage a “Ok, Fedora” |
| `speech.py` | voz: Piper (pt_BR faber) tocando direto na caixa de som com pw-play |
| `voice.py` | (legado) áudio do navegador → whisper.cpp |
| `ui/index.html` | a interface inteira (buraco negro + `#notas`) |

## Serviços (systemd de usuário)

| Serviço | O quê |
|---|---|
| `whisper.service` | whisper.cpp `small` carregado em 127.0.0.1:8178 |
| `ollama.service` | Ollama em 127.0.0.1:11434, RX 580 via Vulkan, modelo `gemma3:4b` |
| `fedora-assistente.service` | este servidor (porta 8720) + o ouvido |

Ligar tudo no login: `systemctl --user enable --now whisper ollama fedora-assistente`
Abrir a janela: menu de apps → **Fedora** (Brave em modo app).

## Usar

- “Ok, Fedora, <pedido>” de uma vez, ou “Ok, Fedora” e o pedido depois.
  Sem a frase, o áudio é descartado e nunca vai pro disco.
- Tocar no buraco negro / Espaço = falar sem dizer o nome. “mic ligado” desliga o microfone de verdade.
- “notas diárias” abre o editor; lá: “ditar”, “organizar”, “salvar”, “voltar”. O resto vai pro Claude.
- Se a janela estiver fechada, “Ok, Fedora” abre ela e entrega o pedido.
