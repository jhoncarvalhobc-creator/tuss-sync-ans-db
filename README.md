# tuss-sync-ans-db

Baixa tabelas TUSS da ANS (Agência Nacional de Saúde Suplementar) rodando
**dentro do GitHub Actions** — 24 horas, sem depender de nenhum computador
ligado. É o "motor" de dados usado pelo painel local
[tuss-sync-v1.1](https://github.com/jhoncarvalhobc-creator/tuss-sync-v1.1)
(ou qualquer outro sistema que queira consumir esses dados).

## Por que existe

Algumas tabelas TUSS são enormes — a TUSS 19 (Materiais/OPME) tem
**1.389.786 registros** em **~60.586 páginas**. Baixar isso página por
página, mesmo em paralelo, leva muitas horas. Este repositório roda essa
sincronização em segundo plano, de forma **retomável**: o GitHub Actions
tem limite de 6 horas por execução, então o progresso é salvo e uma
execução agendada (a cada 5 horas) continua de onde parou, até terminar.

## Como usar

### Pedir para sincronizar uma tabela
Aba **Actions** → workflow "Sincronizar TUSS com a ANS" → **Run workflow**
→ preencha:
- `tabela`: código da tabela na ANS (ex.: `tuss-19`)
- `descricao`: opcional, só para identificar no título
- `filtro`: opcional — restringe a busca (ex.: `stent`), muito mais rápido
- `recomecar`: `true` para forçar um novo download do zero (útil para
  **atualizar** uma tabela que já tinha terminado antes — a ANS não tem
  um jeito de dizer "só o que mudou", então atualizar significa baixar
  tudo de novo e comparar)

A tabela entra na lista de "tabelas ativas" e é retomada automaticamente
a cada 5 horas até terminar. Quando termina 100%, é **removida da lista**
(não fica gastando tempo de execução à toa) e:
- uma **Issue é aberta neste repositório** avisando (o GitHub notifica
  automaticamente quem observa o repo);
- o **Release** `dados-<tabela>` é atualizado com o banco final, mais um
  CSV dos registros **novos** e outro dos **removidos** desta atualização
  (se houve uma sincronização anterior para comparar).

### Baixar os dados prontos
Aba **Releases** → `dados-<tabela>` → baixe `<tabela>.db.gz` (descomprima
com gzip) ou use:
```bash
gh release download dados-tuss-19 --repo jhoncarvalhobc-creator/tuss-sync-ans-db
```

### Saber se já terminou / o que mudou
- **Issues** deste repositório: uma nova issue aparece quando uma tabela
  termina, com o resumo (registros, novos, removidos).
- **`historico.jsonl`** (na raiz do repositório, versionado no git): uma
  linha por execução, com timestamp, tabela, status, contagens — é o log
  de auditoria completo, visível no histórico do próprio git.
- **Release** `dados-<tabela>`: o título diz "completo" ou "parcial —
  página X/Y", e as notas trazem o resumo mais recente.

## O que este repositório NÃO tem
- Nenhum dado sensível/privado — tudo aqui é informação pública da ANS
  (códigos de materiais, medicamentos, procedimentos, etc.).
- Nenhuma tabela SQLite fica versionada no histórico do git (ver
  `.gitignore`) — os bancos ficam só nos assets dos Releases, para não
  inchar o repositório.

## Limite de velocidade (medido, não chutado)
Testado na prática: acima de ~20 conexões simultâneas contra a API da
ANS, o servidor deles satura (mais conexões não baixam mais rápido, só
aumentam erro/timeout). Por isso `sync_ci.py` usa 16 conexões em paralelo
como padrão — é uma escolha baseada em medição real, não um número
arbitrário.
