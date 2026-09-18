#!/usr/bin/env python3
"""
Roda no gatilho agendado (cron) do GitHub Actions: passa pelas tabelas em
tabelas_ativas.json (NA ORDEM em que estao, ver nota abaixo) e retoma cada
uma que ainda nao terminou, respeitando um ORCAMENTO TOTAL para toda esta
execucao -- nao um pedaco fixo por tabela.

Por que orcamento TOTAL (nao fixo por tabela): se dividir 19200s por 65
tabelas de forma fixa (295s cada), uma tabela grande nunca teria tempo
suficiente e o job do Actions ainda corre risco de ser encerrado (limite
de 6h) no meio de uma chamada, perdendo o que nao foi publicado. Em vez
disso, cada tabela recebe "o que resta do orcamento total" -- tabelas
pequenas terminam sozinhas em segundos/minutos (nao usam o tempo todo) e
sobra mais orcamento pras proximas; as grandes usam o que restar, sempre
parando e publicando o progresso ANTES do orcamento total acabar.

Nota sobre ordem: tabelas_ativas.json foi montado com as tabelas pequenas
primeiro e as gigantes (OPME, etc.) por ultimo de proposito -- assim as
~60 tabelas pequenas terminam de vez na primeira execucao, e o tempo que
resta (a maior parte) vai pras grandes.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
TEMPO_TOTAL_S = 19200  # 5h20 -- o job do Actions tem timeout de 5h50 (ver workflow)
MARGEM_MINIMA_S = 120  # nao vale a pena iniciar mais uma tabela com menos que isso


def main() -> None:
    sys.path.insert(0, str(RAIZ))
    import gerenciar_lista as gl

    lista = gl.carregar()
    if not lista:
        print("nenhuma tabela ativa -- nada a fazer.")
        return

    inicio = time.time()
    for item in lista:
        decorrido = time.time() - inicio
        restante = TEMPO_TOTAL_S - decorrido
        if restante < MARGEM_MINIMA_S:
            print(f"orçamento total desta execução esgotado ({decorrido:.0f}s decorridos) "
                  f"-- as tabelas restantes ficam para a próxima execução agendada.")
            break

        tabela = item["tabela"]
        cmd = [sys.executable, str(RAIZ / "sync_ci.py"), "--tabela", tabela, "--tempo-max-s", str(int(restante))]
        if item.get("descricao"):
            cmd += ["--descricao", item["descricao"]]
        if item.get("filtro"):
            cmd += ["--filtro", item["filtro"]]
        print(f"=== [{decorrido:.0f}s decorridos / {restante:.0f}s restantes no orçamento total] {tabela} ===")
        subprocess.run(cmd, check=False)

        estado_path = RAIZ / "dados" / f"{tabela}.json"
        if estado_path.is_file():
            try:
                estado = json.loads(estado_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                estado = {}
            if estado.get("concluido"):
                nova_lista = [
                    i for i in gl.carregar()
                    if not (i["tabela"] == tabela and (i.get("filtro") or None) == (item.get("filtro") or None))
                ]
                gl.salvar(nova_lista)
                print(f"'{tabela}' concluída -- removida da lista de tabelas ativas.")


if __name__ == "__main__":
    main()
