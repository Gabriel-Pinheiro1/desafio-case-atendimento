"""Os testes nunca chamam a rede: qualquer tentativa de abrir uma URL falha na hora."""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    def bloqueado(*a, **k):
        raise AssertionError("teste tentou acessar a rede")
    monkeypatch.setattr(urllib.request, "urlopen", bloqueado)
