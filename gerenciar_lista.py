#!/usr/bin/env python3
"""Mantem tabelas_ativas.json -- a lista de tabelas que o Actions deve
retomar automaticamente a cada execucao agendada, ate cada uma terminar."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CAMINHO = Path(__file__).resolve().parent / "tabelas_ativas.json"


def carregar() -> list[dict]:
    if CAMINHO.is_file():
        try:
            return json.loads(CAMINHO.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def salvar(lista: list[dict]) -> None:
    CAMINHO.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")


def adicionar(tabela: str, descricao: str | None, filtro: str | None) -> None:
    lista = carregar()
    for item in lista:
        if item["tabela"] == tabela and (item.get("filtro") or None) == (filtro or None):
            if descricao:
                item["descricao"] = descricao
            salvar(lista)
            return
    lista.append({"tabela": tabela, "descricao": descricao or None, "filtro": filtro or None})
    salvar(lista)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("acao", choices=["adicionar", "listar", "remover"])
    ap.add_argument("--tabela")
    ap.add_argument("--descricao", default=None)
    ap.add_argument("--filtro", default=None)
    args = ap.parse_args()

    if args.acao == "adicionar":
        adicionar(args.tabela, args.descricao, args.filtro)
        print(f"adicionada/atualizada: {args.tabela}")
    elif args.acao == "remover":
        lista = [i for i in carregar() if i["tabela"] != args.tabela]
        salvar(lista)
        print(f"removida: {args.tabela}")
    elif args.acao == "listar":
        for item in carregar():
            print(json.dumps(item, ensure_ascii=False))


if __name__ == "__main__":
    main()
