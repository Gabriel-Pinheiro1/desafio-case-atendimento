"""Gera um lote de teste "novo" a partir dos dados originais, para provar que o app aceita dados novos.

- sorteia ~500 conversas (clientes inteiros, para manter recontatos coerentes);
- troca todos os IDs (conversa, cliente, pedido, mensagem) e desloca as datas em +91 dias;
- injeta sujeira típica: linhas duplicadas, canal/marca escritos fora do padrão, valor com vírgula,
  data no formato brasileiro e separador ';' no arquivo de pedidos.

Uso: python gerar_lote_teste.py  -> grava em lote_teste/
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
OUT = BASE / "lote_teste"
DESLOC = pd.Timedelta(days=91)
rng = np.random.default_rng(42)

conv = pd.read_csv(BASE / "dados/conversas.csv", dtype=str, keep_default_na=False)
msgs = pd.read_csv(BASE / "dados/mensagens.csv", dtype=str, keep_default_na=False)
peds = pd.read_csv(BASE / "dados/pedidos.csv", dtype=str, keep_default_na=False)

clientes = conv.customer_id.drop_duplicates().sample(frac=1, random_state=7)
escolhidos, total = [], 0
for c in clientes:
    k = (conv.customer_id == c).sum()
    if total + k > 500:
        continue
    escolhidos.append(c); total += k
    if total >= 495:
        break
conv = conv[conv.customer_id.isin(escolhidos)].copy()
msgs = msgs[msgs.conversation_id.isin(conv.conversation_id)].copy()
peds = peds[peds.order_id.isin(conv.order_id)].copy()

novo_id = lambda s, a, b: s.str.replace(a, b, regex=False)
for df in (conv, msgs):
    df["conversation_id"] = novo_id(df.conversation_id, "CV9", "CN7")
for df in (conv, peds):
    df["customer_id"] = novo_id(df.customer_id, "CL4", "CZ8")
    df["order_id"] = df.order_id.str.replace(r"^(G[BC])81", r"\g<1>93", regex=True)
msgs["message_id"] = novo_id(msgs.message_id, "M", "N")
msgs["texto"] = msgs.texto.str.replace(r"\b(G[BC])81(\d{5})\b", r"\g<1>93\2", regex=True)

shift = lambda s, fmt: (pd.to_datetime(s) + DESLOC).dt.strftime(fmt)
conv["data_abertura"] = shift(conv.data_abertura, "%Y-%m-%d %H:%M:%S")
msgs["timestamp"] = shift(msgs.timestamp, "%Y-%m-%d %H:%M:%S")
peds["data_pedido"] = shift(peds.data_pedido, "%Y-%m-%d")

# sujeira proposital
idx = conv.sample(40, random_state=1).index
conv.loc[idx, "canal"] = conv.loc[idx, "canal"].map({"whatsapp": "WhatsApp", "email": "E-mail", "reclame_aqui": "Reclame Aqui"})
idx = conv[conv.marca.isin(["Ápice", "Rituária"])].sample(15, random_state=2).index
conv.loc[idx, "marca"] = conv.loc[idx, "marca"].str.replace("Á", "A").str.replace("á", "a")
idx = conv.sample(10, random_state=3).index
conv.loc[idx, "data_abertura"] = pd.to_datetime(conv.loc[idx, "data_abertura"]).dt.strftime("%d/%m/%Y %H:%M")
conv = pd.concat([conv, conv.sample(8, random_state=4)])          # 8 conversas duplicadas
msgs = pd.concat([msgs, msgs.sample(25, random_state=5)])         # 25 mensagens duplicadas
peds["valor"] = peds.valor.str.replace(".", ",", regex=False)     # decimal com vírgula

OUT.mkdir(exist_ok=True)
conv.to_csv(OUT / "conversas.csv", index=False)
msgs.to_csv(OUT / "mensagens.csv", index=False)
peds.to_csv(OUT / "pedidos.csv", index=False, sep=";")
print(f"lote_teste/: {len(conv)} conversas (8 duplicadas), {len(msgs)} mensagens (25 duplicadas), {len(peds)} pedidos")
