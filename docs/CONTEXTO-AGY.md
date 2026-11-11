<!--
Cache manual de contexto do motor agy (Gemini). Medido em 09/11/2026: o agy não tem cache de prompt neste login
(cache_read_tokens = 0, mesmo repetindo o pedido), e o gasto alto vem de ele explorar arquivos em vários passos,
reenviando tudo a cada passo. Este arquivo vai pronto no início da PRIMEIRA mensagem de cada sessão nova do agy
(motor_agy.py, via contexto.py), para ele não precisar explorar.

Quem edita: você (ou o Claude, quando pedir) à mão, junto com a retrospectiva da semana. O bloco entre as marcas auto
é reescrito sozinho por `python contexto.py --atualiza` (e pelo servidor, depois de cada retrospectiva), só com
números calculados em código. NÃO entra texto do Haiku aqui: a retrospectiva é "fedora-log" e não volta para prompts.
Tamanho máximo que vai ao agy: contexto.MAX_CHARS. Comentários HTML como este NÃO vão ao agy.

Revisão manual: 2026-11-09
-->

## Regras fixas
- Nunca desligar, reiniciar, suspender nem apagar de vez. Valem a lista DENY (runtime.py) e as PC_RULES (commands.py). Não afrouxe nenhuma.
- Apagar é sempre mandar para a Lixeira, depois de confirmar com o usuário.
- Testes offline: tools\testa.ps1. Depois de mexer num módulo, rode o autoteste dele: `.venv\Scripts\python.exe <modulo>.py`.
- Windows 11 (branch master). A branch linux está congelada.

## Mapa do projeto (use isto no lugar de explorar)
- server.py: servidor HTTP local, liga tudo. ui/index.html: a janela (HUD, chat, orbe).
- runtime.py: roda o Claude headless e traduz os eventos; guarda DENY. motor_agy.py: o mesmo para o agy. motor_regra.py: qual motor responde (Claude, ou agy acima de 55% na semana / 90% na sessão de 5 h).
- commands.py: comandos /barra, PC_RULES e contexto do vault. atalhos.py + atalhos.md: respostas rápidas por código (Gemini flash-lite escolhe o atalho). conversa.py: sessão e economia de tokens do turno.
- listener.py (ouvido, push-to-talk), vad.py, whisper_server.py (STT local), speech.py (voz Piper), orb.py (buraco negro flutuante), sysinfo.py (números da máquina).
- memory.py: o vault Obsidian como grafo. notes.py: notas diárias. tidy.py: limpa texto ditado. llm.py: chamadas pequenas à API do Gemini.
- turnolog.py: um registro por turno. retro.py: retrospectiva semanal no vault.
- agentes.py + agentes/*.md: equipe (techlead, junior, writer, design, arquiteto, senior, qa), independente do motor; despachante.py: vigia o backlog no vault e aciona a equipe; docs/EQUIPE.md e docs/MOTORES.md explicam. dupla.py: terminal único com as duas assinaturas.

## Como trabalhar sem gastar à toa
- Abra só o arquivo do módulo citado no pedido. runtime.py e retro.py são grandes (700+ linhas): procure com grep e leia por trechos.
- Não explore: graphify-out/ (só o GRAPH_REPORT.md se precisar de visão geral), logs/, modelos/, __pycache__/, .venv/.
- Pedido que não é de código (clima, abrir app, nota): responda direto, sem ler o projeto.

<!-- auto:inicio -->
## Mapa do Graphify (calculado em código)
- Atenção: o mapa foi gerado no commit 5047d0bb e o projeto está em 3ef566e; confira nos arquivos antes de confiar.
Áreas principais: vad.py, time, Atalhos rápidos, listener.py, notes.py, runtime.py, Ouvido (listener), Orbe flutuante, speech.py, whisper_server.py, llm.py, retro.py, dupla.py, EQUIPE.md, motor_agy.py, commands.py, agentes.py, compara_modelos.py, sys.
Funções e classes mais ligadas (comece por elas):
- `_teste_corpo()` - 21 edges
- `executa()` - 21 edges
- `fold()` - 20 edges
- `_teste()` - 17 edges
- `Ear` - 17 edges
- `_rodar()` - 17 edges
- `Handler` - 16 edges
- `_teste_lote_e_qa()` - 16 edges
<!-- auto:fim -->
