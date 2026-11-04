# Atalhos da Fedora

O Ollama lê este arquivo para escolher o atalho de cada pedido; o código em `atalhos.py` executa.
Pode editar os exemplos à vontade. Atalho novo aqui sem código lá vai para o Claude.

## Rápidos (resposta direta, sem Claude)

- `hora`: diz a hora certa. Ex.: "que horas são", "me diz as horas", "fala a hora aí"
- `data`: diz o dia de hoje. Ex.: "que dia é hoje", "hoje é dia quanto", "que dia da semana é hoje"
- `abrir_pasta` (arg = nome da pasta): Downloads, Documentos, Imagens, Músicas, Vídeos, Área de trabalho, pasta pessoal, Projetos, ou outra pasta da pasta pessoal. Ex.: "abre meus downloads", "abre a pasta projetos"
- `abrir_app` (arg = nome do app): abre um programa instalado. Ex.: "abre o chrome", "abre o dolphin", "abre o obsidian", "abre a calculadora"
- `volume` (arg = número de 0 a 100, "mais", "menos" ou "mudo"): volume da caixa de som. Ex.: "volume 40", "aumenta o volume", "abaixa o som", "muta"
- `sistema`: uso de RAM e placa de vídeo. Ex.: "como está o sistema", "quanto de memória estou usando"
- `descarregar_modelo`: tira o modelo do Ollama da placa de vídeo. Ex.: "descarrega o modelo", "libera a placa de vídeo"
- `desligar_ollama`: para o serviço do Ollama (volta quando a janela da Fedora abrir de novo). Ex.: "desliga o ollama"

## Pela janela (já instantâneos, não passam por aqui)

- notas diárias, ditar, organizar, salvar, voltar, desfazer

## Pelo Claude (precisam pensar, pesquisar ou confirmar)

- `nenhum`: qualquer outra coisa. Perguntas, contas, pesquisa na web (clima, notícias, preços), escrever ou criar arquivo, mover ou renomear arquivo, fechar app (pergunta "confirma?" antes), tarefas com mais de um passo.
- Apagar não é atalho: só pelo Claude, para a Lixeira e com confirmação.
