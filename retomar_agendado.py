#!/usr/bin/env python3
"""
Roda no gatilho agendado (cron) do GitHub Actions: passa por todas as
tabelas em tabelas_ativas.json e retoma cada uma que ainda nao terminou,
dividindo o tempo desta execucao entre elas. Tabela que termina eh
removida da lista automaticamente (nao precisa mais ser retomada).
"""
import json
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
TEMPO_TOTAL_S = 19200  # 5h20 no total desta execucao, dividido entre as tabelas ativas


def main() -> None:
    sys.path.insert(0, str(RAIZ))
    import gerenciar_lista as gl

    lista = gl.carregar()
    if not lista:
        print("nenhuma tabela ativa -- nada a fazer.")
        return

    fatia = max(1200, TEMPO_TOTAL_S // len(lista))
    for item in list(lista):
        tabela = item["tabela"]
        cmd = [sys.executable, str(RAIZ / "sync_ci.py"), "--tabela", tabela, "--tempo-max-s", str(fatia)]
        if item.get("descricao"):
            cmd += ["--descricao", item["descricao"]]
        if item.get("filtro"):
            cmd += ["--filtro", item["filtro"]]
        print("=== executando:", " ".join(cmd), "===")
        subprocess.run(cmd, check=False)

        estado_path = RAIZ / "dados" / f"{tabela}.json"
        if estado_path.is_file():
            try:
                estado = json.loads(estado_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                estado = {}
            if estado.get("concluido"):
                gl.adicionar.__globals__  # no-op, so pra deixar claro que usamos o modulo
                nova_lista = [i for i in gl.carregar() if not (i["tabela"] == tabela and (i.get("filtro") or None) == (item.get("filtro") or None))]
                gl.salvar(nova_lista)
                print(f"'{tabela}' concluida -- removida da lista de tabelas ativas.")


if __name__ == "__main__":
    main()
