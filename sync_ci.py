#!/usr/bin/env python3
"""
Sincronizador de tabelas TUSS que roda no GitHub Actions, de forma
RETOMAVEL entre execucoes (o Actions tem limite de 6h por execucao; tabelas
grandes como a TUSS 19/OPME, com ~60 mil paginas, levam mais que isso).

Como funciona:
  1. Baixa o progresso salvo anteriormente (se existir) do GitHub Release
     da tabela (tag "dados-<tabela>", asset "<tabela>.db.gz" + "<tabela>.json").
  2. Continua de onde parou (campo "proxima_pagina" no estado), buscando
     paginas da ANS em paralelo, ate o orcamento de tempo desta execucao
     acabar (TEMPO_MAX_S) ou a tabela terminar.
  3. Publica o progresso de novo no mesmo Release (sobrescreve os assets) --
     seja parcial (para a proxima execucao agendada continuar) ou completo
     (para quem for consumir o dado final).
  4. So quando a tabela fica 100% completa, calcula novos/removidos
     comparando com o que existia ANTES desta rodada de sincronizacao
     (usando uma tabela auxiliar `_antes_desta_sincronizacao` no banco).

Uso (dentro do workflow, ver .github/workflows/sincronizar.yml):
    python sync_ci.py --tabela tuss-19 --descricao "Materiais/OPME" --tempo-max-s 19200
    python sync_ci.py --tabela tuss-19 --filtro odonto
    python sync_ci.py --tabela tuss-19 --recomecar   (ignora o progresso salvo e comeca do zero)

Mesmo contrato de API descoberto e documentado no projeto tuss-sync-v1.1
(nao adivinhado): GET /rest/oclservice/ANS/source e
GET /rest/oclservice/ANS/concepts/{table}?page=N&q=filtro (header "pages").
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ANS_BASE = "https://consulta-ocl.apps.sa-1a.mendixcloud.com/rest/oclservice/ANS"
TIMEOUT_ANS_S = 40
TENTATIVAS_POR_PAGINA = 3
PAUSA_ENTRE_TENTATIVAS_S = 2
CONCORRENCIA = 16  # medido na pratica: acima de ~20 a ANS satura sem ganho (ver README)
PAGINAS_POR_LOTE_DE_ESCRITA = 20  # commita no sqlite a cada N paginas concluidas

RAIZ_REPO = Path(__file__).resolve().parent
DADOS_DIR = RAIZ_REPO / "dados"


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def normalizar(s: str | None) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


class ErroANS(Exception):
    pass


def buscar_ans(caminho: str) -> tuple[int, dict, bytes]:
    url = f"{ANS_BASE}{caminho}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_ANS_S) as resp:
            corpo = resp.read()
            return resp.status, {"pages": resp.headers.get("pages")}, corpo
    except urllib.error.HTTPError as e:
        return e.code, {}, e.read()
    except Exception as e:
        raise ErroANS(str(e)) from e


def buscar_pagina_concepts(tabela: str, pagina: int, filtro: str | None) -> tuple[list, int | None, str]:
    qs = {"page": str(pagina)}
    if filtro:
        qs["q"] = filtro
    caminho = f"/concepts/{urllib.parse.quote(tabela)}?{urllib.parse.urlencode(qs)}"
    _status, headers, corpo = buscar_ans(caminho)
    itens = json.loads(corpo) if corpo else []
    total_paginas = None
    if headers.get("pages"):
        try:
            total_paginas = int(headers["pages"])
        except ValueError:
            total_paginas = None
    return itens, total_paginas, f"{ANS_BASE}{caminho}"


def buscar_pagina_com_retry(tabela: str, pagina: int, filtro: str | None):
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS_POR_PAGINA + 1):
        try:
            return buscar_pagina_concepts(tabela, pagina, filtro)
        except ErroANS as e:
            ultimo_erro = e
            if tentativa < TENTATIVAS_POR_PAGINA:
                time.sleep(PAUSA_ENTRE_TENTATIVAS_S)
    raise ultimo_erro


# ---------------- banco local (um arquivo por tabela) ----------------

def caminho_db(tabela: str) -> Path:
    return DADOS_DIR / f"{tabela}.db"


def caminho_estado(tabela: str) -> Path:
    return DADOS_DIR / f"{tabela}.json"


def conectar(tabela: str) -> sqlite3.Connection:
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(caminho_db(tabela)))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS registros (
            codigo TEXT PRIMARY KEY,
            descricao TEXT,
            descricao_norm TEXT,
            inicio_vigencia TEXT,
            fim_vigencia TEXT,
            extras TEXT,
            origem TEXT,
            lote TEXT,
            atualizado_em TEXT
        )
    """)
    return con


def upsert(con: sqlite3.Connection, item: dict, origem: str, lote: str, agora: str) -> None:
    codigo = str(item["id"])
    descricao = item.get("display_name") or ""
    extras = item.get("extras") or {}
    con.execute("""
        INSERT INTO registros (codigo, descricao, descricao_norm, inicio_vigencia, fim_vigencia, extras, origem, lote, atualizado_em)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(codigo) DO UPDATE SET
            descricao=excluded.descricao, descricao_norm=excluded.descricao_norm,
            inicio_vigencia=excluded.inicio_vigencia, fim_vigencia=excluded.fim_vigencia,
            extras=excluded.extras, origem=excluded.origem, lote=excluded.lote, atualizado_em=excluded.atualizado_em
    """, (codigo, descricao, normalizar(descricao), extras.get("inicio_vigencia"), extras.get("fim_vigencia"),
          json.dumps(extras, ensure_ascii=False), origem, lote, agora))


# ---------------- publicar/baixar do GitHub Release ----------------

def gh(*args: str, ok_falhar: bool = False) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0 and not ok_falhar:
        raise RuntimeError(f"gh {' '.join(args)} falhou: {r.stderr.strip()}")
    return r.stdout.strip()


def baixar_progresso_anterior(tabela: str) -> None:
    tag = f"dados-{tabela}"
    destino = DADOS_DIR
    destino.mkdir(parents=True, exist_ok=True)
    saida = gh("release", "download", tag, "--dir", str(destino), "--clobber", ok_falhar=True)
    gz = caminho_db(tabela).with_suffix(".db.gz")
    if gz.is_file():
        with gzip.open(gz, "rb") as f_in, open(caminho_db(tabela), "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        gz.unlink()
        log(f"progresso anterior de {tabela} restaurado do release '{tag}'.")
    else:
        log(f"nenhum progresso anterior encontrado para '{tag}' (comecando do zero).")


def publicar_progresso(tabela: str, estado: dict, csv_novos: Path | None = None, csv_removidos: Path | None = None) -> None:
    tag = f"dados-{tabela}"
    caminho_estado(tabela).write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    gz = caminho_db(tabela).with_suffix(".db.gz")
    with open(caminho_db(tabela), "rb") as f_in, gzip.open(gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    existe = gh("release", "view", tag, ok_falhar=True)
    titulo = f"Dados TUSS {tabela} — {'completo' if estado.get('concluido') else 'parcial'}"
    resumo_novidades = ""
    if estado.get("concluido"):
        resumo_novidades = (
            f"- Novos nesta sincronização: {estado.get('novos', 0)}"
            f"{' (ver ' + tabela + '_novos.csv)' if csv_novos else ''}\n"
            f"- Removidos pela ANS: {estado.get('removidos', 0)}"
            f"{' (ver ' + tabela + '_removidos.csv)' if csv_removidos else ''}\n"
        )
    if estado.get("concluido"):
        status_txt = "COMPLETO"
    else:
        status_txt = f"parcial — página {estado.get('proxima_pagina', 1) - 1}/{estado.get('total_paginas', '?')}"
    notas = (
        f"Atualizado automaticamente pelo GitHub Actions.\n\n"
        f"- Tabela: {tabela} ({estado.get('descricao') or '-'})\n"
        f"- Status: {status_txt}\n"
        f"- Registros: {estado.get('total_registros', 0)}\n"
        f"{resumo_novidades}"
        f"- Última atualização: {estado.get('atualizado_em')}\n"
    )
    ativos = [f"{tabela}.db.gz", f"{tabela}.json"] + ([f"{tabela}_novos.csv"] if csv_novos else []) + \
        ([f"{tabela}_removidos.csv"] if csv_removidos else [])
    arquivos = [str(gz), str(caminho_estado(tabela))] + \
        ([str(csv_novos)] if csv_novos else []) + ([str(csv_removidos)] if csv_removidos else [])

    if existe:
        for nome in ativos:
            gh("release", "delete-asset", tag, nome, "-y", ok_falhar=True)
        gh("release", "upload", tag, *arquivos)
        gh("release", "edit", tag, "--title", titulo, "--notes", notas)
    else:
        gh("release", "create", tag, *arquivos, "--title", titulo, "--notes", notas)
    log(f"progresso publicado no release '{tag}' ({'completo' if estado.get('concluido') else 'parcial'}).")


def avisar_conclusao(tabela: str, estado: dict) -> None:
    """Abre uma Issue no repositorio -- o GitHub notifica por e-mail quem
    tem notificacoes ativadas para o repo, sem precisar de nenhuma
    integracao extra."""
    titulo = f"✅ {tabela} sincronizada por completo — {estado.get('total_registros', 0)} registros"
    corpo = (
        f"A sincronização da tabela **{tabela}** ({estado.get('descricao') or '-'}) terminou.\n\n"
        f"- Total de registros: {estado.get('total_registros', 0)}\n"
        f"- Novos nesta atualização: {estado.get('novos', 0)}\n"
        f"- Removidos pela ANS: {estado.get('removidos', 0)}\n"
        f"- Concluída em: {estado.get('atualizado_em')}\n\n"
        f"Baixe em: `gh release download dados-{tabela}` ou na aba Releases deste repositório.\n"
    )
    try:
        gh("issue", "create", "--title", titulo, "--body", corpo, ok_falhar=True)
        log("issue de aviso criada.")
    except Exception as e:
        log(f"aviso: nao consegui criar a issue de notificacao: {e}")


def gravar_historico(tabela: str, estado: dict) -> None:
    caminho = RAIZ_REPO / "historico.jsonl"
    linha = {
        "quando": time.strftime("%Y-%m-%dT%H:%M:%S"), "tabela": tabela,
        "descricao": estado.get("descricao"), "filtro": estado.get("filtro"),
        "concluido": estado.get("concluido"), "total_registros": estado.get("total_registros"),
        "novos": estado.get("novos"), "removidos": estado.get("removidos"),
        "proxima_pagina": estado.get("proxima_pagina"), "total_paginas": estado.get("total_paginas"),
        "erros_ultima_execucao": estado.get("erros_ultima_execucao"),
    }
    with caminho.open("a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")


# ---------------- sincronizacao com orcamento de tempo ----------------

def sincronizar(tabela: str, descricao: str | None, filtro: str | None, tempo_max_s: int, recomecar: bool) -> None:
    inicio_execucao = time.time()
    agora = time.strftime("%Y-%m-%dT%H:%M:%S")

    if recomecar:
        for p in (caminho_db(tabela), caminho_estado(tabela)):
            if p.is_file():
                p.unlink()
    else:
        baixar_progresso_anterior(tabela)

    estado = {}
    if caminho_estado(tabela).is_file():
        try:
            estado = json.loads(caminho_estado(tabela).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            estado = {}

    if estado.get("concluido") and not recomecar:
        log(f"'{tabela}' ja esta 100% sincronizada (registrado em {estado.get('atualizado_em')}). Nada a fazer.")
        return

    con = conectar(tabela)
    lote = estado.get("lote") or uuid.uuid4().hex[:12]
    proxima_pagina = estado.get("proxima_pagina", 1)
    total_paginas = estado.get("total_paginas")
    novos_execucao = 0
    baixados_execucao = 0
    erros_execucao = 0

    if proxima_pagina == 1 and total_paginas is None:
        # inicio de uma sincronizacao nova (ou recomecada): guarda quem
        # existia ANTES, pra no final sabermos o que a ANS removeu.
        con.execute("DROP TABLE IF EXISTS _antes_desta_sincronizacao")
        con.execute("CREATE TABLE _antes_desta_sincronizacao AS SELECT codigo FROM registros")
        con.commit()

    def orcamento_esgotado() -> bool:
        return (time.time() - inicio_execucao) > tempo_max_s

    try:
        if proxima_pagina == 1 and total_paginas is None:
            itens, total_paginas, origem = buscar_pagina_com_retry(tabela, 1, filtro)
            for item in itens:
                upsert(con, item, origem, lote, agora)
            con.commit()
            baixados_execucao += len(itens)
            proxima_pagina = 2
            log(f"pagina 1/{total_paginas} ok — {len(itens)} registros.")

        while total_paginas and proxima_pagina <= total_paginas and not orcamento_esgotado():
            fim_lote = min(total_paginas, proxima_pagina + PAGINAS_POR_LOTE_DE_ESCRITA - 1)
            paginas_lote = list(range(proxima_pagina, fim_lote + 1))
            with ThreadPoolExecutor(max_workers=CONCORRENCIA) as pool:
                futuros = {pool.submit(buscar_pagina_com_retry, tabela, p, filtro): p for p in paginas_lote}
                for fut in as_completed(futuros):
                    p = futuros[fut]
                    try:
                        itens, _tp, origem = fut.result()
                        for item in itens:
                            upsert(con, item, origem, lote, agora)
                        baixados_execucao += len(itens)
                    except ErroANS as e:
                        erros_execucao += 1
                        log(f"pagina {p} falhou apos {TENTATIVAS_POR_PAGINA} tentativas: {e}")
            con.commit()
            proxima_pagina = fim_lote + 1
            decorrido = time.time() - inicio_execucao
            log(f"progresso: pagina {min(proxima_pagina - 1, total_paginas)}/{total_paginas} "
                f"({baixados_execucao} registros nesta execucao, {decorrido:.0f}s decorridos, {erros_execucao} erro(s))")
            if orcamento_esgotado():
                log("orcamento de tempo desta execucao esgotado — salvando progresso para a proxima.")
                break

        concluido = bool(total_paginas) and proxima_pagina > total_paginas
        novos = 0
        removidos = 0
        csv_novos = csv_removidos = None
        if concluido:
            # nada de listas gigantes de codigo em Python/SQL (a primeira
            # sincronizacao de uma tabela como a OPME faria "novos" = 1,39
            # milhao de itens) -- tudo resolvido com subconsultas no SQLite,
            # que nao tem esse limite de tamanho.
            havia_dados_antes = con.execute("SELECT COUNT(*) FROM _antes_desta_sincronizacao").fetchone()[0] > 0

            # removidos = linhas que ficaram com um lote ANTIGO (a ANS nao
            # devolveu esse codigo em nenhuma pagina desta sincronizacao)
            linhas_removidas = con.execute("SELECT codigo, descricao FROM registros WHERE lote != ?", (lote,)).fetchall()
            removidos = len(linhas_removidas)

            linhas_novas = []
            if havia_dados_antes:
                linhas_novas = con.execute(
                    "SELECT codigo, descricao FROM registros WHERE lote = ? "
                    "AND codigo NOT IN (SELECT codigo FROM _antes_desta_sincronizacao)", (lote,)
                ).fetchall()
            novos = len(linhas_novas)

            if linhas_novas:
                csv_novos = DADOS_DIR / f"{tabela}_novos.csv"
                csv_novos.write_text("Codigo;Descricao\n" + "\n".join(
                    f"{r['codigo']};{(r['descricao'] or '').replace(';', ',')}" for r in linhas_novas
                ), encoding="utf-8")

            if linhas_removidas:
                csv_removidos = DADOS_DIR / f"{tabela}_removidos.csv"
                csv_removidos.write_text("Codigo;Descricao\n" + "\n".join(
                    f"{r['codigo']};{(r['descricao'] or '').replace(';', ',')}" for r in linhas_removidas
                ), encoding="utf-8")
                con.execute("DELETE FROM registros WHERE lote != ?", (lote,))

            con.execute("DROP TABLE IF EXISTS _antes_desta_sincronizacao")
            con.commit()

        total_registros = con.execute("SELECT COUNT(*) FROM registros").fetchone()[0]
        estado = {
            "tabela": tabela, "descricao": descricao or estado.get("descricao"), "filtro": filtro,
            "lote": lote, "proxima_pagina": proxima_pagina, "total_paginas": total_paginas,
            "total_registros": total_registros, "concluido": concluido,
            "novos": novos if concluido else estado.get("novos"),
            "removidos": removidos if concluido else estado.get("removidos"),
            "erros_ultima_execucao": erros_execucao,
            "iniciado_em": estado.get("iniciado_em") or agora,
            "atualizado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    finally:
        con.close()

    publicar_progresso(tabela, estado, csv_novos, csv_removidos)
    gravar_historico(tabela, estado)
    if estado["concluido"]:
        log(f"'{tabela}' concluida: {estado['total_registros']} registros "
            f"(+{estado['novos']} novos, -{estado['removidos']} removidos).")
        avisar_conclusao(tabela, estado)
    else:
        restantes = (total_paginas or 0) - proxima_pagina + 1
        log(f"'{tabela}' parcial: faltam {max(0, restantes)} paginas. "
            f"A proxima execucao agendada continua automaticamente.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tabela", required=True, help="ex.: tuss-19")
    ap.add_argument("--descricao", default=None)
    ap.add_argument("--filtro", default=None)
    ap.add_argument("--tempo-max-s", type=int, default=19200)  # 5h20 (limite do Actions e 6h)
    ap.add_argument("--recomecar", action="store_true", help="ignora o progresso salvo e comeca do zero")
    args = ap.parse_args()
    sincronizar(args.tabela, args.descricao, args.filtro, args.tempo_max_s, args.recomecar)


if __name__ == "__main__":
    main()
