"""Motor de análise do atendimento Gocase + GoBeauté.

Sem estado e sem rede: recebe os três CSVs (caminho, bytes ou DataFrame) e um dicionário de
parâmetros, e devolve um dicionário pronto para JSON. Toda chamada recalcula tudo.
"""
from __future__ import annotations

import io
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from classificador import (
    CATEGORIAS, TAGS_OFICIAIS, classificar_conversa, detectar_pii, mascarar, normalizar, sem_acento,
)

BASE = Path(__file__).resolve().parent
DADOS = BASE / "dados"

# ---------------------------------------------------------------------------
# Parâmetros de negócio (valores de parametros.md) e premissas editáveis
# ---------------------------------------------------------------------------
DEFAULT_PARAMS: dict = {
    "custo_canal": {"whatsapp": 4.80, "email": 7.20, "reclame_aqui": 42.00},
    "sla_horas": {"whatsapp": 0.5, "email": 24.0, "reclame_aqui": 48.0},
    # WhatsApp: SLA conta só no horário comercial; e-mail e RA em horas corridas
    "sla_so_horario_comercial": {"whatsapp": True, "email": False, "reclame_aqui": False},
    "hora_abertura": 8,
    "hora_fechamento": 20,
    "fator_amostra": 3.0,           # amostra de 1 em cada 3 clientes
    "dias_periodo": None,           # None = calcula pelas datas das conversas
    "data_referencia": None,        # None = dia seguinte à última mensagem (data de extração)
    "pedidos_periodo": {"Gocase": 70000, "GoBeauté": 96000},
    "custo_conversa_vazia_pct": 0.0,  # conversa em que o cliente nem escreveu (só o bot falou)
    "prazo_cosmetovigilancia_horas": 24,
    # Premissas de ganho (editáveis): % dos contatos que a ação elimina
    "taxas_iniciativas": {
        "rota_express": 0.50,       # levar a Rota Express ao nível das outras cortaria ~70%; usamos 50%
        "rastreio_proativo": 0.30,  # aviso ativo de status/atraso para as demais transportadoras
        "expedicao": 0.50,          # voltar ao patamar de antes de 31/08 cortaria ~75% do recente
        "avaria": 0.30,
        "personalizacao": 0.50,     # voltar ao patamar de antes de 24/08 cortaria ~75% do recente
        "pagamentos": 0.40,
        "autoatendimento": 0.30,    # adoção parcial de autoatendimento
        "fragrancia": 0.20,
        "primeiro_contato": 0.40,   # aplicado só sobre o custo que sobrou depois das outras ações
        "cosmetovigilancia": 0.0,
    },
    # Automação: % do custo da conversa economizado quando o sistema resolve sozinho / prepara para o humano
    "economia_automacao": {"automatico": 0.70, "assistido": 0.30, "humano": 0.0},
}

COLUNAS = {
    "conversas": ["conversation_id", "customer_id", "order_id", "marca", "canal", "data_abertura", "tag_agente", "status", "n_interacoes"],
    "mensagens": ["conversation_id", "message_id", "autor", "timestamp", "texto"],
    "pedidos": ["order_id", "customer_id", "marca", "data_pedido", "valor", "sku", "descricao_produto", "transportadora", "uf", "status_entrega"],
}
OBRIGATORIAS = {
    "conversas": ["conversation_id", "canal", "data_abertura"],
    "mensagens": ["conversation_id", "autor", "timestamp", "texto"],
    "pedidos": ["order_id"],
}

MARCAS = ["Gocase", "Ápice", "Barbour's", "Kokeshi", "Rituária", "Lescent", "By Samia", "Auá", "Yenzah"]
_MARCA_CHAVE = {re.sub(r"[^a-z]", "", normalizar(m)): m for m in MARCAS}

CANAIS = {
    "whatsapp": "whatsapp", "wpp": "whatsapp", "zap": "whatsapp", "whats": "whatsapp", "wa": "whatsapp",
    "email": "email", "e-mail": "email", "e_mail": "email", "mail": "email",
    "reclame_aqui": "reclame_aqui", "reclameaqui": "reclame_aqui", "reclame aqui": "reclame_aqui", "ra": "reclame_aqui",
}
STATUS = {"resolvido", "pendente", "reaberto", "sem_resposta_cliente"}
AUTORES = {"cliente": "cliente", "agente": "agente", "bot": "bot", "atendente": "agente", "sistema": "bot", "customer": "cliente"}
_TAG_CHAVE = {normalizar(t): t for t in TAGS_OFICIAIS}

NOME_CANAL = {"whatsapp": "WhatsApp", "email": "E-mail", "reclame_aqui": "Reclame Aqui"}


def mesclar_params(params: dict | None) -> dict:
    p = deepcopy(DEFAULT_PARAMS)
    for k, v in (params or {}).items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            for k2, v2 in v.items():
                p[k][k2] = v2
        else:
            p[k] = v
    # coerção numérica defensiva (a interface manda strings às vezes)
    for grupo in ("custo_canal", "sla_horas", "taxas_iniciativas", "economia_automacao", "pedidos_periodo"):
        for k2, v2 in list(p[grupo].items()):
            try:
                p[grupo][k2] = float(v2)
            except (TypeError, ValueError):
                p[grupo][k2] = DEFAULT_PARAMS[grupo].get(k2, 0.0)
    for k in ("fator_amostra", "custo_conversa_vazia_pct", "hora_abertura", "hora_fechamento", "prazo_cosmetovigilancia_horas"):
        try:
            p[k] = float(p[k])
        except (TypeError, ValueError):
            p[k] = DEFAULT_PARAMS[k]
    if p["fator_amostra"] <= 0:
        p["fator_amostra"] = 1.0
    if p.get("dias_periodo") in ("", 0, "0"):
        p["dias_periodo"] = None
    if p.get("data_referencia") in ("",):
        p["data_referencia"] = None
    return p


# ---------------------------------------------------------------------------
# Leitura robusta de CSV
# ---------------------------------------------------------------------------
def ler_csv(fonte) -> pd.DataFrame:
    """Aceita caminho, bytes, arquivo ou DataFrame. Detecta encoding e separador."""
    if isinstance(fonte, pd.DataFrame):
        df = fonte.copy()
    else:
        if isinstance(fonte, (str, Path)):
            raw = Path(fonte).read_bytes()
        elif isinstance(fonte, (bytes, bytearray)):
            raw = bytes(fonte)
        else:
            raw = fonte.read()
        texto = None
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                texto = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        primeira = texto.splitlines()[0] if texto else ""
        sep = ";" if primeira.count(";") > primeira.count(",") else ","
        df = pd.read_csv(io.StringIO(texto), sep=sep, dtype=str, keep_default_na=False, on_bad_lines="skip")
    df.columns = [re.sub(r"\s+", "_", sem_acento(str(c)).strip().lower()) for c in df.columns]
    return df.astype(str).apply(lambda s: s.str.strip())


def _validar_colunas(df: pd.DataFrame, tabela: str) -> None:
    faltando = [c for c in OBRIGATORIAS[tabela] if c not in df.columns]
    if faltando:
        raise ValueError(f"{tabela}.csv: faltam as colunas obrigatórias {faltando}. Colunas recebidas: {list(df.columns)}")
    for c in COLUNAS[tabela]:
        if c not in df.columns:
            df[c] = ""


def _parse_datas(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip().replace({"": None, "nan": None, "NaT": None})
    iso = pd.to_datetime(s, errors="coerce", format="mixed", dayfirst=False)
    # datas no formato brasileiro (dd/mm/aaaa) que o parser ISO não entende ou inverte
    br_mask = s.fillna("").str.match(r"^\d{1,2}/\d{1,2}/\d{4}")
    if br_mask.any():
        iso.loc[br_mask] = pd.to_datetime(s[br_mask], errors="coerce", dayfirst=True, format="mixed")
    return iso


def _parse_valor(s: pd.Series) -> pd.Series:
    t = s.astype(str).str.replace(r"[R$\s]", "", regex=True)
    tem_virgula = t.str.contains(",")
    t = t.where(~tem_virgula, t.str.replace(".", "", regex=False).str.replace(",", ".", regex=False))
    return pd.to_numeric(t, errors="coerce")


def _canon_marca(valor: str, order_id: str = "") -> str:
    chave = re.sub(r"[^a-z]", "", normalizar(valor))
    if chave in _MARCA_CHAVE:
        return _MARCA_CHAVE[chave]
    if chave.startswith("barbour"):
        return "Barbour's"
    if not chave and str(order_id).upper().startswith("GC"):
        return "Gocase"
    return valor.strip() or "(sem marca)"


def grupo_marca(marca: str) -> str:
    return "Gocase" if marca == "Gocase" else "GoBeauté"


# ---------------------------------------------------------------------------
# Limpeza (cada passo vira uma linha do relatório de qualidade)
# ---------------------------------------------------------------------------
def limpar(conv: pd.DataFrame, msgs: pd.DataFrame, peds: pd.DataFrame):
    log: list[dict] = []

    def reg(tabela, verificacao, n, acao, tipo="limpeza"):
        log.append({"tabela": tabela, "verificacao": verificacao, "linhas": int(n), "acao": acao, "tipo": tipo})

    for df, nome in ((conv, "conversas"), (msgs, "mensagens"), (peds, "pedidos")):
        _validar_colunas(df, nome)
    brutos = {"conversas": len(conv), "mensagens": len(msgs), "pedidos": len(peds)}

    # --- conversas
    n = conv.duplicated().sum(); conv = conv.drop_duplicates()
    reg("conversas", "Linhas exatamente duplicadas", n, "removidas")
    vazio = conv.conversation_id.eq("")
    reg("conversas", "Sem conversation_id", vazio.sum(), "removidas"); conv = conv[~vazio]
    n = conv.conversation_id.duplicated().sum(); conv = conv.drop_duplicates("conversation_id", keep="first")
    reg("conversas", "conversation_id repetido com conteúdo diferente", n, "mantida a primeira ocorrência")

    canal_raw = conv.canal.map(lambda v: normalizar(v).replace(" ", "_") if normalizar(v) not in CANAIS else normalizar(v))
    canal = canal_raw.map(lambda v: CANAIS.get(v, CANAIS.get(v.replace("_", " "), v)))
    n = (canal != conv.canal).sum(); conv = conv.assign(canal=canal)
    reg("conversas", "Canal escrito fora do padrão (maiúsculas, 'e-mail', 'Reclame Aqui'...)", n, "padronizado")
    desconhecido = ~conv.canal.isin(["whatsapp", "email", "reclame_aqui"])
    reg("conversas", "Canal desconhecido", desconhecido.sum(), "mantido como 'outro' e custo zero", "alerta")
    conv.loc[desconhecido, "canal"] = "outro"

    marca = [_canon_marca(m, o) for m, o in zip(conv.marca, conv.order_id)]
    n = (pd.Series(marca, index=conv.index) != conv.marca).sum(); conv = conv.assign(marca=marca)
    reg("conversas", "Marca escrita fora do padrão (sem acento, caixa diferente)", n, "padronizada")

    tag = conv.tag_agente.map(lambda t: _TAG_CHAVE.get(normalizar(t), t.strip()))
    n = (tag != conv.tag_agente).sum(); conv = conv.assign(tag_agente=tag)
    reg("conversas", "Tag do agente escrita fora do padrão", n, "padronizada")
    fora = ~conv.tag_agente.isin(TAGS_OFICIAIS + [""])
    reg("conversas", "Tag fora da lista oficial", fora.sum(), "mantida, conta como discordância", "alerta")

    st = conv.status.map(lambda s: normalizar(s).replace(" ", "_"))
    n = (st != conv.status).sum(); conv = conv.assign(status=st)
    reg("conversas", "Status escrito fora do padrão", n, "padronizado")

    conv = conv.assign(dt_abertura=_parse_datas(conv.data_abertura))
    msgs = msgs.assign(dt=_parse_datas(msgs.timestamp))

    # --- mensagens
    n = msgs.duplicated(["conversation_id", "message_id", "autor", "timestamp", "texto"]).sum()
    msgs = msgs.drop_duplicates(["conversation_id", "message_id", "autor", "timestamp", "texto"])
    reg("mensagens", "Mensagens exatamente duplicadas", n, "removidas")
    dup2 = msgs.message_id.ne("") & msgs.duplicated("message_id", keep="first")
    reg("mensagens", "message_id repetido", dup2.sum(), "mantida a primeira"); msgs = msgs[~dup2]
    dup3 = msgs.duplicated(["conversation_id", "autor", "timestamp", "texto"])
    reg("mensagens", "Mesmo texto, autor e horário com IDs diferentes (envio duplicado)", dup3.sum(), "removidas"); msgs = msgs[~dup3]
    autor = msgs.autor.map(lambda a: AUTORES.get(normalizar(a), normalizar(a)))
    n = (autor != msgs.autor).sum(); msgs = msgs.assign(autor=autor)
    reg("mensagens", "Autor escrito fora do padrão", n, "padronizado")
    vaz = msgs.texto.str.strip().eq("")
    reg("mensagens", "Mensagens sem texto", vaz.sum(), "removidas"); msgs = msgs[~vaz]
    orf = ~msgs.conversation_id.isin(conv.conversation_id)
    reg("mensagens", "Mensagens de conversas que não existem em conversas.csv", orf.sum(), "removidas"); msgs = msgs[~orf]
    sem_ts = msgs.dt.isna()
    reg("mensagens", "Data/hora inválida", sem_ts.sum(), "removidas"); msgs = msgs[~sem_ts]

    # data de abertura inválida: usa a primeira mensagem
    sem_dt = conv.dt_abertura.isna()
    primeira = msgs.groupby("conversation_id").dt.min()
    conv.loc[sem_dt, "dt_abertura"] = conv.loc[sem_dt, "conversation_id"].map(primeira)
    reg("conversas", "Data de abertura inválida ou vazia", sem_dt.sum(), "substituída pela 1ª mensagem")
    sem_dt2 = conv.dt_abertura.isna()
    reg("conversas", "Sem data e sem mensagens", sem_dt2.sum(), "removidas"); conv = conv[~sem_dt2]

    m2 = msgs.merge(conv[["conversation_id", "dt_abertura"]], on="conversation_id")
    antes = (m2.dt < m2.dt_abertura - pd.Timedelta(minutes=1)).sum()
    reg("mensagens", "Mensagem com horário anterior à abertura da conversa", antes, "mantida; abertura recalculada", "alerta")
    if antes:
        conv["dt_abertura"] = conv[["conversation_id", "dt_abertura"]].apply(
            lambda r: min(r.dt_abertura, primeira.get(r.conversation_id, r.dt_abertura)), axis=1)

    cont = msgs.groupby("conversation_id").size()
    n_real = conv.conversation_id.map(cont).fillna(0).astype(int)
    n_inf = pd.to_numeric(conv.n_interacoes, errors="coerce")
    reg("conversas", "n_interacoes diferente do número real de mensagens", (n_inf != n_real).sum(), "usado o número real")
    conv = conv.assign(n_msgs=n_real)
    semmsg = n_real.eq(0)
    reg("conversas", "Conversa sem nenhuma mensagem", semmsg.sum(), "mantida como 'sem mensagem'", "alerta")

    # --- pedidos
    n = peds.duplicated().sum(); peds = peds.drop_duplicates()
    reg("pedidos", "Linhas exatamente duplicadas", n, "removidas")
    n = peds.order_id.duplicated().sum(); peds = peds.drop_duplicates("order_id", keep="first")
    reg("pedidos", "order_id repetido", n, "mantida a primeira ocorrência")
    peds = peds.assign(valor_num=_parse_valor(peds.valor))
    ruim = peds.valor_num.isna() | (peds.valor_num <= 0)
    reg("pedidos", "Valor vazio, inválido, zero ou negativo", ruim.sum(), "valor ignorado nas somas", "alerta")
    peds.loc[ruim, "valor_num"] = np.nan
    peds = peds.assign(marca=[_canon_marca(m, o) for m, o in zip(peds.marca, peds.order_id)],
                       dt_pedido=_parse_datas(peds.data_pedido),
                       status_entrega=peds.status_entrega.map(lambda s: normalizar(s).replace(" ", "_")),
                       uf=peds.uf.str.upper().str.strip(),
                       transportadora=peds.transportadora.str.strip())

    sem_ped = conv.order_id.ne("") & ~conv.order_id.isin(peds.order_id)
    reg("conversas", "order_id que não existe em pedidos.csv", sem_ped.sum(), "mantida sem dados do pedido", "alerta")
    x = conv.merge(peds[["order_id", "marca", "customer_id"]], on="order_id", how="inner", suffixes=("", "_p"))
    reg("conversas", "Marca da conversa diferente da marca do pedido", (x.marca != x.marca_p).sum(), "vale a do pedido", "alerta")
    reg("conversas", "Cliente da conversa diferente do cliente do pedido", (x.customer_id != x.customer_id_p).sum(), "mantido; sinalizado", "alerta")
    if (x.marca != x.marca_p).any():
        mp = peds.set_index("order_id").marca
        conv["marca"] = [mp.get(o, m) for o, m in zip(conv.order_id, conv.marca)]
    abre_antes = x.merge(peds[["order_id", "dt_pedido"]], on="order_id")
    abre_antes = (abre_antes.dt_abertura.dt.normalize() < abre_antes.dt_pedido).sum()
    reg("conversas", "Conversa aberta antes da data do pedido", abre_antes, "mantida; sinalizado", "alerta")

    finais = {"conversas": len(conv), "mensagens": len(msgs), "pedidos": len(peds)}
    return conv.reset_index(drop=True), msgs.reset_index(drop=True), peds.reset_index(drop=True), log, brutos, finais


# ---------------------------------------------------------------------------
# SLA
# ---------------------------------------------------------------------------
def horas_comerciais(ini: datetime, fim: datetime, h0: float, h1: float) -> float:
    if fim <= ini:
        return 0.0
    total = 0.0
    dia = datetime(ini.year, ini.month, ini.day)
    while dia <= fim:
        a = max(ini, dia + timedelta(hours=h0))
        b = min(fim, dia + timedelta(hours=h1))
        if b > a:
            total += (b - a).total_seconds()
        dia += timedelta(days=1)
    return total / 3600.0


def calcular_sla(canal: str, t_cliente, t_agente, data_ref, params: dict) -> tuple[float | None, str]:
    """Retorna (horas até a 1ª resposta humana, situação)."""
    if t_cliente is None or pd.isna(t_cliente):
        return None, "sem mensagem do cliente"
    limite = params["sla_horas"].get(canal)
    if limite is None:
        return None, "canal sem SLA"
    comercial = params["sla_so_horario_comercial"].get(canal, False)
    h0, h1 = params["hora_abertura"], params["hora_fechamento"]
    fim = t_agente if (t_agente is not None and not pd.isna(t_agente)) else data_ref
    horas = horas_comerciais(t_cliente.to_pydatetime(), fim.to_pydatetime(), h0, h1) if comercial \
        else (fim - t_cliente).total_seconds() / 3600.0
    if t_agente is None or pd.isna(t_agente):
        return horas, ("sem resposta: SLA estourado" if horas > limite else "sem resposta: ainda no prazo")
    return horas, ("no prazo" if horas <= limite else "fora do prazo")


# ---------------------------------------------------------------------------
# Ações do agente e aderência às políticas
# ---------------------------------------------------------------------------
ACOES = [
    ("chamado_transportadora", r"chamado com a transportadora"),
    ("promessa_retorno", r"te retorno em ate|retorno em ate \d+ ?h"),
    ("reenvio", r"reenvio"),
    ("pede_foto", r"(enviar|mandar|envie|mande) (uma )?foto"),
    ("nova_producao", r"producao de uma peca nova|nova producao|refazer"),
    ("encaminha_financeiro", r"encaminhar para o financeiro|financeiro verificar"),
    ("cancelamento", r"solicitar o cancelamento|cancelamento"),
    ("informacao_produto", r"informacoes do produto|especificacoes do produto"),
    ("troca", r"passo a passo da troca|troca por outro|realizar a troca|fazer a troca"),
    ("cupom_no_lugar_da_troca", r"cupom para experimentar outra fragrancia"),
    ("verifica_cupom", r"verificar o cupom"),
    ("verifica_endereco", r"verificar se ainda e possivel alterar"),
    ("orienta_suspensao", r"suspender o uso"),
    ("registro_cosmetovigilancia", r"cosmetovigilancia|responsavel tecnica|numero do lote|\blote\b|notificacao sanitaria"),
    ("informa_prazo_estorno", r"estorno .{0,40}(faturas?|dias uteis)"),
    ("agradece", r"obrigada pelo carinho"),
]
_ACOES_RE = [(k, re.compile(p)) for k, p in ACOES]
RE_PROMESSA_H = re.compile(r"ate (\d+) ?h")


def acoes_agente(textos_agente: list[str]) -> list[str]:
    t = " | ".join(normalizar(x) for x in textos_agente)
    return [k for k, rx in _ACOES_RE if rx.search(t)]


def avaliar_politica(cat: str, saude: bool, acoes: list[str], frt_h, status_entrega: str, alegacao_sem_div: bool, params,
                     foto_sem_retorno: bool = False) -> tuple[str, str]:
    """Retorna (situação, explicação). situação: conforme | nao_conforme | atencao | n/a."""
    a = set(acoes)
    if cat == "reacao_adversa" or saude:
        if "registro_cosmetovigilancia" in a and frt_h is not None and frt_h <= params["prazo_cosmetovigilancia_horas"]:
            return "conforme", "Registro de cosmetovigilância feito no prazo"
        if not acoes:
            return "nao_conforme", "Reação adversa sem nenhuma resposta humana"
        motivo = "Sem registro de cosmetovigilância (produto, lote, fotos) e sem acionar a responsável técnica"
        if "troca" in a:
            motivo += "; ofereceu troca, que pela política não substitui o registro"
        return "nao_conforme", motivo
    if cat == "insatisfacao":
        if "cupom_no_lugar_da_troca" in a:
            return "nao_conforme", "Ofereceu cupom no lugar da troca prevista (1 troca em até 30 dias)"
        if "troca" in a:
            return "conforme", "Ofereceu a troca prevista na política"
        return ("atencao", "Sem resposta humana") if not acoes else ("atencao", "Não ofereceu a troca prevista")
    if cat in ("item_faltando", "brinde", "avaria"):
        if "reenvio" in a and "pede_foto" not in a:
            return "nao_conforme", "Reenvio prometido sem pedir a foto exigida pela política"
        if "pede_foto" in a and foto_sem_retorno:
            return "atencao", "Cliente mandou a foto e ficou sem retorno"
        if "pede_foto" in a:
            return "conforme", "Pediu foto antes do reenvio"
        return ("atencao", "Sem resposta humana") if not acoes else ("atencao", "Sem tratativa clara de reenvio")
    if cat == "personalizacao":
        if alegacao_sem_div and "nova_producao" in a:
            return "atencao", "Nova produção aprovada, mas o próprio relato não mostra divergência"
        if "nova_producao" in a and "pede_foto" in a:
            return "conforme", "Pediu foto e acionou nova produção sem custo"
        return ("atencao", "Sem resposta humana") if not acoes else ("atencao", "Sem nova produção")
    if cat == "endereco":
        if status_entrega in ("em_transito", "entregue", "atrasado"):
            return "atencao", f"Pedido já saiu ({status_entrega}): alteração impossível; a resposta poderia ser imediata"
        if status_entrega == "em_separacao":
            return "atencao", "Pedido em separação: alterar já, antes que saia"
        return "conforme", "Pedido ainda não separado: alteração possível"
    if cat in ("cancelamento", "arrependimento") and status_entrega == "entregue" and "cancelamento" in a:
        return "atencao", "Pedido já entregue: não é cancelamento, é devolução (arrependimento só até 7 dias do recebimento)"
    if cat == "cobranca_indevida":
        if "informa_prazo_estorno" in a:
            return "conforme", "Informou o prazo de estorno"
        return ("atencao", "Sem resposta humana") if not acoes else ("atencao", "Encaminhou ao financeiro sem informar o prazo de estorno")
    return "n/a", ""


# ---------------------------------------------------------------------------
# Matriz de automação
# ---------------------------------------------------------------------------
AUTOMACAO = {
    "rastreio": ("automatico", "Bot consulta o pedido e responde status, transportadora e previsão; abre o chamado com a transportadora sozinho se estiver parado."),
    "duvida_produto": ("automatico", "Bot responde com a ficha técnica do produto (FAQ por SKU)."),
    "duvida_personalizacao": ("automatico", "Bot informa prazo de produção e regras de alteração."),
    "cupom_promocao": ("automatico", "Sistema valida as regras do cupom/frete grátis e aplica ou explica o motivo."),
    "cashback": ("automatico", "Sistema consulta o extrato de cashback e informa a data de liberação."),
    "pagamento_pendente": ("automatico", "Sistema consulta o gateway (pix/boleto) e libera o pedido se o pagamento constar."),
    "endereco": ("automatico", "Se o pedido não foi separado, o bot altera; se já saiu, informa na hora que não é possível."),
    "sem_mensagem": ("automatico", "Menu do bot com as opções mais comuns (rastreio, troca, 2ª via)."),
    "elogio": ("automatico", "Agradecimento automático."),
    "atraso": ("assistido", "Sistema avisa o atraso de forma proativa com a nova previsão; compensação fica com o humano."),
    "item_faltando": ("assistido", "Bot coleta a foto e confere o peso/separação; humano aprova o reenvio."),
    "brinde": ("assistido", "Sistema confere se o pedido tinha direito ao brinde; humano aprova o envio."),
    "avaria": ("assistido", "Bot coleta foto do produto e da embalagem; humano aprova o reenvio."),
    "personalizacao": ("assistido", "Bot coleta a foto e mostra a arte do pedido lado a lado; humano aprova a nova produção."),
    "insatisfacao": ("assistido", "Sistema checa elegibilidade (30 dias, 1 troca, até 20% de uso) e gera a troca; humano só nas exceções."),
    "troca_erro_compra": ("assistido", "Sistema gera a troca de modelo/tamanho; humano nas exceções."),
    "arrependimento": ("assistido", "Sistema checa os 7 dias do recebimento; humano aprova o reembolso."),
    "cancelamento": ("assistido", "Se não foi separado, o sistema cancela; senão o humano trata."),
    "reacao_adversa": ("humano", "Saúde: orientar a suspensão, registrar cosmetovigilância em 24h e acionar a responsável técnica."),
    "cobranca_indevida": ("humano", "Dinheiro do cliente e possível fraude: conferir e estornar no prazo da política."),
    "outros": ("humano", "Não classificado: triagem humana."),
}


# ---------------------------------------------------------------------------
# Iniciativas (mudança de processo/produto)
# ---------------------------------------------------------------------------
INICIATIVAS = [
    dict(id="cosmetovigilancia", titulo="Protocolo de cosmetovigilância em toda reação adversa",
         dono="Responsável técnica + liderança de atendimento", tipo="Processo (conformidade)", risco="Crítico", esforco=1,
         acao="Criar a tag 'Reação adversa', roteiro obrigatório (suspender uso, coletar produto/lote/fotos, registrar em até 24h) e alerta automático para a RT.",
         filtro=lambda d: d.categoria.eq("reacao_adversa") | d.saude),
    dict(id="rota_express", titulo="Rota Express no Nordeste: plano de recuperação ou troca de transportadora",
         dono="Logística (gestão de transportadoras)", tipo="Processo", risco="Alto", esforco=2,
         acao="Cobrar SLA contratual, redistribuir CE/PE/RN/PB/BA para outra transportadora e auditar os pedidos parados.",
         filtro=lambda d: d.categoria.isin(["rastreio", "atraso"]) & d.transportadora.eq("Rota Express")),
    dict(id="rastreio_proativo", titulo="Rastreio proativo + bot com status real do pedido",
         dono="CX Digital (bot) + Tecnologia", tipo="Produto", risco="Médio", esforco=2,
         acao="Avisar o cliente a cada mudança de status e quando o pedido atrasar; bot responde 'onde está meu pedido' com dados do pedido.",
         filtro=lambda d: d.categoria.isin(["rastreio", "atraso"])),
    dict(id="expedicao", titulo="Conferência na expedição (itens do kit e brinde da campanha)",
         dono="Operações / CD + Marketing (regras de brinde)", tipo="Processo", risco="Alto", esforco=2,
         acao="Checagem por peso ou bipagem de cada item antes de fechar a caixa; brinde da campanha entra como item do pedido no sistema.",
         filtro=lambda d: d.categoria.isin(["item_faltando", "brinde"])),
    dict(id="personalizacao", titulo="Qualidade da personalização Gocase (texto trocado e impressão)",
         dono="Produção Gocase + Tecnologia (gerador de arte)", tipo="Processo / produto", risco="Alto", esforco=2,
         acao="Investigar o salto desde a semana de 24/08 (letras embaralhadas e impressão borrada); prévia da arte para o cliente aprovar; inspeção por amostragem.",
         filtro=lambda d: d.categoria.eq("personalizacao")),
    dict(id="avaria", titulo="Embalagem e lacre (vazamento do Cond. Anti-Frizz 1000ml e frascos)",
         dono="Qualidade / Embalagem Ápice + Operações", tipo="Produto", risco="Médio", esforco=2,
         acao="Lacre de indução e saco plástico em líquidos grandes; teste de queda; revisar o fornecedor da tampa do Anti-Frizz 1000ml.",
         filtro=lambda d: d.categoria.eq("avaria")),
    dict(id="pagamentos", titulo="Conciliação automática de pix/boleto e trava de cobrança dupla",
         dono="Financeiro + Tecnologia (checkout)", tipo="Produto", risco="Alto", esforco=2,
         acao="Baixa automática por webhook do gateway, idempotência no checkout e estorno automático de duplicidade.",
         filtro=lambda d: d.categoria.isin(["pagamento_pendente", "cobranca_indevida"])),
    dict(id="autoatendimento", titulo="Autoatendimento: endereço, cancelamento, cupom e dúvidas de produto",
         dono="Produto digital / E-commerce", tipo="Produto", risco="Baixo", esforco=2,
         acao="Página 'meu pedido' com alterar endereço/cancelar antes da separação, cupom aplicável após a compra, FAQ e ficha técnica por SKU.",
         filtro=lambda d: d.categoria.isin(["endereco", "cancelamento", "cupom_promocao", "cashback", "duvida_produto", "duvida_personalizacao", "troca_erro_compra"])),
    dict(id="fragrancia", titulo="Fragrâncias: descrição olfativa e cumprir a política de troca",
         dono="Marcas Barbour's e Lescent + CX", tipo="Produto / processo", risco="Médio", esforco=1,
         acao="Pirâmide olfativa e 'parecido com' na página; parar de oferecer cupom no lugar da troca a que o cliente tem direito.",
         filtro=lambda d: d.categoria.eq("insatisfacao")),
    dict(id="primeiro_contato", titulo="Resolver no primeiro contato (cumprir os retornos prometidos)",
         dono="Liderança de atendimento", tipo="Processo", risco="Alto", esforco=1,
         acao="Fila de follow-up para as promessas de '48h', não encerrar como 'resolvido' sem solução, supervisor liga a partir do 3º contato.",
         filtro=lambda d: d.recontato),
]


# ---------------------------------------------------------------------------
# Análise principal
# ---------------------------------------------------------------------------
def _num(x, nd=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def carregar_originais():
    return (ler_csv(DADOS / "conversas.csv"), ler_csv(DADOS / "mensagens.csv"), ler_csv(DADOS / "pedidos.csv"))


def analisar(conv_src=None, msgs_src=None, peds_src=None, params: dict | None = None, origem: str = "dados originais") -> dict:
    P = mesclar_params(params)
    if conv_src is None or msgs_src is None or peds_src is None:
        conv_raw, msgs_raw, peds_raw = carregar_originais()
    else:
        conv_raw, msgs_raw, peds_raw = ler_csv(conv_src), ler_csv(msgs_src), ler_csv(peds_src)

    conv, msgs, peds, log, brutos, finais = limpar(conv_raw, msgs_raw, peds_raw)
    if conv.empty:
        raise ValueError("Nenhuma conversa válida depois da limpeza.")

    data_ref = pd.Timestamp(P["data_referencia"]) if P.get("data_referencia") else \
        (max(msgs.dt.max() if not msgs.empty else conv.dt_abertura.max(), conv.dt_abertura.max()).normalize() + pd.Timedelta(days=1))
    dias = float(P["dias_periodo"]) if P.get("dias_periodo") else \
        float((conv.dt_abertura.max().normalize() - conv.dt_abertura.min().normalize()).days + 1)
    dias = max(dias, 1.0)
    fator = P["fator_amostra"]
    fator_mes = fator * 30.0 / dias

    # ---------- junta pedido
    pcols = ["order_id", "valor_num", "sku", "descricao_produto", "transportadora", "uf", "status_entrega", "dt_pedido"]
    d = conv.merge(peds[pcols], on="order_id", how="left")
    for c in ["sku", "descricao_produto", "transportadora", "uf", "status_entrega"]:
        d[c] = d[c].fillna("")
    d["grupo_marca"] = d.marca.map(grupo_marca)

    # ---------- mensagens por conversa (uma passada só, em listas: bem mais rápido que filtrar DataFrame)
    msgs = msgs.sort_values(["conversation_id", "dt", "message_id"])
    por_conv: dict[str, dict] = {}
    for cid, autor, dt, texto in zip(msgs.conversation_id, msgs.autor, msgs.dt, msgs.texto):
        e = por_conv.setdefault(cid, {"cli": [], "cli_t": [], "age": [], "age_t": [], "ultimo": ""})
        if autor == "cliente":
            e["cli"].append(texto); e["cli_t"].append(dt)
        elif autor == "agente":
            e["age"].append(texto); e["age_t"].append(dt)
        e["ultimo"] = autor
    produtos_norm = sorted({normalizar(x) for x in peds.descricao_produto if x}, key=len, reverse=True)

    pii_tot = {"cpf": 0, "cartao": 0, "telefone": 0, "email": 0}
    vazio = {"cli": [], "cli_t": [], "age": [], "age_t": [], "ultimo": ""}
    linhas = []
    for row in d.itertuples(index=False):
        e = por_conv.get(row.conversation_id, vazio)
        textos_cli = e["cli"]
        res = classificar_conversa(textos_cli, produtos_norm)
        pii = {"cpf": 0, "cartao": 0, "telefone": 0, "email": 0}
        for t in textos_cli:
            for k, v in detectar_pii(t).items():
                pii[k] += v
        for k in pii_tot:
            pii_tot[k] += pii[k]
        t_cli = e["cli_t"][0] if textos_cli else None
        t_age = None
        if t_cli is not None:
            t_age = next((t for t in e["age_t"] if t >= t_cli), None)
        frt, sla = calcular_sla(row.canal, t_cli, t_age, data_ref, P)
        acoes = acoes_agente(e["age"])
        prom = RE_PROMESSA_H.search(" ".join(normalizar(x) for x in e["age"])) if "promessa_retorno" in acoes else None
        sit, expl = avaliar_politica(res.categoria, res.saude_em_qualquer_msg, acoes, frt if t_age is not None else None,
                                     row.status_entrega, res.alegacao_sem_divergencia, P,
                                     foto_sem_retorno=res.foto_enviada and e["ultimo"] == "cliente")
        linhas.append(dict(
            categoria=res.categoria, secundarias=res.secundarias, confianca=res.confianca, regra=res.regra,
            recontato_texto=res.recontato_texto, vezes_declaradas=res.vezes_declaradas, ironia=res.ironia,
            injecao=res.injecao, ameaca=res.ameaca, urgencia=res.urgencia, foto_enviada=res.foto_enviada,
            saude=res.saude_em_qualquer_msg, alegacao_sem_divergencia=res.alegacao_sem_divergencia,
            n_cli=len(textos_cli), n_agente=len(e["age"]), pii=any(pii.values()), pii_tipos=[k for k, v in pii.items() if v],
            t_cliente=t_cli, t_agente=t_age, frt_h=frt if t_age is not None else None, sla=sla, acoes=acoes,
            promessa_h=int(prom.group(1)) if prom else None, ultimo_autor=e["ultimo"],
            politica=sit, politica_motivo=expl,
            trecho=mascarar(textos_cli[0])[:220] if textos_cli else "",
        ))
    d = pd.concat([d.reset_index(drop=True), pd.DataFrame(linhas)], axis=1)

    # ---------- recontato e reabertura (por cliente + pedido)
    d = d.sort_values("dt_abertura").reset_index(drop=True)
    chave = d.customer_id.where(d.customer_id.ne(""), d.conversation_id) + "|" + d.order_id.where(d.order_id.ne(""), d.conversation_id)
    d["chave_caso"] = chave
    d["ordem_contato"] = d.groupby("chave_caso").cumcount() + 1
    d["contatos_no_caso"] = d.groupby("chave_caso").conversation_id.transform("count")
    d["recontato"] = (d.ordem_contato > 1) | d.recontato_texto
    prox = d.groupby("chave_caso").dt_abertura.shift(-1)
    d["voltou_depois"] = prox.notna()
    d["horas_ate_voltar"] = (prox - d.dt_abertura).dt.total_seconds() / 3600
    d["resolvido_sem_resolver"] = d.status.eq("resolvido") & d.voltou_depois
    d["promessa_quebrada"] = d.promessa_h.notna() & d.voltou_depois
    d["escalou_ra"] = d.canal.eq("reclame_aqui") & d.recontato

    # ---------- custo
    custo = d.canal.map(P["custo_canal"]).fillna(0.0)
    custo = np.where(d.n_cli.eq(0), custo * P["custo_conversa_vazia_pct"], custo)
    d["custo"] = custo
    d["custo_mes"] = d.custo * fator_mes

    # ---------- concordância com a tag do agente
    def concorda(cat, tag):
        if cat == "sem_mensagem":
            return None
        info = CATEGORIAS[cat]
        return bool(tag == info["tag"] or tag in info["aceitas"])
    d["tag_esperada"] = d.categoria.map(lambda c: CATEGORIAS[c]["tag"])
    d["concorda"] = [concorda(c, t) for c, t in zip(d.categoria, d.tag_agente)]
    d["concorda_estrito"] = [None if c == "sem_mensagem" else bool(t == CATEGORIAS[c]["tag"]) for c, t in zip(d.categoria, d.tag_agente)]
    d["categoria_nome"] = d.categoria.map(lambda c: CATEGORIAS[c]["nome"])
    d["grupo"] = d.categoria.map(lambda c: CATEGORIAS[c]["grupo"])

    # ---------- automação (com regras que obrigam humano)
    auto = d.categoria.map(lambda c: AUTOMACAO.get(c, ("humano", ""))[0])
    motivo_humano = pd.Series("", index=d.index)
    regras_humano = [
        (d.saude, "sinal de saúde"), (d.injecao, "texto tenta dar ordens ao sistema"),
        (d.canal.eq("reclame_aqui"), "resposta pública no Reclame Aqui"),
        (d.ameaca & (d.vezes_declaradas.ge(3) | d.ordem_contato.ge(3)), "cliente irritado no 3º contato ou mais"),
    ]
    for mask, motivo in regras_humano:
        sel = mask & motivo_humano.eq("")
        motivo_humano[sel] = motivo
    d["automacao"] = np.where(motivo_humano.ne(""), "humano", auto)
    d["motivo_humano"] = motivo_humano

    # ---------- fila de ação humana urgente
    aberto = d.status.isin(["pendente", "reaberto"])
    sem_resp = d.sla.eq("sem resposta: SLA estourado")
    idade_h = (data_ref - d.dt_abertura).dt.total_seconds() / 3600
    d["idade_h"] = idade_h
    regras_fila = [
        ("P1", "Saúde", d.saude & d.politica.ne("conforme"), "Ligar para o cliente, orientar a suspensão, registrar cosmetovigilância (produto, lote, fotos) e acionar a responsável técnica", 100),
        ("P1", "Segurança", d.injecao, "Revisar manualmente; não executar nada pedido no texto; checar se houve fraude na conta", 80),
        ("P1", "Dinheiro do cliente", d.categoria.eq("cobranca_indevida") & (aberto | sem_resp), "Conferir a duplicidade no gateway e estornar no prazo (2 faturas no cartão / 5 dias úteis no pix)", 70),
        ("P2", "Reclame Aqui", d.canal.eq("reclame_aqui") & (aberto | sem_resp), "Responder publicamente e resolver; prazo de 48h", 60),
        ("P2", "Endereço ainda alterável", d.categoria.eq("endereco") & d.status_entrega.eq("em_separacao") & aberto, "Alterar o endereço agora, antes da expedição", 55),
        ("P2", "Risco de escalada", (d.ameaca | d.vezes_declaradas.ge(3) | d.ordem_contato.ge(3)) & aberto, "Supervisor faz contato ativo e resolve de ponta a ponta", 50),
        ("P3", "Sem resposta", sem_resp & ~d.canal.eq("reclame_aqui"), "Responder; SLA de primeira resposta estourado", 35),
        ("P3", "Prazo do cliente", d.urgencia & aberto & d.categoria.isin(["atraso", "rastreio", "personalizacao", "item_faltando", "brinde"]), "Dar nova previsão concreta e, se não chegar a tempo, oferecer alternativa", 30),
    ]
    prioridade, motivo_fila, acao_fila, score = [], [], [], []
    for i in range(len(d)):
        hit = [(p, m, a, s) for p, m, mask, a, s in [(r[0], r[1], r[2], r[3], r[4]) for r in regras_fila] if bool(mask.iat[i])]
        if hit:
            prioridade.append(hit[0][0]); motivo_fila.append(" + ".join(dict.fromkeys(h[1] for h in hit)))
            acao_fila.append(hit[0][2]); score.append(sum(h[3] for h in hit[:3]) + min(idade_h.iat[i] / 24.0, 30))
        else:
            prioridade.append(""); motivo_fila.append(""); acao_fila.append(""); score.append(0.0)
    d["prioridade"], d["motivo_fila"], d["acao_fila"], d["score_fila"] = prioridade, motivo_fila, acao_fila, score

    return montar_resposta(d, peds, log, brutos, finais, P, data_ref, dias, fator, fator_mes, pii_tot, origem)


# ---------------------------------------------------------------------------
# Agregações para a interface
# ---------------------------------------------------------------------------
def _pct(a, b):
    return _num(100.0 * a / b, 1) if b else None


def montar_resposta(d, peds, log, brutos, finais, P, data_ref, dias, fator, fator_mes, pii_tot, origem):
    n = len(d)
    custo_mes_total = d.custo_mes.sum()
    com_msg = d[d.n_cli > 0]

    # ---------- motivos
    motivos = []
    for cat, g in d.groupby("categoria"):
        motivos.append(dict(
            categoria=cat, nome=CATEGORIAS[cat]["nome"], grupo=CATEGORIAS[cat]["grupo"],
            conversas=int(len(g)), conversas_mes=_num(len(g) * fator_mes, 0),
            custo_mes=_num(g.custo_mes.sum()), custo_ano=_num(g.custo_mes.sum() * 12),
            pct_volume=_pct(len(g), n), pct_custo=_pct(g.custo_mes.sum(), custo_mes_total),
            pct_reclame_aqui=_pct(g.canal.eq("reclame_aqui").sum(), len(g)),
            pct_recontato=_pct(g.recontato.sum(), len(g)),
            pct_aberto=_pct(g.status.isin(["pendente", "reaberto"]).sum(), len(g)),
            ticket_medio=_num(g.valor_num.mean()),
            concordancia=_pct(g.concorda.eq(True).sum(), g.concorda.notna().sum()),
            automacao=AUTOMACAO.get(cat, ("humano", ""))[0], como_automatizar=AUTOMACAO.get(cat, ("", ""))[1],
            tag_esperada=CATEGORIAS[cat]["tag"],
            tags_do_agente={k: int(v) for k, v in g.tag_agente.replace("", "(vazia)").value_counts().head(5).items()},
        ))
    motivos.sort(key=lambda m: -m["custo_mes"])
    grupos = (d.groupby("grupo").agg(conversas=("conversation_id", "count"), custo_mes=("custo_mes", "sum"))
              .sort_values("custo_mes", ascending=False).reset_index())

    # ---------- canais
    canais = []
    for c, g in d.groupby("canal"):
        sla_validos = g[g.sla.isin(["no prazo", "fora do prazo", "sem resposta: SLA estourado"])]
        canais.append(dict(
            canal=c, nome=NOME_CANAL.get(c, c), conversas=int(len(g)), conversas_mes=_num(len(g) * fator_mes, 0),
            custo_unit=P["custo_canal"].get(c, 0.0), custo_mes=_num(g.custo_mes.sum()),
            pct_volume=_pct(len(g), n), pct_custo=_pct(g.custo_mes.sum(), custo_mes_total),
            sla_h=P["sla_horas"].get(c), pct_sla_ok=_pct(sla_validos.sla.eq("no prazo").sum(), len(sla_validos)),
            frt_mediana_h=_num(g.frt_h.median()), frt_p90_h=_num(g.frt_h.quantile(0.9)),
            sem_resposta=int(g.sla.str.startswith("sem resposta").sum()),
            pct_recontato=_pct(g.recontato.sum(), len(g)),
        ))
    canais.sort(key=lambda x: -x["conversas"])

    # SLA do WhatsApp: dentro e fora do horário
    wa = d[(d.canal == "whatsapp") & d.t_cliente.notna()]
    h_abre, h_fecha = P["hora_abertura"], P["hora_fechamento"]
    fora_h = wa.t_cliente.map(lambda t: not (h_abre <= t.hour + t.minute / 60 < h_fecha))
    sla_wa = dict(
        fora_horario=int(fora_h.sum()), pct_fora_horario=_pct(fora_h.sum(), len(wa)),
        pct_ok_dentro=_pct(wa[~fora_h].sla.eq("no prazo").sum(), (~fora_h).sum()),
        pct_ok_fora=_pct(wa[fora_h].sla.eq("no prazo").sum(), fora_h.sum()),
        espera_mediana_fora_h=_num(((wa[fora_h].t_agente - wa[fora_h].t_cliente).dt.total_seconds() / 3600).median()),
    )

    # ---------- concordância
    avaliaveis = d[d.concorda.notna()]
    conf = pd.crosstab(avaliaveis.categoria_nome, avaliaveis.tag_agente.replace("", "(vazia)"))
    tag_vazia_com_conteudo = int(((d.n_cli > 0) & d.tag_agente.eq("")).sum())
    tag_em_vazia = int(((d.n_cli == 0) & d.tag_agente.ne("")).sum())
    por_tag = []
    for tag, g in avaliaveis.groupby(avaliaveis.tag_agente.replace("", "(vazia)")):
        top = g.categoria_nome.value_counts()
        por_tag.append(dict(tag=tag, conversas=int(len(g)), pct_concorda=_pct(g.concorda.sum(), len(g)),
                            principal_real=top.index[0], pct_principal=_pct(top.iloc[0], len(g))))
    exemplos_disc = (avaliaveis[avaliaveis.concorda.eq(False) & avaliaveis.confianca.eq("alta")]
                     .sample(min(12, int((avaliaveis.concorda.eq(False) & avaliaveis.confianca.eq("alta")).sum())), random_state=1)
                     if len(avaliaveis) else avaliaveis)
    concordancia = dict(
        flexivel=_pct(avaliaveis.concorda.sum(), len(avaliaveis)),
        estrita=_pct(avaliaveis.concorda_estrito.sum(), len(avaliaveis)),
        avaliadas=int(len(avaliaveis)), tag_vazia_com_conteudo=tag_vazia_com_conteudo, tag_em_conversa_vazia=tag_em_vazia,
        matriz=dict(linhas=list(conf.index), colunas=list(conf.columns), valores=conf.values.tolist()),
        por_tag=sorted(por_tag, key=lambda x: x["pct_concorda"] or 0),
        exemplos=[dict(id=r.conversation_id, tag=r.tag_agente or "(vazia)", nossa=r.categoria_nome, trecho=r.trecho)
                  for r in exemplos_disc.itertuples()],
        confianca={k: int(v) for k, v in d.confianca.value_counts().items()},
        multiassunto=int(d.secundarias.map(len).gt(0).sum()),
    )

    # ---------- concentrações
    entrega = d[d.categoria.isin(["rastreio", "atraso"])]
    transp = []
    peds_t = peds[peds.transportadora.ne("")]
    for t, g in peds_t.groupby("transportadora"):
        ge = entrega[entrega.transportadora == t]
        transp.append(dict(
            transportadora=t, pedidos=int(len(g)), pct_pedidos=_pct(len(g), len(peds_t)),
            contatos_entrega=int(len(ge)), pct_contatos_entrega=_pct(len(ge), len(entrega)),
            indice=_num((len(ge) / max(len(entrega), 1)) / (len(g) / max(len(peds_t), 1)), 2),
            pct_em_transito=_pct(g.status_entrega.isin(["em_transito", "atrasado"]).sum(), len(g)),
            custo_mes=_num(ge.custo_mes.sum()),
        ))
    transp.sort(key=lambda x: -(x["indice"] or 0))
    uf_tab = []
    for uf, g in entrega[entrega.uf.ne("")].groupby("uf"):
        base = (peds_t.uf == uf).sum()
        uf_tab.append(dict(uf=uf, contatos=int(len(g)), pedidos=int(base),
                           indice=_num((len(g) / max(len(entrega), 1)) / (base / max(len(peds_t), 1)), 2) if base else None,
                           pct_rota=_pct(g.transportadora.eq(transp[0]["transportadora"] if transp else "").sum(), len(g))))
    uf_tab.sort(key=lambda x: -x["contatos"])
    ct = pd.crosstab(entrega.transportadora, entrega.uf) if len(entrega) else pd.DataFrame()
    top_ufs = [u["uf"] for u in uf_tab[:10]]
    heat = dict(linhas=list(ct.index), colunas=top_ufs,
                valores=[[int(ct.loc[t, u]) if u in ct.columns else 0 for u in top_ufs] for t in ct.index]) if len(ct) else {}

    # semana a semana
    d["semana"] = d.dt_abertura.dt.to_period("W-SUN").dt.start_time
    semanas = sorted(d.semana.unique())
    sem_lbl = [pd.Timestamp(s).strftime("%d/%m") for s in semanas]
    serie = {}
    for cat, g in d.groupby("categoria"):
        vc = g.semana.value_counts()
        serie[cat] = [int(vc.get(s, 0)) for s in semanas]
    # detecção de mudança: média das últimas 4 semanas completas vs das 4 primeiras
    completas = [i for i, s in enumerate(semanas) if (d.semana == s).sum() >= 0.5 * (n / max(len(semanas), 1))]
    mudancas = []
    if len(completas) >= 6:
        ini, fim = completas[:4], completas[-4:]
        for cat, v in serie.items():
            if cat == "sem_mensagem":
                continue
            a = np.mean([v[i] for i in ini]); b = np.mean([v[i] for i in fim])
            if max(a, b) >= 3:
                mudancas.append(dict(categoria=cat, nome=CATEGORIAS[cat]["nome"], antes=_num(a, 1), depois=_num(b, 1),
                                     variacao=_pct(b - a, a) if a else None))
        mudancas.sort(key=lambda x: -(x["variacao"] or 999))
    # quando começou (primeira semana com volume >= 2x a mediana das 4 primeiras)
    def inicio_salto(cat):
        v = serie.get(cat, [])
        if len(v) < 6:
            return None
        base = max(np.median(v[1:5]), 1)
        for i in range(4, len(v)):
            if v[i] >= 2 * base and (i + 1 < len(v) and v[i + 1] >= 2 * base):
                return sem_lbl[i]
        return None
    for m in mudancas:
        m["desde"] = inicio_salto(m["categoria"])

    # produtos
    base_prod = peds.descricao_produto.value_counts()

    def top_produtos(cat, k=6):
        g = d[d.categoria == cat]
        out = []
        for prod, gg in g.groupby("descricao_produto"):
            if not prod:
                continue
            base = base_prod.get(prod, 0)
            out.append(dict(produto=prod, marca=gg.marca.iloc[0], contatos=int(len(gg)), pedidos_na_base=int(base),
                            pct_da_categoria=_pct(len(gg), len(g))))
        return sorted(out, key=lambda x: -x["contatos"])[:k]
    produtos = {cat: top_produtos(cat) for cat in ["avaria", "personalizacao", "reacao_adversa", "insatisfacao", "item_faltando", "brinde"]}

    # marcas: taxa de contato por pedido
    marcas = []
    for gm, g in d.groupby("grupo_marca"):
        ped = P["pedidos_periodo"].get(gm)
        conv_periodo = len(g) * fator
        marcas.append(dict(grupo=gm, conversas=int(len(g)), conversas_periodo=_num(conv_periodo, 0), pedidos_periodo=ped,
                           contatos_por_100_pedidos=_num(100 * conv_periodo / ped, 1) if ped else None,
                           custo_por_pedido=_num(g.custo.sum() * fator / ped, 2) if ped else None,
                           custo_mes=_num(g.custo_mes.sum())))
    por_marca = []
    for mca, g in d.groupby("marca"):
        top = g[g.categoria != "sem_mensagem"].categoria.value_counts()
        por_marca.append(dict(marca=mca, conversas=int(len(g)), custo_mes=_num(g.custo_mes.sum()),
                              motivo_1=CATEGORIAS[top.index[0]]["nome"] if len(top) else "", pct_motivo_1=_pct(top.iloc[0], len(g)) if len(top) else None,
                              pct_ra=_pct(g.canal.eq("reclame_aqui").sum(), len(g))))
    por_marca.sort(key=lambda x: -x["conversas"])
    hora = d[d.t_cliente.notna()].t_cliente.dt.hour.value_counts().reindex(range(24), fill_value=0)

    # ---------- políticas
    pol = d[d.politica.ne("n/a")]
    politicas = []
    for cat, g in pol.groupby("categoria"):
        nc = g[g.politica.eq("nao_conforme")]
        politicas.append(dict(categoria=cat, nome=CATEGORIAS[cat]["nome"], casos=int(len(g)),
                              conformes=int(g.politica.eq("conforme").sum()), nao_conformes=int(len(nc)),
                              atencao=int(g.politica.eq("atencao").sum()),
                              pct_nao_conforme=_pct(len(nc), len(g)),
                              principal_desvio=(nc.politica_motivo.value_counts().index[0] if len(nc) else
                                                (g[g.politica.eq("atencao")].politica_motivo.value_counts().index[0] if g.politica.eq("atencao").any() else ""))))
    politicas.sort(key=lambda x: -(x["nao_conformes"] + 0.01 * x["atencao"]))
    end = d[d.categoria.eq("endereco")]
    politica_extra = dict(
        endereco_ja_saiu=int(end.status_entrega.isin(["em_transito", "entregue", "atrasado"]).sum()),
        endereco_total=int(len(end)),
        promessas=int(d.promessa_h.notna().sum()), promessas_quebradas=int(d.promessa_quebrada.sum()),
        resolvido=int(d.status.eq("resolvido").sum()), resolvido_sem_resolver=int(d.resolvido_sem_resolver.sum()),
        saude_total=int((d.categoria.eq("reacao_adversa") | d.saude).sum()),
        saude_com_registro=int(((d.categoria.eq("reacao_adversa") | d.saude) & d.politica.eq("conforme")).sum()),
        saude_tags={k: int(v) for k, v in d[d.categoria.eq("reacao_adversa") | d.saude].tag_agente.replace("", "(vazia)").value_counts().items()},
        alegacao_sem_divergencia=int(d.alegacao_sem_divergencia.sum()),
    )

    # ---------- iniciativas: cada conversa entra só na 1ª iniciativa que a cobre (sem dupla contagem)
    restante = pd.Series(d.custo_mes.to_numpy(copy=True), index=d.index)  # cópia: não alterar d
    taxas = P["taxas_iniciativas"]
    ja = pd.Series(False, index=d.index)
    iniciativas = []
    for ini in INICIATIVAS:
        mask = ini["filtro"](d).fillna(False).astype(bool)
        tx = float(taxas.get(ini["id"], 0.0))
        if ini["id"] == "primeiro_contato":
            alvo = mask  # recontatos: aplica sobre o que sobrou depois das outras iniciativas
            ganho_mes = (restante[alvo] * tx).sum()
            evitadas = alvo.sum() * tx * fator_mes  # aproximação
            restante[alvo] = restante[alvo] * (1 - tx)
        else:
            alvo = mask & ~ja
            ganho_mes = (restante[alvo] * tx).sum()
            evitadas = alvo.sum() * tx * fator_mes
            restante[alvo] = restante[alvo] * (1 - tx)
            ja |= alvo
        g = d[mask]
        iniciativas.append(dict(
            id=ini["id"], titulo=ini["titulo"], dono=ini["dono"], tipo=ini["tipo"], risco=ini["risco"], esforco=ini["esforco"],
            acao=ini["acao"], taxa=tx, conversas_amostra=int(mask.sum()), conversas_mes=_num(mask.sum() * fator_mes, 0),
            custo_atual_mes=_num(d.custo_mes[mask].sum()), ganho_mes=_num(ganho_mes), ganho_ano=_num(ganho_mes * 12),
            contatos_evitados_mes=_num(evitadas, 0), pct_ra=_pct(g.canal.eq("reclame_aqui").sum(), len(g)),
            valor_pedidos_mes=_num(g.drop_duplicates("order_id").valor_num.sum() * fator_mes),
        ))
    peso_risco = {"Crítico": 4, "Alto": 3, "Médio": 2, "Baixo": 1}
    for i in iniciativas:
        # critério: risco crítico (saúde/lei) sempre primeiro; depois ganho anual / esforço, com bônus por risco
        i["score"] = (1e9 if i["risco"] == "Crítico" else 0) + (i["ganho_ano"] or 0) / i["esforco"] * (1 + 0.25 * peso_risco[i["risco"]])
    iniciativas.sort(key=lambda x: -x["score"])
    for k, i in enumerate(iniciativas, 1):
        i["ordem"] = k
    ganho_total_mes = sum(i["ganho_mes"] or 0 for i in iniciativas)

    # evidências específicas para os cartões
    evid = {}
    if transp:
        t0 = transp[0]
        ne = {"BA", "CE", "PE", "RN", "PB", "MA", "PI", "AL", "SE"}
        e0 = entrega[entrega.transportadora == t0["transportadora"]]
        evid["rota_express"] = (f"{t0['transportadora']} tem {t0['pct_pedidos']}% dos pedidos e {t0['pct_contatos_entrega']}% dos contatos de entrega "
                                f"(índice {t0['indice']}x); {_pct(e0.uf.isin(ne).sum(), len(e0))}% desses contatos são do Nordeste e "
                                f"{t0['pct_em_transito']}% dos pedidos dela seguem em trânsito/atrasados na extração.")
    for m in mudancas:
        if m["categoria"] in ("personalizacao", "item_faltando", "brinde", "rastreio", "avaria") and m["variacao"]:
            evid.setdefault("tendencia_" + m["categoria"], f"{m['nome']}: de {m['antes']} para {m['depois']} conversas/semana na amostra ({'+' if m['variacao'] > 0 else ''}{m['variacao']}%)" + (f", salto a partir da semana de {m['desde']}" if m.get("desde") else ""))
    if produtos.get("avaria"):
        a0 = produtos["avaria"][0]
        evid["avaria"] = f"{a0['produto']} ({a0['marca']}) responde por {a0['pct_da_categoria']}% das avarias ({a0['contatos']} conversas)."
    for i in iniciativas:
        chaves = {"rota_express": ["rota_express", "tendencia_rastreio"], "expedicao": ["tendencia_item_faltando", "tendencia_brinde"],
                  "personalizacao": ["tendencia_personalizacao"], "avaria": ["avaria", "tendencia_avaria"]}.get(i["id"], [])
        i["evidencia"] = " ".join(evid[c] for c in chaves if c in evid)
    for i in iniciativas:
        if i["id"] == "cosmetovigilancia":
            i["evidencia"] = (f"{politica_extra['saude_total']} relatos de reação adversa na amostra (~{_num(politica_extra['saude_total'] * fator_mes, 0)}/mês); "
                              f"{politica_extra['saude_com_registro']} com registro de cosmetovigilância. A lista de tags não tem categoria de saúde: "
                              f"os casos aparecem como {', '.join(list(politica_extra['saude_tags'])[:4])}.")
        if i["id"] == "fragrancia":
            fr = next((p for p in politicas if p["categoria"] == "insatisfacao"), None)
            if fr:
                i["evidencia"] = f"{fr['nao_conformes']} de {fr['casos']} pedidos de troca por fragrância receberam cupom em vez da troca prevista."
        if i["id"] == "primeiro_contato":
            i["evidencia"] = (f"{_pct(d.recontato.sum(), n)}% das conversas são recontato do mesmo pedido; {politica_extra['resolvido_sem_resolver']} conversas "
                              f"'resolvidas' voltaram depois; {politica_extra['promessas_quebradas']} de {politica_extra['promessas']} promessas de retorno em 48h terminaram com o cliente voltando.")
        if i["id"] == "pagamentos":
            i["evidencia"] = f"{int(d.categoria.isin(['cobranca_indevida', 'pagamento_pendente']).sum())} conversas sobre cobrança dupla ou pagamento não confirmado na amostra."
        if i["id"] == "rastreio_proativo":
            r0 = d[d.categoria.isin(["rastreio", "atraso"])]
            i["evidencia"] = (f"{_pct(r0.status_entrega.isin(['em_transito', 'atrasado']).sum(), len(r0))}% dos contatos de entrega são de pedidos em trânsito ou atrasados: "
                              f"a informação já estava no sistema. {_pct(r0.recontato.sum(), len(r0))}% deles são recontato.")
        if i["id"] == "autoatendimento":
            i["evidencia"] = (f"{politica_extra['endereco_ja_saiu']} de {politica_extra['endereco_total']} pedidos de troca de endereço já tinham saído: "
                              f"a resposta pode ser automática.")

    # ---------- evitáveis
    evitavel_ids = {"rastreio", "atraso", "item_faltando", "brinde", "pagamento_pendente", "cupom_promocao", "cashback", "endereco",
                    "duvida_produto", "duvida_personalizacao", "cancelamento", "personalizacao", "avaria"}
    d["evitavel"] = d.categoria.isin(evitavel_ids) | d.recontato
    evitaveis = dict(
        conversas=int(d.evitavel.sum()), pct=_pct(d.evitavel.sum(), n), custo_mes=_num(d.custo_mes[d.evitavel].sum()),
        pct_custo=_pct(d.custo_mes[d.evitavel].sum(), custo_mes_total),
        recontatos=int(d.recontato.sum()), custo_recontatos_mes=_num(d.custo_mes[d.recontato].sum()),
        ra_recontato=int(d.escalou_ra.sum()), ra_total=int(d.canal.eq("reclame_aqui").sum()),
        custo_ra_recontato_mes=_num(d.custo_mes[d.escalou_ra].sum()),
        sem_mensagem=int(d.categoria.eq("sem_mensagem").sum()),
        ganho_estimado_mes=_num(ganho_total_mes), ganho_estimado_ano=_num(ganho_total_mes * 12),
    )

    # ---------- automação
    auto_res = []
    eco = P["economia_automacao"]
    for nivel in ("automatico", "assistido", "humano"):
        g = d[d.automacao == nivel]
        auto_res.append(dict(nivel=nivel, conversas=int(len(g)), pct=_pct(len(g), n), conversas_mes=_num(len(g) * fator_mes, 0),
                             custo_mes=_num(g.custo_mes.sum()), economia_mes=_num(g.custo_mes.sum() * eco.get(nivel, 0))))
    motivos_humano = {k: int(v) for k, v in d[d.motivo_humano.ne("")].motivo_humano.value_counts().items()}

    fila = d[d.prioridade.ne("")].sort_values(["prioridade", "score_fila"], ascending=[True, False])
    fila_rows = [dict(
        id=r.conversation_id, prioridade=r.prioridade, motivo=r.motivo_fila, acao=r.acao_fila, canal=NOME_CANAL.get(r.canal, r.canal),
        marca=r.marca, categoria=r.categoria_nome, status=r.status, idade_dias=_num(r.idade_h / 24, 1),
        aberta_em=r.dt_abertura.strftime("%d/%m %H:%M"), pedido=r.order_id, status_entrega=r.status_entrega, trecho=r.trecho,
        contatos=int(r.contatos_no_caso),
    ) for r in fila.itertuples()]
    fila_resumo = {k: int(v) for k, v in fila.motivo_fila.str.split(" \\+ ").explode().value_counts().items()} if len(fila) else {}

    # ---------- conversas (para conferência/exportação; texto mascarado)
    conversas = [dict(
        id=r.conversation_id, data=r.dt_abertura.strftime("%Y-%m-%d %H:%M"), marca=r.marca, canal=r.canal, status=r.status,
        tag_agente=r.tag_agente, categoria=r.categoria, categoria_nome=r.categoria_nome, grupo=r.grupo,
        secundarias="; ".join(CATEGORIAS[s]["nome"] for s in r.secundarias), confianca=r.confianca,
        concorda=r.concorda, regra=r.regra, recontato=bool(r.recontato), contatos_no_caso=int(r.contatos_no_caso),
        sla=r.sla, frt_h=_num(r.frt_h), politica=r.politica, politica_motivo=r.politica_motivo, automacao=r.automacao,
        motivo_humano=r.motivo_humano, prioridade=r.prioridade, injecao=bool(r.injecao), ironia=bool(r.ironia), pii=bool(r.pii),
        custo=_num(r.custo), pedido=r.order_id, transportadora=r.transportadora, uf=r.uf, status_entrega=r.status_entrega,
        produto=r.descricao_produto, trecho=r.trecho,
    ) for r in d.itertuples()]

    # ---------- qualidade dos dados
    qualidade = list(log)
    def q(tabela, verif, nn, acao, tipo="achado"):
        qualidade.append({"tabela": tabela, "verificacao": verif, "linhas": int(nn), "acao": acao, "tipo": tipo})
    q("mensagens", "Dados pessoais no texto do cliente (CPF)", pii_tot["cpf"], "mascarados como [CPF] em tela e exportação")
    q("mensagens", "Dados pessoais no texto do cliente (número de cartão)", pii_tot["cartao"], "mascarados como [CARTÃO]")
    q("mensagens", "Dados pessoais no texto do cliente (telefone)", pii_tot["telefone"], "mascarados como [TELEFONE]")
    q("mensagens", "Dados pessoais no texto do cliente (e-mail)", pii_tot["email"], "mascarados como [E-MAIL]")
    q("mensagens", "Texto tentando dar ordens ao sistema (prompt injection)", int(d.injecao.sum()), "trecho ignorado na classificação; conversa vai para revisão humana")
    q("conversas", "Conversas em que o cliente não escreveu nada (só o bot falou)", int(d.n_cli.eq(0).sum()), f"custo considerado {int(P['custo_conversa_vazia_pct']*100)}% do canal; fora da concordância")
    q("conversas", "Tag preenchida em conversa sem mensagem do cliente", tag_em_vazia, "tag desconsiderada")
    q("conversas", "Tag vazia em conversa com mensagem do cliente", tag_vazia_com_conteudo, "conta como discordância")
    q("conversas", "Tag do agente diferente do conteúdo da conversa", int(avaliaveis.concorda.eq(False).sum()), "a análise usa a classificação pelo texto, não a tag", "achado")
    q("conversas", "Status 'resolvido' mas o cliente voltou depois pelo mesmo pedido", politica_extra["resolvido_sem_resolver"], "status manual não é usado como prova de resolução")
    q("conversas", "Cliente diz que já é o 2º/3º contato (recontato declarado)", int(d.recontato_texto.sum()), "conta como recontato mesmo sem a conversa anterior na base")
    q("conversas", "Mais de um assunto na mesma mensagem", concordancia["multiassunto"], "motivo principal = 1º assunto; demais viram secundários")
    q("conversas", "Ironia/sarcasmo detectado", int(d.ironia.sum()), "não conta como elogio; tom negativo")
    q("conversas", "Relato de erro de personalização sem divergência (pediu X, veio X)", politica_extra["alegacao_sem_divergencia"], "sinalizado para conferência antes da nova produção")

    principais = [m for m in motivos if m["categoria"] != "sem_mensagem"]
    executivo = dict(
        conversas_amostra=n, conversas_mes=_num(n * fator_mes, 0), custo_mes=_num(custo_mes_total), custo_ano=_num(custo_mes_total * 12),
        custo_medio=_num(d.custo[d.n_cli > 0].mean()),
        pct_ra_volume=_pct(d.canal.eq("reclame_aqui").sum(), n), pct_ra_custo=_pct(d.custo_mes[d.canal.eq("reclame_aqui")].sum(), custo_mes_total),
        pct_recontato=_pct(d.recontato.sum(), n), pct_aberto=_pct(d.status.isin(["pendente", "reaberto"]).sum(), n),
        concordancia=concordancia["flexivel"], fila_urgente=int(len(fila)), fila_p1=int((fila.prioridade == "P1").sum()),
        top_motivos=principais[:6], ganho_mes=_num(ganho_total_mes), ganho_ano=_num(ganho_total_mes * 12),
        pct_evitavel=evitaveis["pct"], pct_automatico=_pct((d.automacao == "automatico").sum(), n),
        pct_sla_whatsapp=next((c["pct_sla_ok"] for c in canais if c["canal"] == "whatsapp"), None),
    )
    executivo["achados"] = gerar_achados(executivo, motivos, canais, transp, mudancas, produtos, politicas, politica_extra, evitaveis, concordancia, marcas, sla_wa, fator_mes, d)

    return dict(
        meta=dict(origem=origem, gerado_em=datetime.now().strftime("%d/%m/%Y %H:%M"), data_referencia=(data_ref - pd.Timedelta(seconds=1)).strftime("%d/%m/%Y %H:%M"),
                  periodo=f"{d.dt_abertura.min():%d/%m/%Y} a {d.dt_abertura.max():%d/%m/%Y}", dias=dias, fator_amostra=fator,
                  fator_mes=_num(fator_mes, 4), linhas_brutas=brutos, linhas_finais=finais),
        parametros=P, executivo=executivo, motivos=motivos,
        grupos=[dict(grupo=r.grupo, conversas=int(r.conversas), custo_mes=_num(r.custo_mes)) for r in grupos.itertuples()],
        canais=canais, sla_whatsapp=sla_wa, concordancia=concordancia,
        concentracoes=dict(transportadoras=transp, ufs=uf_tab[:15], heatmap=heat, marcas=marcas, por_marca=por_marca,
                           produtos=produtos, mudancas=mudancas, hora=[int(x) for x in hora.values]),
        semanas=dict(rotulos=sem_lbl, series=serie, nomes={k: CATEGORIAS[k]["nome"] for k in serie}),
        politicas=dict(itens=politicas, extra=politica_extra),
        iniciativas=iniciativas, evitaveis=evitaveis,
        automacao=dict(niveis=auto_res, motivos_humano=motivos_humano,
                       matriz=[dict(categoria=k, nome=CATEGORIAS[k]["nome"], nivel=v[0], como=v[1],
                                    conversas=int((d.categoria == k).sum())) for k, v in AUTOMACAO.items()]),
        fila=dict(itens=fila_rows[:300], total=len(fila_rows), resumo=fila_resumo),
        qualidade=qualidade, conversas=conversas,
    )


def _brl(v):
    s = f"{v:,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def gerar_achados(ex, motivos, canais, transp, mudancas, produtos, politicas, pe, ev, conc, marcas, sla_wa, fator_mes, d):
    """Frases curtas, sempre calculadas a partir dos dados (mudam com o upload e os parâmetros)."""
    a = []
    top = [m for m in motivos if m["categoria"] != "sem_mensagem"]
    ent = [m for m in motivos if m["grupo"] == "Entrega"]
    if ent:
        v = sum(m["conversas"] for m in ent); c = sum(m["custo_mes"] for m in ent)
        a.append(dict(titulo="Entrega é o maior motivo",
                      texto=f"'Onde está meu pedido' e atraso somam {_pct(v, ex['conversas_amostra'])}% das conversas e {_brl(c)}/mês ({_pct(c, ex['custo_mes'])}% do custo).",
                      nivel="alto"))
    if transp and transp[0]["indice"] and transp[0]["indice"] > 1.3:
        t = transp[0]
        a.append(dict(titulo=f"{t['transportadora']} concentra o problema",
                      texto=f"Tem {t['pct_pedidos']}% dos pedidos e {t['pct_contatos_entrega']}% dos contatos de entrega ({t['indice']}x o esperado), quase todos no Nordeste.",
                      nivel="alto"))
    ra = next((c for c in canais if c["canal"] == "reclame_aqui"), None)
    if ra:
        a.append(dict(titulo="Reclame Aqui pesa mais que o volume",
                      texto=f"É {ra['pct_volume']}% das conversas e {ra['pct_custo']}% do custo; {_pct(ev['ra_recontato'], ev['ra_total'])}% dos casos no RA são de quem já tinha procurado antes.",
                      nivel="alto"))
    if pe["saude_total"]:
        a.append(dict(titulo="Reações adversas sem protocolo",
                      texto=f"{pe['saude_total']} relatos na amostra (~{_num(pe['saude_total'] * fator_mes, 0)}/mês) e {pe['saude_com_registro']} com registro de cosmetovigilância. É risco sanitário e legal, não de custo.",
                      nivel="critico"))
    for m in mudancas[:3]:
        if m["variacao"] and m["variacao"] >= 80:
            a.append(dict(titulo=f"Salto: {m['nome']}",
                          texto=f"De {m['antes']} para {m['depois']} conversas/semana na amostra ({'+' if m['variacao'] > 0 else ''}{m['variacao']}%)" + (f", desde a semana de {m['desde']}." if m.get("desde") else "."),
                          nivel="medio"))
    if produtos.get("avaria"):
        p0 = produtos["avaria"][0]
        if p0["pct_da_categoria"] and p0["pct_da_categoria"] >= 15:
            a.append(dict(titulo="Um produto concentra as avarias",
                          texto=f"{p0['produto']} ({p0['marca']}): {p0['pct_da_categoria']}% das reclamações de avaria/vazamento.", nivel="medio"))
    a.append(dict(titulo="Recontato e falso 'resolvido'",
                  texto=f"{ex['pct_recontato']}% das conversas são recontato do mesmo pedido ({_brl(ev['custo_recontatos_mes'])}/mês). {pe['resolvido_sem_resolver']} conversas marcadas como 'resolvido' voltaram depois.",
                  nivel="alto"))
    a.append(dict(titulo="As tags dos agentes não são confiáveis",
                  texto=f"Só {conc['flexivel']}% das tags batem com o conteúdo (estrita: {conc['estrita']}%). Decidir pelo relatório de tags leva a prioridades erradas.",
                  nivel="medio"))
    if sla_wa.get("pct_fora_horario"):
        a.append(dict(titulo="Fora do horário, só o bot (que não resolve)",
                      texto=f"{sla_wa['pct_fora_horario']}% dos clientes do WhatsApp escrevem fora das 8h-20h e esperam em mediana {sla_wa['espera_mediana_fora_h']}h pela 1ª resposta.",
                      nivel="medio"))
    return a
