"""Classificação de conversas de atendimento por regras (sem LLM, sem rede).

O texto do cliente é tratado como ENTRADA NÃO CONFIÁVEL:
- dados pessoais (CPF, cartão, telefone, e-mail) são mascarados antes de qualquer exibição;
- tentativas de dar ordens ao "sistema" (prompt injection) são detectadas, isoladas e
  nunca executadas: o trecho é removido antes de classificar e a conversa vai para revisão humana;
- nomes de produto são retirados antes da busca por palavras-chave, para que palavras do
  catálogo (ex.: "Glow", "Fix", "Protein") não virem motivo de contato.

A classificação é feita por trecho: a primeira mensagem do cliente é quebrada nos conectores
de "mais de um assunto" ("Ah, e também", "Aproveitando,", "E outra coisa:"). O primeiro trecho
define o motivo principal; os demais viram motivos secundários. Há tratamento de negação
("não veio quebrado") e de ironia ("Adorei..., só que não", "Parabéns pela eficiência 👏").
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Taxonomia
# ---------------------------------------------------------------------------
# tag_esperada: tag da lista oficial que o agente deveria ter usado
# tags_aceitas: outras tags da lista que consideramos aceitáveis (concordância "flexível")
CATEGORIAS: dict[str, dict] = {
    "rastreio": dict(nome="Onde está meu pedido", grupo="Entrega", tag="Rastreio", aceitas={"Atraso"}),
    "atraso": dict(nome="Atraso na entrega (prazo vencido)", grupo="Entrega", tag="Atraso", aceitas={"Rastreio"}),
    "item_faltando": dict(nome="Item faltando no pedido", grupo="Pedido incompleto", tag="Produto faltando", aceitas=set()),
    "brinde": dict(nome="Brinde da campanha não veio", grupo="Pedido incompleto", tag="Produto faltando", aceitas={"Promoção"}),
    "avaria": dict(nome="Avaria ou vazamento", grupo="Qualidade / avaria", tag="Avaria", aceitas={"Reclamação produto"}),
    "personalizacao": dict(nome="Erro de personalização (Gocase)", grupo="Qualidade / avaria", tag="Reclamação produto", aceitas={"Avaria", "Troca"}),
    "reacao_adversa": dict(nome="Reação adversa (saúde)", grupo="Saúde", tag="Reclamação produto", aceitas=set()),
    "insatisfacao": dict(nome="Não gostou (fragrância/produto)", grupo="Troca e devolução", tag="Troca", aceitas={"Reclamação produto"}),
    "troca_erro_compra": dict(nome="Troca por erro na compra (modelo/tamanho/cor)", grupo="Troca e devolução", tag="Troca", aceitas=set()),
    "arrependimento": dict(nome="Arrependimento / devolução", grupo="Troca e devolução", tag="Cancelamento", aceitas={"Troca"}),
    "cancelamento": dict(nome="Cancelamento (duplicado, recompra com cupom)", grupo="Pedido / compra", tag="Cancelamento", aceitas=set()),
    "cobranca_indevida": dict(nome="Cobrança duplicada ou não reconhecida", grupo="Financeiro", tag="Financeiro", aceitas=set()),
    "pagamento_pendente": dict(nome="Pagou e o pedido não foi liberado", grupo="Financeiro", tag="Financeiro", aceitas=set()),
    "cashback": dict(nome="Cashback não recebido", grupo="Financeiro", tag="Financeiro", aceitas={"Promoção"}),
    "cupom_promocao": dict(nome="Cupom, promoção ou frete grátis não aplicou", grupo="Pedido / compra", tag="Promoção", aceitas=set()),
    "endereco": dict(nome="Alteração de endereço", grupo="Pedido / compra", tag="Endereço", aceitas=set()),
    "duvida_produto": dict(nome="Dúvida sobre produto", grupo="Dúvida", tag="Dúvida", aceitas=set()),
    "duvida_personalizacao": dict(nome="Dúvida sobre personalização", grupo="Dúvida", tag="Dúvida", aceitas=set()),
    "elogio": dict(nome="Elogio", grupo="Elogio", tag="Elogio", aceitas=set()),
    "sem_mensagem": dict(nome="Abriu o chat e não escreveu", grupo="Sem conteúdo", tag="", aceitas=set()),
    "outros": dict(nome="Não classificado", grupo="Outros", tag="Outros", aceitas=set()),
}

TAGS_OFICIAIS = [
    "Rastreio", "Atraso", "Produto faltando", "Avaria", "Troca", "Cancelamento",
    "Reclamação produto", "Financeiro", "Dúvida", "Promoção", "Endereço", "Elogio", "Outros",
]


@lru_cache(maxsize=100_000)
def sem_acento(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(ch for ch in texto if not unicodedata.combining(ch))


def normalizar(texto: str) -> str:
    """minúsculas, sem acento, espaços colapsados."""
    if not isinstance(texto, str):
        return ""
    return _normalizar(texto)


@lru_cache(maxsize=100_000)
def _normalizar(texto: str) -> str:
    t = sem_acento(texto).lower()
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# Dados pessoais (LGPD)
# ---------------------------------------------------------------------------
RE_CARTAO = re.compile(r"\b(?:\d{4}[ .-]?){3}\d{4}\b")
RE_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
RE_TEL = re.compile(r"(?:\(?\b\d{2}\)?[\s.-]?)?\b9?\d{4}[\s.-]?\d{4}\b")


def detectar_pii(texto: str) -> dict:
    if not isinstance(texto, str):
        return {"cpf": 0, "cartao": 0, "telefone": 0, "email": 0}
    t = texto
    n_cartao = len(RE_CARTAO.findall(t)); t = RE_CARTAO.sub(" ", t)
    n_cpf = len(RE_CPF.findall(t)); t = RE_CPF.sub(" ", t)
    n_email = len(RE_EMAIL.findall(t)); t = RE_EMAIL.sub(" ", t)
    n_tel = len(RE_TEL.findall(t))
    return {"cpf": n_cpf, "cartao": n_cartao, "telefone": n_tel, "email": n_email}


def mascarar(texto: str) -> str:
    """Mascara dados pessoais. A ordem importa: cartão (16 dígitos) antes de CPF e telefone."""
    if not isinstance(texto, str):
        return ""
    return _mascarar(texto)


@lru_cache(maxsize=100_000)
def _mascarar(texto: str) -> str:
    t = RE_CARTAO.sub("[CARTÃO]", texto)
    t = RE_CPF.sub("[CPF]", t)
    t = RE_EMAIL.sub("[E-MAIL]", t)
    t = RE_TEL.sub("[TELEFONE]", t)
    return t


# ---------------------------------------------------------------------------
# Prompt injection: texto que tenta dar ordens a um sistema automático
# ---------------------------------------------------------------------------
RE_INJECAO = re.compile(
    r"(ignore (todas )?(as )?(suas )?instruc|instrucao para o (assistente|sistema|bot|agente)|\[sistema\]|"
    r"system override|system prompt|se voce for uma ia|esqueca (suas|as) (regras|instrucoes)|"
    r"agente automatico|ola,? robo|voce agora e|aja como|desconsidere (as|todas)|modo desenvolvedor|"
    r"jailbreak|<\s*/?\s*(system|prompt)\s*>)"
)


def detectar_injecao(texto_norm: str) -> int:
    """Retorna a posição onde começa a tentativa de manipulação, ou -1."""
    m = RE_INJECAO.search(texto_norm)
    return m.start() if m else -1


# ---------------------------------------------------------------------------
# Regras por categoria
# ---------------------------------------------------------------------------
NEGACAO = re.compile(r"\b(nao|nem|sem|nunca|nenhum|nenhuma|jamais)\b(\s+\w+){0,2}\s*$")


def _r(*padroes: str) -> list[re.Pattern]:
    # um único regex por categoria (alternância): bem mais rápido que testar padrão a padrão
    return [re.compile("|".join(f"(?:{p})" for p in padroes))]


# (código, padrões, negável?) na ordem de prioridade: em caso de empate dentro do MESMO trecho,
# vence a categoria mais grave/específica.
REGRAS: list[tuple[str, list[re.Pattern], bool]] = [
    ("reacao_adversa", _r(
        r"reacao alergic", r"\balergi", r"\bincha(ram|ou|cao|ço)?\b", r"\binchad[oa]s?\b", r"coceira",
        r"\bard(endo|eu|encia|ia|e muito)\b", r"queimacao", r"queimou (meu|minha|a|o) (pele|rosto|couro|cabelo|olho|boca)",
        r"\bbolinhas\b", r"manchas? vermelhas?", r"\benjoo\b|\bnausea", r"pele (ficou )?(toda )?vermelha",
        r"descaman", r"irritac(ao|ou)", r"\bbrotoeja", r"falta de ar"), True),
    ("cobranca_indevida", _r(
        r"cobrad[ao]s? (duas|2|tres|3) vezes", r"saiu (duas|2) vezes", r"cobrad[ao] de novo", r"debitad[ao] (duas|2) vezes",
        r"cobranca .{0,40}nao reconhe", r"nao reconhe\w* .{0,30}cobranca", r"cobranca (em )?duplicad", r"cobranca indevida",
        r"valor (cobrado )?a mais"), False),
    ("pagamento_pendente", _r(
        r"pix .{0,40}(nao confirm|nao (foi )?(compensad|identificad|reconhecid|aprovad))",
        r"(boleto|pix) (ja )?(foi )?pago .{0,50}(nao liber|aguardando pagamento|nao confirm|nao aprov)",
        r"aguardando pagamento", r"pagamento .{0,20}nao (foi )?(confirmad|aprovad|identificad)"), False),
    ("personalizacao", _r(
        r"personalizacao (veio |saiu |chegou |esta )?(toda )?(manchad|borrad|errad|tort|apagad|descasc)",
        r"tinta escorreu", r"impressao .{0,50}(borrad|tort|descasc|manchad|apagad|errad|cortad)",
        r"\b(nome|iniciais|foto|frase)\b .{0,40}(escrito errado|vier(am)? errad|veio errad|sairam errad|saiu errad)",
        r"\b(nome|iniciais|foto)\b .{0,30}(borrad|tort|cortad)", r"veio a (foto )?de outra pessoa",
        r"pedi (as iniciais |o nome )?\"[^\"]+\" e veio", r"pedi as iniciais \w+ .{0,30}veio"), True),
    ("brinde", _r(r"\bbrindes?\b", r"\bmimos?\b"), True),
    ("item_faltando", _r(
        r"\bfaltou\b", r"\bfaltando\b", r"\bfaltante", r"veio pela metade", r"(kit|pedido|caixa) (chegou|veio) incomplet",
        r"chegou so (o|a|um|uma)\b", r"cade o resto", r"comprei \d+ e (chegaram|vieram|veio|chegou) \d+",
        r"o que faltou", r"nao veio (o|a|um|uma) (item|produto)"), True),
    ("avaria", _r(
        r"quebrad", r"trincad", r"rachad", r"vazand", r"\bvazou\b", r"vazament", r"\bmelad", r"amassad", r"arranhad",
        r"riscad", r"lacre violad", r"veio abert", r"caixa (veio )?abert", r"danificad", r"avariad", r"estragad",
        r"cara de usado"), True),
    ("atraso", _r(
        r"atrasad", r"\batraso\b", r"prazo .{0,40}(venceu|vencido|estourad|passou|expirou)", r"passou do prazo",
        r"prometeram (entregar|para|pra)", r"nada ate hoje", r"dias de atraso", r"fora do prazo"), False),
    ("rastreio", _r(
        r"rastrei", r"cade (o |meu )?pedido", r"onde (esta|ta)\b", r"status do pedido", r"(ainda )?nao chegou",
        r"em transito", r"parado (em|na|no|ha)\b", r"nao atualiza", r"ate agora nada", r"codigo de rastr",
        r"previsao de entrega", r"quando (vai )?cheg"), False),
    ("arrependimento", _r(
        r"arrependi", r"\bdesisti\b", r"dinheiro de volta", r"\bdevolv", r"\bdevoluc", r"\breembols", r"direito de arrependimento"), True),
    ("cancelamento", _r(
        r"\bcancel", r"comprei (sem querer )?(duas|2) vezes", r"compra duplicad", r"pedido duplicad"), True),
    ("troca_erro_compra", _r(
        r"(tamanho|modelo|cor|numero|versao)( de celular| do celular| do aparelho)? errad",
        r"escolhi (a|o) (cor|modelo|tamanho|versao) errad", r"comprei (o|a) (modelo|tamanho|cor) .{0,25}errad",
        r"nao (serve|serviu) no (meu )?(celular|aparelho)"), False),
    ("insatisfacao", _r(
        r"nao gostei", r"cheiro (e |eh )?(muito )?(forte|doce|ruim|enjoativo|fraco)", r"fragrancia .{0,40}diferente",
        r"trocar? .{0,50}(fragrancia|cheiro)", r"cheiro some", r"nao fixa", r"esperava mais", r"nao era o que eu esperava",
        r"nao (funcionou|fez efeito)", r"\bgostaria de trocar\b", r"\bquero trocar\b"), False),
    ("cashback", _r(r"cash ?back"), False),
    ("cupom_promocao", _r(
        r"\bcupo(m|ns)\b", r"promocao .{0,40}nao (aplicou|funcion|apareceu|entrou)", r"frete gratis",
        r"desconto .{0,30}nao (aplicou|apareceu|entrou)", r"\bpromocao\b"), False),
    ("endereco", _r(r"endereco", r"numero da casa", r"\bcep\b", r"mudar .{0,25}entrega", r"\bcomplemento\b"), False),
    ("duvida_personalizacao", _r(
        r"(quanto tempo|prazo|demora) .{0,25}(personaliz|refazer|produc)", r"mudar (o nome|as iniciais|a foto|a frase) .{0,40}depois",
        r"(posso|consigo) (mudar|alterar|trocar) (o nome|as iniciais|a foto)"), False),
    ("duvida_produto", _r(
        r"vegan", r"gravidez|gravida|amamenta", r"pele (oleosa|seca|sensivel|mista)", r"\bvalidade\b", r"cabelo com quimica",
        r"diferenca entre", r"quantas vezes", r"formula|ingrediente|composicao", r"carregador|inducao", r"aguenta queda",
        r"mantem a bebida|gelad", r"espaco para", r"durabilidade", r"serve (pra|para)", r"pode (ser )?usa(r|do)", r"como (uso|usar|aplic)",
        r"\btem (em|no) estoque\b", r"cruelty"), False),
    ("elogio", _r(
        r"\bamei\b", r"\badorei\b", r"\bperfeit", r"melhor compra", r"(passando|passar) (pra|para) agradecer",
        r"atendimento (foi )?(otimo|excelente|maravilhos|incrivel)", r"\bamo (essa|esse|a|o)\b", r"recomendo"), True),
]

PRIORIDADE = {cod: i for i, (cod, _, _) in enumerate(REGRAS)}

# Ironia/sarcasmo: impede que "Adorei..., só que não" vire elogio e marca o tom como negativo.
RE_IRONIA = re.compile(
    r"(so que nao|parabens pela eficiencia|que capricho|impecavel, so que nao|\U0001F44F|otimo, mais um|"
    r"que maravilha,? (chegou|veio|mais)|nossa, que (rapido|eficiente)|obrigad[ao] por nada)"
)
# Prefixos que indicam recontato e conectores de vários assuntos
RE_PREFIXO_RECONTATO = re.compile(
    r"^\s*(de novo aqui\.?|ja e a (\d+)\s*a? vez que (eu )?entro em contato\.?|ninguem me respondeu ainda!?|"
    r"(pela )?(\d+)a? vez (que )?(eu )?(entro em contato|escrevo)\.?|mais uma vez\.?)\s*"
)
RE_CONECTOR = re.compile(r"\s+(?:ah,? e tambem|aproveitando,|e outra coisa:|alem disso,|outra coisa:)\s+")
RE_SUFIXO_PII = re.compile(r"\s*(meu cpf e|me liga no|o cartao cobrado foi o|meu telefone e|meu e-?mail e)\s*\[[^\]]+\]\s*")

# Sinais nas mensagens seguintes do cliente
RE_AMEACA = re.compile(r"(vou abrir reclamac|reclame aqui|procon|advogad|processar|justica|desrespeito|absurdo|vou denunciar)")
RE_URGENCIA = re.compile(r"(resposta hoje|urgente|presente|nao chega a tempo|ate sexta|preciso (pra|para) (hoje|amanha))")
RE_FOTO = re.compile(r"(segue a foto|foto em anexo|mandei a foto|enviei a foto|segue foto)")
RE_ALEGACAO_PERSONALIZACAO = re.compile(r"pedi (?:as iniciais |o nome )?\"([^\"]+)\" e veio \"([^\"]+)\"")


@dataclass
class Resultado:
    categoria: str = "outros"
    secundarias: list[str] = field(default_factory=list)
    trechos: list[str] = field(default_factory=list)
    recontato_texto: bool = False
    vezes_declaradas: int = 0
    ironia: bool = False
    injecao: bool = False
    ameaca: bool = False
    urgencia: bool = False
    foto_enviada: bool = False
    saude_em_qualquer_msg: bool = False
    alegacao_sem_divergencia: bool = False
    confianca: str = "baixa"
    regra: str = ""


def _casa(padroes: list[re.Pattern], texto: str, negavel: bool) -> str | None:
    for p in padroes:
        for m in p.finditer(texto):
            if negavel and NEGACAO.search(texto[max(0, m.start() - 30):m.start()]):
                continue  # ex.: "não veio quebrado", "não tive alergia"
            return m.group(0)
    return None


def classificar_trecho(trecho: str, ironia: bool = False) -> tuple[str, str]:
    """Retorna (categoria, padrão que casou). Usa a ordem de prioridade das REGRAS."""
    for cod, padroes, negavel in REGRAS:
        if cod == "elogio" and ironia:
            continue
        pad = _casa(padroes, trecho, negavel)
        if pad:
            return cod, pad
    return "outros", ""


def remover_produtos(texto_norm: str, produtos_norm: list[str]) -> str:
    for p in produtos_norm:
        if p and p in texto_norm:
            texto_norm = texto_norm.replace(p, " <produto> ")
    return re.sub(r"\s+", " ", texto_norm)


def preparar(texto: str, produtos_norm: list[str]) -> tuple[str, bool]:
    """Mascara PII, normaliza, remove nomes de produto e corta a tentativa de injeção."""
    t = normalizar(mascarar(texto))
    t = remover_produtos(t, produtos_norm)
    pos = detectar_injecao(t)
    injecao = pos >= 0
    if injecao:
        t = t[:pos].strip()
    return t, injecao


def classificar_conversa(msgs_cliente: list[str], produtos_norm: list[str] | None = None) -> Resultado:
    """msgs_cliente: textos do cliente em ordem cronológica."""
    produtos_norm = produtos_norm or []
    r = Resultado()
    if not msgs_cliente:
        r.categoria, r.confianca, r.regra = "sem_mensagem", "alta", "sem mensagem do cliente"
        return r

    preparados = [preparar(m, produtos_norm) for m in msgs_cliente]
    r.injecao = any(inj for _, inj in preparados)
    textos = [t for t, _ in preparados]
    todos = " | ".join(textos)
    r.ironia = bool(RE_IRONIA.search(todos)) or "\U0001F44F" in " ".join(msgs_cliente)
    r.ameaca = bool(RE_AMEACA.search(todos))
    r.urgencia = bool(RE_URGENCIA.search(todos))
    r.foto_enviada = bool(RE_FOTO.search(todos))

    # 1) primeira mensagem: prefixo de recontato + quebra em trechos
    primeira = textos[0]
    m = RE_PREFIXO_RECONTATO.match(primeira)
    if m:
        r.recontato_texto = True
        num = m.group(2) or m.group(5)
        r.vezes_declaradas = int(num) if num else 2
        primeira = primeira[m.end():]
    primeira = RE_SUFIXO_PII.sub(" ", primeira).strip()
    trechos = [t.strip(" .") for t in RE_CONECTOR.split(primeira) if t.strip(" .")]
    r.trechos = trechos

    cats: list[str] = []
    regra_principal = ""
    for i, tr in enumerate(trechos):
        cat, pad = classificar_trecho(tr, r.ironia)
        if i == 0:
            regra_principal = pad
        if cat != "outros" and cat not in cats:
            cats.append(cat)

    # 2) se a primeira mensagem não diz nada útil, olha as seguintes
    origem = "1ª mensagem"
    if not cats:
        for t in textos[1:]:
            t2 = RE_SUFIXO_PII.sub(" ", t)
            cat, pad = classificar_trecho(t2, r.ironia)
            if cat != "outros":
                cats.append(cat)
                regra_principal = pad
                origem = "mensagem seguinte"
                break

    # 3) sinais de saúde em QUALQUER mensagem (ex.: "já parei de usar, mas continua ardendo")
    padroes_saude = REGRAS[0][1]
    r.saude_em_qualquer_msg = any(_casa(padroes_saude, t, True) for t in textos)

    if cats:
        r.categoria = cats[0]
        r.secundarias = cats[1:]
        r.regra = f"{origem}: \"{regra_principal}\""
        r.confianca = "alta" if origem == "1ª mensagem" and len(trechos) == 1 else "média"
    else:
        r.categoria, r.confianca, r.regra = "outros", "baixa", "nenhuma regra casou"

    # 4) alegação de erro de personalização sem divergência (pediu "Ana", veio "Ana")
    for t in msgs_cliente:
        for pedido, veio in RE_ALEGACAO_PERSONALIZACAO.findall(normalizar(t)):
            if pedido.strip() == veio.strip():
                r.alegacao_sem_divergencia = True
    return r
