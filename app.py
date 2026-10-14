"""API + página do painel de atendimento.

Sem estado entre requisições (pronto para serverless/Vercel):
- GET  /api/analise  -> analisa os dados originais de dados/ com os parâmetros padrão
- POST /api/analise  -> multipart com os 3 CSVs (opcionais em conjunto) + campo 'params' (JSON)
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

import analise

BASE = Path(__file__).resolve().parent
MAX_ARQUIVO = 20 * 1024 * 1024  # 20 MB por arquivo (na Vercel o limite do corpo é 4,5 MB no total)

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


if __name__ == "__main__":
    import uvicorn

    porta = int(os.environ.get("PORT", "8766"))
    print(f"Painel em http://127.0.0.1:{porta}")
    uvicorn.run(app, host="127.0.0.1", port=porta)
