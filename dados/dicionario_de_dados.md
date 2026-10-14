# Dicionário de dados: Atendimento

Todos os dados são sintéticos. Nomes, documentos e contatos que aparecem nas mensagens são fictícios.

## conversas.csv (uma linha por conversa)

| Campo | Descrição |
| --- | --- |
| conversation_id | Identificador da conversa |
| customer_id | Identificador do cliente |
| order_id | Pedido associado à conversa, quando houver |
| marca | Marca do pedido (Gocase ou marcas GoBeauté) |
| canal | whatsapp, email ou reclame_aqui |
| data_abertura | Data e hora de abertura da conversa |
| tag_agente | Categoria atribuída pelo agente ao encerrar |
| status | resolvido, pendente, reaberto ou sem_resposta_cliente |
| n_interacoes | Número de mensagens na conversa, incluindo as do bot |

## mensagens.csv (uma linha por mensagem)

| Campo | Descrição |
| --- | --- |
| conversation_id | Conversa a que a mensagem pertence |
| message_id | Identificador da mensagem |
| autor | cliente, agente ou bot (assistente virtual no WhatsApp e resposta automática no e-mail; o Reclame Aqui não tem bot) |
| timestamp | Data e hora da mensagem |
| texto | Conteúdo da mensagem |

## pedidos.csv (pedidos dos clientes que abriram contato)

| Campo | Descrição |
| --- | --- |
| order_id | Identificador do pedido (GB = GoBeauté, GC = Gocase) |
| customer_id | Identificador do cliente |
| marca | Marca |
| data_pedido | Data do pedido |
| valor | Valor pago (R$) |
| sku, descricao_produto | Produto principal do pedido |
| transportadora | Transportadora responsável |
| uf | Estado de entrega |
| status_entrega | Situação logística na data de extração |
