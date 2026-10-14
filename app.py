"""API + página do painel de atendimento.

Sem estado entre requisições (pronto para serverless/Vercel):
- GET  /api/analise  -> analisa os dados originais de dados/ com os parâmetros padrão
- POST /api/analise  -> multipart com os 3 CSVs (opcionais em conjunto) + campo 'params' (JSON)
- GET  /api/agente/status -> se o agente de IA está ligado (sem expor a chave) e os limites
- POST /api/agente   -> roda o agente em UMA conversa (campo 'conversation_id'; CSVs opcionais, como no /api/analise)
Toda requisição traz tudo de que precisa; nada fica guardado no servidor.

Rodar localmente:  python app.py   (http://127.0.0.1:8766)
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import agente
import analise

agente.carregar_env()  # só localmente: na Vercel valem as Environment Variables do projeto

BASE = Path(__file__).resolve().parent
MAX_ARQUIVO = 20 * 1024 * 1024  # 20 MB por arquivo (na Vercel o limite do corpo é 4,5 MB no total)
MAX_CORPO_AGENTE = 12 * 1024 * 1024
limite_agente = agente.LimitePorIP()

app = FastAPI(title="Atendimento Gocase + GoBeauté: motivos, custo e ação", docs_url="/api/docs", redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def cabecalhos_seguranca(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@lru_cache(maxsize=1)
def _pagina() -> str:
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")


@lru_cache(maxsize=4)
def _analise_padrao() -> dict:
    # resultado derivado só dos arquivos do repositório: pode ser memorizado sem quebrar a ausência de estado
    return analise.analisar()


@app.get("/", response_class=HTMLResponse)
def pagina():
    return HTMLResponse(_pagina())


@app.get("/api/saude")
def saude():
    return {"ok": True}


@app.get("/api/parametros")
def parametros():
    return analise.DEFAULT_PARAMS


@app.get("/api/analise")
def analise_padrao():
    return JSONResponse(_analise_padrao())


async def _ler(arq: UploadFile | None, nome: str) -> bytes | None:
    if arq is None or not arq.filename:
        return None
    dados = await arq.read(MAX_ARQUIVO + 1)
    if len(dados) > MAX_ARQUIVO:
        raise HTTPException(413, f"{nome}: arquivo maior que {MAX_ARQUIVO // (1024 * 1024)} MB")
    if not dados.strip():
        raise HTTPException(400, f"{nome}: arquivo vazio")
    return dados


@app.post("/api/analise")
async def analise_upload(
    conversas: UploadFile | None = File(None),
    mensagens: UploadFile | None = File(None),
    pedidos: UploadFile | None = File(None),
    params: str = Form("{}"),
):
    try:
        p = json.loads(params or "{}")
        if not isinstance(p, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "params deve ser um JSON com os parâmetros")

    arquivos = [await _ler(conversas, "conversas.csv"), await _ler(mensagens, "mensagens.csv"), await _ler(pedidos, "pedidos.csv")]
    enviados = sum(a is not None for a in arquivos)
    if enviados not in (0, 3):
        raise HTTPException(400, "Envie os três arquivos juntos (conversas, mensagens e pedidos) ou nenhum.")
    try:
        if enviados == 3:
            nomes = ", ".join(a.filename for a in (conversas, mensagens, pedidos))
            res = analise.analisar(*arquivos, params=p, origem=f"upload: {nomes}")
        else:
            res = analise.analisar(params=p)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return JSONResponse(res)


def _ip(request: Request) -> str:
    # na Vercel o IP real do cliente vem no primeiro item de x-forwarded-for
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or request.headers.get("x-real-ip", "") or
            (request.client.host if request.client else "")) or "desconhecido"


@app.get("/api/agente/status")
def agente_status():
    return agente.status()


@app.post("/api/agente")
async def agente_rodar(
    request: Request,
    conversation_id: str = Form(""),
    conversas: UploadFile | None = File(None),
    mensagens: UploadFile | None = File(None),
    pedidos: UploadFile | None = File(None),
):
    if int(request.headers.get("content-length") or 0) > MAX_CORPO_AGENTE:
        raise HTTPException(413, "Requisição grande demais para o agente.")
    if not agente.ligado():
        raise HTTPException(503, "Agente de IA desligado: defina AI_API_KEY e AI_BASE_URL nas variáveis de ambiente. "
                                 "O restante do painel funciona normalmente.")
    if not limite_agente.permitir(_ip(request)):
        raise HTTPException(429, f"Muitas chamadas ao agente: o limite é {agente.LIMITE_POR_MINUTO} por minuto. Tente de novo em instantes.")
    cid = (conversation_id or "").strip()
    if not agente.RE_ID.match(cid):
        raise HTTPException(400, "Informe um ID de conversa válido (letras, números, '-' ou '_', até 40 caracteres).")
    arquivos = [await _ler(conversas, "conversas.csv"), await _ler(mensagens, "mensagens.csv"), await _ler(pedidos, "pedidos.csv")]
    enviados = sum(a is not None for a in arquivos)
    if enviados not in (0, 3):
        raise HTTPException(400, "Envie os três arquivos juntos (conversas, mensagens e pedidos) ou nenhum.")
    try:
        base = await run_in_threadpool(agente.Base.de_fontes, *arquivos) if enviados == 3 else agente.base_original()
        return JSONResponse(await run_in_threadpool(agente.rodar, cid, base))
    except agente.CasoNaoEncontrado:
        raise HTTPException(404, f"Conversa {cid} não encontrada nos dados em uso.")
    except agente.ErroIA as e:
        raise HTTPException(502, f"O agente não conseguiu concluir: {e}.")
    except ValueError as e:
        raise HTTPException(422, str(e))


if __name__ == "__main__":
    import uvicorn

    porta = int(os.environ.get("PORT", "8766"))
    print(f"Painel em http://127.0.0.1:{porta}")
    uvicorn.run(app, host="127.0.0.1", port=porta)
