# Fedora

Assistente pessoal local (diga “Ok, Fedora”): um buraco negro que ouve, com o sistema e o vault orbitando,
e uma página de notas diárias com ditado. O cérebro é o `claude` CLI (assinatura, sem
chave de API); a transcrição é o whisper.cpp; a reescrita do ditado é o Ollama. Tudo
roda em `127.0.0.1`.

Base: fork de jarvis-claude-code (98b1972), com a interface trocada.

## Rodar

```bash
./deploy/instalar.sh   # instala e liga os serviços (whisper, ollama, fedora-assistente)
./tools/testa.sh       # roda o autoteste de cada módulo
```

Configuração em `.env` (vault, permissão do Claude, porta, modelo do Ollama, endereço do whisper); dependências do `.venv` em `requirements.txt`.

## Peças

| Arquivo | O que faz |
|---|---|
| `server.py` | servidor HTTP local; só aceita `localhost` e exige token nos POST |
| `runtime.py` | chama `claude -p` (acesso total; a lista `DENY` veta desligar o PC e apagar por `rm`/`dd`/`shred`) |
| `sysinfo.py` | CPU, RAM (`/proc`), GPU/VRAM da AMD (sysfs), modelos do Ollama |
| `notes.py` | lista, lê e salva daily notes; recusa salvar se a nota mudou fora (Obsidian) |
| `tidy.py` | reorganiza a nota diária inteira pelas seções (Ollama), sem perder linha |
| `atalhos.py` + `atalhos.md` | pedidos rápidos sem Claude: o qwen3 escolhe o atalho, o código executa |
| `modelo.py` | qwen3 carregado na placa enquanto a janela está aberta |
| `vad.py` | Silero VAD: decide o que é fala |
| `listener.py` | ouvido sempre ligado: microfone (PipeWire) → Silero → whisper; só reage a “Ok, Fedora” |
| `speech.py` | voz: Piper (pt_BR faber) tocando direto na caixa de som com pw-play |
| `ui/index.html` | a interface inteira (buraco negro + `#notas`) |

## Serviços (systemd de usuário)

| Serviço | O quê |
|---|---|
| `whisper.service` | whisper.cpp `medium` na placa (Vulkan) em 127.0.0.1:8178 |
| `ollama.service` | Ollama em 127.0.0.1:11434, RX 580 via Vulkan, modelo `qwen3:8b` |
| `fedora-assistente.service` | este servidor (porta 8720) + o ouvido |

Os arquivos dos serviços e atalhos ficam em `deploy/` (cópia do que está instalado em `~/.config`).
Abrir a janela: menu de apps → **Fedora** (Chrome em modo app); abre sozinha no login.

## Usar

- “Ok, Fedora, <pedido>” de uma vez, ou “Ok, Fedora” e o pedido depois.
  Sem a frase, o áudio é descartado e nunca vai pro disco.
- Tocar no buraco negro / Espaço = falar sem dizer o nome. “mic ligado” desliga o microfone de verdade.
- “notas diárias” abre o editor; lá: “ditar”, “organizar”, “salvar”, “voltar”. O resto vai pro Claude.
- Se a janela estiver fechada, “Ok, Fedora” abre ela e entrega o pedido.
