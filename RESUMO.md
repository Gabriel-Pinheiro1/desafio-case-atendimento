# Resumo: Atendimento Gocase + GoBeauté (Problema 1)

**Problema.** ~4.000 conversas/mês em WhatsApp, e-mail e Reclame Aqui, categorizadas à mão. A liderança não sabe
por que os clientes procuram nem o que fazer. Construí um app que classifica cada conversa pelo texto (regras, sem IA
externa), põe peso em R$ por motivo, confere as políticas e prioriza ações. Recalcula com novos CSVs e parâmetros.

**Números (extrapolados da amostra 1:3, por mês)**

| | |
| --- | --- |
| Custo do atendimento | **R$ 33,9 mil/mês** (R$ 407 mil/ano) |
| Entrega (onde está + atraso) | 34% das conversas, **40% do custo** |
| Reclame Aqui | 10% do volume, **50% do custo**; 36% já tinham procurado antes |
| Não deveriam existir | **49% das conversas** (R$ 19,3 mil/mês): resposta já estava no sistema ou é recontato |
| Recontato do mesmo pedido | 19%; 432 "resolvidos" voltaram; 131 de 498 promessas de "retorno em 48h" falharam |
| Tag do agente bate com o texto | 75% (estrita 68%) |
| Reação adversa | ~40/mês, **0 com registro de cosmetovigilância** |

**Onde está o problema.** Rota Express: 35% dos pedidos e 63% dos contatos de entrega, 90% no Nordeste, dobrando desde
10/08. Item faltando ×4 e brinde ×5 desde 31/08 em todas as marcas (expedição). Erro de personalização ×4 desde 24/08
(letras trocadas, impressão borrada). Cond. Anti-Frizz 1000ml (Ápice) = 29% das avarias.

**Recomendações (critério: risco crítico primeiro; depois ganho anual ÷ esforço)**

| # | Ação | Dono | Ganho/mês |
| --- | --- | --- | --- |
| 1 | Protocolo de cosmetovigilância + tag "Reação adversa" | Responsável técnica + atendimento | obrigatório |
| 2 | Rota Express no NE: cobrar SLA ou redistribuir | Logística | R$ 4,5 mil |
| 3 | Resolver no 1º contato: follow-up das promessas, não fechar sem solução | Liderança de atendimento | R$ 2,1 mil |
| 4 | Conferência de itens e brinde na expedição | Operações/CD + Marketing | R$ 1,8 mil |
| 5 | Conciliação automática pix/boleto e trava de cobrança dupla | Financeiro + Tecnologia | R$ 1,3 mil |
| 6 | Autoatendimento (endereço, cancelamento, cupom, FAQ) | E-commerce | R$ 1,7 mil |
| 7 | Rastreio proativo + bot com status real | CX Digital | R$ 1,4 mil |
| 8-10 | Fragrâncias (cumprir a troca), embalagem do Anti-Frizz, QA da personalização | Marcas, Qualidade, Produção | R$ 1,8 mil |

Total estimado: **R$ 14,5 mil/mês (R$ 174 mil/ano)**, sem dupla contagem e com premissas editáveis (ex.: Rota Express
reduz 50% dos contatos, não os ~70% que levariam ao nível das outras).

**Caso a caso (hoje).** Fila de 554 casos: 104 P1 (40 de saúde, 6 com texto tentando manipular o sistema, 58 cobranças
indevidas em aberto), depois Reclame Aqui, endereço ainda alterável e clientes no 3º contato.

**Sistema × pessoa.** 44% automatizável (rastreio, dúvidas, cupom, pagamento, endereço), 41% assistido (sistema coleta
foto/checa regra, humano aprova), 15% humano obrigatório (saúde, cobrança, Reclame Aqui, manipulação).

**Limites.** Regras cobrem os padrões desta base; pedidos.csv só tem quem procurou (índices de concentração, não taxas);
sem data de recebimento, os prazos de 7/30 dias não são verificáveis; ganhos são de custo de atendimento, sem reenvio,
estorno ou churn. Dados pessoais mascarados; texto do cliente tratado como não confiável.
