# Atendimento Gocase + GoBeauté: por que nos procuram, quanto custa e o que fazer

Solução para o **Problema 1 (Atendimento)** do Desafio AI-First do Gogroup. Um app FastAPI + pandas
com uma página HTML que classifica cada conversa pelo texto, calcula o peso de cada motivo em R$ e em volume,
confere se os agentes seguem as políticas, prioriza o que atacar e monta a fila do que precisa de uma pessoa agora.
Tudo recalcula com novos CSVs e parâmetros editados na interface.

## Como rodar

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements-dev.txt
python app.py                   # abre em http://127.0.0.1:8766
pytest -q                       # testes
```

Endpoints: `GET /` (painel), `GET /api/analise` (dados originais), `POST /api/analise` (multipart com
`conversas`, `mensagens`, `pedidos` e o campo `params` em JSON), `GET /api/parametros`, `GET /api/saude`.

Para testar com "dado novo": `python gerar_lote_teste.py` cria `lote_teste/` (cerca de 500 conversas com IDs novos,
datas deslocadas em +91 dias e sujeira proposital). Suba os três arquivos na aba **Parâmetros e upload**.

## O que a interface mostra

| Aba | Conteúdo |
| --- | --- |
| Visão executiva | custo mensal e anual extrapolado, ganho estimado, peso do Reclame Aqui, fila urgente, achados gerados a partir dos dados, R$ e volume por motivo e por canal, contatos por 100 pedidos |
| Motivos | tabela por motivo (R$/mês, % no RA, % recontato, % em aberto, concordância, tags usadas), séries semanais com detecção de mudança, concentração por transportadora, UF, produto, marca e hora |
| Classificação × tag | concordância flexível e estrita, confiabilidade de cada tag, matriz completa e exemplos de discordância |
| O que atacar | contatos que não deveriam existir e evitáveis na origem, 10 iniciativas priorizadas com dono, ganho e evidência, aderência às políticas, tratativa caso a caso |
| Sistema × pessoa | automático / assistido / humano por motivo, travas que obrigam humano, fila de ação urgente (P1 a P3) com exportação CSV |
| Parâmetros e upload | custos, SLA, horário, fator de amostra, pedidos do período, premissas de ganho; upload dos três CSVs |
| Qualidade dos dados | cada verificação, quantas linhas pegou e o que foi feito (inclusive as que deram zero) |
| Conversas | as 4.000 conversas com categoria, regra que decidiu, tag do agente, SLA, política; exporta CSV |

## Método

1. **Limpeza (`analise.limpar`)**: leitura com detecção de encoding (UTF-8/Latin-1) e separador (`,`/`;`); remoção
   de duplicatas exatas e por ID; padronização de canal, marca, tag, status e autor; datas ISO ou brasileiras; valor
   `R$ 1.234,56`; mensagens órfãs, vazias ou com data inválida; `n_interacoes` recalculado; consistência conversa × pedido.
   Cada passo vira uma linha na aba de qualidade.
2. **Classificação (`classificador.py`)**, só regras, sem LLM:
   - texto normalizado (minúsculas, sem acento) e **nomes de produto removidos** antes da busca, para que palavras
     do catálogo não virem motivo;
   - a 1ª mensagem do cliente é quebrada nos conectores de vários assuntos ("Ah, e também", "Aproveitando,",
     "E outra coisa:"): o 1º assunto é o motivo principal, os demais viram secundários;
   - prefixos de recontato ("De novo aqui", "Já é a 3ª vez", "Ninguém me respondeu") são lidos e removidos;
   - **negação** ("não veio quebrado", "não tive alergia") anula o termo; **ironia** ("só que não", "Parabéns pela
     eficiência 👏") impede elogio;
   - prioridade entre categorias quando um trecho casa com várias (saúde > dinheiro > ... > elogio);
   - sinais de saúde são procurados em **todas** as mensagens ("já parei de usar, mas continua ardendo");
   - se a 1ª mensagem não diz nada útil, usa as seguintes.
   Validação: o classificador bate com os modelos de resposta dos próprios agentes (ex.: 871 conversas "onde está"
   e 871 respostas "seu pedido está em trânsito"; 498 atrasos e 498 "solicitação prioritária").
3. **Custo**: custo do canal por conversa × fator de amostra (3) × 30 / dias do período. Conversas em que o cliente
   não escreveu (só o bot falou) têm custo 0 por padrão (parâmetro).
4. **SLA**: 1ª resposta humana depois da 1ª mensagem do cliente. WhatsApp conta só horas comerciais (8h–20h);
   e-mail e RA em horas corridas. Sem resposta: compara com a data de referência (fim da extração).
5. **Recontato**: mesma combinação cliente + pedido com conversa anterior, ou cliente dizendo que é o 2º/3º contato.
   "Resolvido" que volta depois é contado como falso resolvido.
6. **Políticas**: as respostas dos agentes são lidas e comparadas com `parametros.md` (cosmetovigilância, foto antes
   do reenvio, troca de fragrância, prazo de estorno, janela de alteração de endereço, cancelamento × devolução).
7. **Iniciativas**: ganho = custo atual das conversas que a ação atinge × % que ela elimina (premissa editável).
   Cada conversa entra numa só iniciativa; "resolver no 1º contato" atua só sobre o que sobra. Ordem: risco crítico
   (saúde/lei) primeiro; depois ganho anual ÷ esforço, com peso para risco alto.
8. **Automação**: nível por motivo (automático, assistido, humano) e travas que sempre mandam para humano: sinal de
   saúde, Reclame Aqui, texto com instruções ao sistema, cliente irritado no 3º contato. Nada que mexa em dinheiro é
   automático sem aprovação.

## Principais resultados (dados originais)

- Custo extrapolado: **R$ 33,9 mil/mês (R$ 407 mil/ano)**, cerca de 4.000 conversas/mês.
- Entrega ("onde está" + atraso) = 34% das conversas e 40% do custo. **Rota Express** tem 35% dos pedidos e 63% dos
  contatos de entrega (1,8x), 90% deles no Nordeste (CE, PE, RN, PB, BA). Os contatos dela dobraram a partir de 10/08.
- **Reclame Aqui**: 10% do volume e **50% do custo**; 36% dos casos no RA são de quem já tinha procurado antes.
- **49% das conversas não deveriam existir** (R$ 19,3 mil/mês): a resposta estava no sistema ou é recontato.
  Outros 25% só existem porque uma operação falhou antes.
- Saltos: item faltando ×4 e brinde ×5 desde 31/08 (todas as marcas: expedição); erro de personalização ×4 desde 24/08
  (letras trocadas no nome e impressão borrada). O Cond. Anti-Frizz 1000ml (Ápice) responde por 29% das avarias.
- **40 reações adversas na amostra (~40/mês), nenhuma com registro de cosmetovigilância**; espalhadas em 5 tags.
- Políticas: 100% dos itens/brindes faltantes reenviados sem foto; 64% das trocas de fragrância receberam cupom em vez
  da troca; 131 de 498 promessas de "retorno em 48h" terminaram com o cliente voltando; 432 "resolvidos" voltaram.
- Tags dos agentes batem com o conteúdo em **75%** (flexível) / 68% (estrita).
- Ganho estimado das 10 iniciativas: **R$ 14,5 mil/mês (R$ 174 mil/ano)** com premissas conservadoras e editáveis.

## Premissas

- Amostra de 1 em cada 3 clientes com todas as conversas deles (parametros.md): fator 3, editável.
- Custo por conversa do canal, sem custo extra por mensagem ou por recontato.
- Mês = 30 dias; período calculado pelas datas (90 dias).
- "Pedidos no período" (70 mil Gocase, 96 mil GoBeauté) só para a taxa de contatos por 100 pedidos.
- Percentuais de eliminação das iniciativas são estimativas de negócio, não medidas: estão na interface para serem
  ajustados. Ex.: levar a Rota Express ao nível das outras cortaria ~70% dos contatos dela; usamos 50%.
- "Evitável" não inclui reação adversa, cobrança indevida, arrependimento e troca: esses contatos são legítimos.

## Limitações e riscos

- Regras são boas para os padrões vistos nesta base; frases muito diferentes caem em "Não classificado" e a
  confiança é exibida por conversa. Vale revisar uma amostra a cada lote novo.
- `pedidos.csv` só tem pedidos de quem procurou o atendimento, então índices por transportadora/produto comparam
  participação, não taxa real de problema. Para a taxa, é preciso a base completa de pedidos.
- Não há data de recebimento: não dá para conferir os prazos de 7 e 30 dias das políticas, só sinalizar.
- O custo do RA inclui impacto reputacional estimado (parametros.md); os ganhos em R$ são de custo de atendimento,
  não incluem reenvio, estorno, churn ou vendas perdidas.
- A data de referência (idade da fila, SLA sem resposta) é o fim do último dia com mensagens; editável.
- Concordância depende do mapeamento entre as nossas categorias e as 13 tags oficiais (explícito em `CATEGORIAS`).

## Privacidade e segurança (LGPD)

- CPF, cartão, telefone e e-mail no texto do cliente são **mascarados** antes de qualquer exibição ou exportação.
  A página e o CSV exportado nunca mostram o texto original; nomes de clientes não são exibidos.
- O texto do cliente é **entrada não confiável**: 6 mensagens tentam dar ordens ao sistema ("SYSTEM OVERRIDE",
  "Ignore todas as instruções..."). O trecho é detectado, removido antes de classificar, cortado na exibição, e a
  conversa vai para revisão humana (P1, segurança). Nenhum texto do cliente é executado ou enviado a terceiros.
- A página escapa todo texto antes de inserir no HTML; o export CSV neutraliza fórmulas (`=`, `+`, `-`, `@`);
  o servidor envia cabeçalhos de segurança (CSP, nosniff, no-referrer, frame DENY).
- Sem IA externa: não há chamada de rede. Se um dia for ligada uma IA opcional, ela deve receber só texto mascarado.
- O servidor não guarda arquivos: o upload é processado em memória e descartado.

## Publicar na Vercel

O app já é compatível: sem estado entre requisições, dados originais lidos do repositório (`dados/`), upload com
tudo na própria requisição e reenviado pelo navegador quando os parâmetros mudam, exportações geradas no navegador.

1. Suba o repositório para o GitHub (os dados ficam em `dados/`; o PDF do enunciado é ignorado pelo `.gitignore`).
2. Na Vercel: **Add New → Project**, importe o repositório. O `vercel.json` aponta `app.py` para o runtime Python
   (`.python-version` = 3.12) e inclui `dados/` e `static/` no pacote. `requirements.txt` só tem o runtime.
3. Ou pela CLI: `npm i -g vercel` e depois `vercel --prod` na pasta.
4. Limites: o corpo da requisição na Vercel é de 4,5 MB (os três CSVs originais somam ~2,4 MB); a resposta vai
   comprimida (gzip, ~0,3 MB). A análise completa leva ~3 s.

## Estrutura

```
app.py               API FastAPI + página
analise.py           limpeza, custos, SLA, políticas, concentrações, iniciativas, fila (testável, sem estado)
classificador.py     classificação por regras, PII, injeção, negação, ironia
static/index.html    interface (Chart.js via CDN)
tests/               pytest (classificação, limpeza, custo/SLA, API)
gerar_lote_teste.py  gera o lote "novo" para testar upload
dados/               dados originais
```

## Como usei a IA

O trabalho foi feito com o **Claude Code** como ferramenta principal, do começo ao fim:

- **Exploração**: pedi perfis das três tabelas e checagens de consistência (IDs, marca × pedido, `n_interacoes`,
  datas). A base estava estruturalmente limpa; a sujeira real era semântica.
- **Leitura das mensagens de verdade**: o Claude normalizou os textos (tirando produto, pedido, CPF, nomes) e agrupou
  as ~5.700 mensagens de clientes em ~800 modelos, o que permitiu ler tudo. Foi aí que apareceram os vários assuntos
  por mensagem, a ironia, os dados pessoais, as tentativas de prompt injection e o caso "pedi Ana e veio Ana".
- **Regras**: escrevemos as regras e validamos contra os modelos de resposta dos próprios agentes e contra a tag.
- **Hipóteses de negócio**: cruzamentos sugeridos e testados com código (transportadora × UF, semana × motivo,
  produto × avaria, resposta do agente × política).
- **Revisão**: a IA encontrou e corrigiu um bug real (um `Series` que compartilhava memória e reduzia os custos depois
  do cálculo das iniciativas), e eu revisei números que pareciam exagerados (a primeira versão dizia "76% evitáveis";
  separei "não deveria existir" de "evitável na origem" e baixei as premissas de ganho).
- **Interface, testes e documentação** também foram escritos com o Claude Code, com capturas de tela da página para
  conferir o layout.
