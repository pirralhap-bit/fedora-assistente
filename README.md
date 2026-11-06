# Fedora

Assistente pessoal local (diga “Ok, Fedora”): um buraco negro que ouve, com o sistema e o vault orbitando,
e uma página de notas diárias com ditado. O cérebro é o `claude` CLI (assinatura, sem
chave de API); a transcrição é o whisper.cpp; a reescrita do ditado e a escolha dos atalhos rápidos são o Gemini (API do Google, `GEMINI_API_KEY` no `.env`). Tudo
roda em `127.0.0.1`.

Base: fork de jarvis-claude-code (98b1972), com a interface trocada.

## Rodar

```bash
./deploy/instalar.sh   # instala e liga os serviços (whisper, fedora-assistente)
./tools/testa.sh       # roda o autoteste de cada módulo
```

Configuração em `.env` (vault, permissão do Claude, porta, chave e modelos do Gemini, endereço do whisper); dependências do `.venv` em `requirements.txt`.

## Peças

| Arquivo | O que faz |
|---|---|
| `server.py` | servidor HTTP local; só aceita `localhost` e exige token nos POST |
| `runtime.py` | chama `claude -p` (acesso total; a lista `DENY` veta desligar o PC e apagar por `rm`/`dd`/`shred`) |
| `sysinfo.py` | CPU, RAM (`/proc`), GPU/VRAM da AMD (sysfs), status da última chamada ao Gemini |
| `notes.py` | lista, lê e salva daily notes; recusa salvar se a nota mudou fora (Obsidian) |
| `tidy.py` | reorganiza a nota diária inteira pelas seções (Gemini), sem perder linha |
| `llm.py` | chamada à API do Gemini (JSON), usada por `tidy.py` e `atalhos.py` |
| `atalhos.py` + `atalhos.md` | pedidos rápidos sem Claude: o Gemini (flash-lite) escolhe o atalho, o código executa |
| `vad.py` | Silero VAD: decide o que é fala |
| `listener.py` | ouvido sempre ligado: microfone (PipeWire) → Silero → whisper; só reage a “Ok, Fedora” |
| `speech.py` | voz: Piper (pt_BR faber) tocando direto na caixa de som com pw-play |
| `ui/index.html` | a interface inteira (buraco negro + `#notas`) |

## Serviços (systemd de usuário)

| Serviço | O quê |
|---|---|
| `whisper.service` | whisper.cpp `medium` na placa (Vulkan) em 127.0.0.1:8178 |
| `fedora-assistente.service` | este servidor (porta 8720) + o ouvido |

Os arquivos dos serviços e atalhos ficam em `deploy/` (cópia do que está instalado em `~/.config`).
Abrir a janela: menu de apps → **Fedora** (Chrome em modo app); abre sozinha no login.
Modo gamer: `tools/modo-gamer.sh liga` para os três serviços e fecha a janela; `tools/modo-gamer.sh desliga` traz tudo de volta.

## Usar

- “Ok, Fedora, <pedido>” de uma vez, ou “Ok, Fedora” e o pedido depois.
  Sem a frase, o áudio é descartado e nunca vai pro disco.
- Tocar no buraco negro / Espaço = falar sem dizer o nome. “mic ligado” desliga o microfone de verdade.
- “notas diárias” abre o editor; lá: “ditar”, “organizar”, “salvar”, “voltar”. O resto vai pro Claude.
- Se a janela estiver fechada, “Ok, Fedora” abre ela e entrega o pedido.
