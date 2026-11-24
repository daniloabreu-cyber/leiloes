#!/usr/bin/env python3
"""
Coletor dos leilões da Receita Federal (portal SLE).

Lê a lista de editais, os lotes de cada edital e a descrição de cada lote,
e grava tudo em data.json (que a página index.html usa para a busca).

Só baixa de novo a descrição de um lote quando ele é novo ou quando mudou o
número de avisos/erratas. Se aparecer lote novo de PS5, escreve novos_alertas.md
(o GitHub usa esse arquivo para te avisar por e-mail).
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = os.environ.get("SLE_BASE", "https://www25.receita.fazenda.gov.br/sle-sociedade/api")
PAUSA = float(os.environ.get("SLE_PAUSA", "0.4"))  # segundos entre chamadas
PASTA = Path(__file__).resolve().parent
DADOS = PASTA / "data.json"
ALERTA = PASTA / "novos_alertas.md"

# O que dispara aviso por e-mail. Para avisar também de Xbox, acrescente: re.compile(r"x\s*box")
ALERTAS = [("PlayStation 5", re.compile(r"play\s*station\s*5|\bps\s*5\b|\bps5\b"))]

sessao = requests.Session()
sessao.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "application/json",
})


def normalizar(texto):
    texto = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in texto if unicodedata.category(c) != "Mn").lower()


def get_json(caminho, tentativas=3):
    """Devolve (dados, status). dados é None se não deu certo."""
    status = None
    for i in range(tentativas):
        try:
            r = sessao.get(BASE + caminho, timeout=60)
            status = r.status_code
            if r.status_code == 200:
                return r.json(), 200
            if r.status_code in (403, 404):
                break
        except (requests.RequestException, ValueError) as e:
            status = type(e).__name__
        time.sleep(3 * (i + 1))
    return None, status


def carregar_anterior():
    if DADOS.exists():
        try:
            return json.loads(DADOS.read_text("utf-8"))
        except ValueError:
            pass
    return {"lotes": [], "alertados": []}


def main():
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    anterior = carregar_anterior()
    antigos = {f"{l['edle']}/{l['lote']}": l for l in anterior.get("lotes", [])}
    alertados = set(anterior.get("alertados", []))

    portal, status = get_json("/portal")
    if not portal:
        sys.exit(f"ERRO: não consegui abrir a lista de editais da Receita (resposta: {status}). "
                 "Se for 403, o site da Receita está bloqueando o acesso a partir daqui.")

    editais = []
    for s in portal.get("situacoes", []):
        for e in s.get("lista", []):
            editais.append({**e, "situacao": s.get("label", "")})
    print(f"{len(editais)} editais encontrados.")

    lotes, falhas, baixados = [], 0, 0
    for e in editais:
        edle = e["edle"]
        dados, status = get_json(f"/edital/{edle}")
        time.sleep(PAUSA)
        if dados is None:
            falhas += 1
            print(f"  AVISO: edital {e['edital']} falhou ({status}); mantendo dados antigos.")
            lotes += [l for l in antigos.values() if l["edle"] == edle]
            continue
        lista = dados.get("listaLotes") or []
        print(f"  Edital {e['edital']} ({e['cidade']}): {len(lista)} lotes")
        for l in lista:
            nr = l["nrAtribuido"]
            chave = f"{edle}/{nr}"
            velho = antigos.get(chave)
            precisa = (
                velho is None
                or velho.get("semDetalhe")
                or velho.get("nrAvisos") != l.get("nrAvisos")
                or velho.get("nrErratas") != l.get("nrErratas")
            )
            itens = velho["itens"] if velho else []
            avisos = velho["avisos"] if velho else []
            sem_detalhe = bool(velho and velho.get("semDetalhe"))
            if precisa:
                det, st = get_json(f"/lote/{edle}/{nr}")
                time.sleep(PAUSA)
                baixados += 1
                if det is not None:
                    itens = [
                        " ".join(str(p) for p in (i.get("quantidade"), i.get("unMedida"), i.get("descricao")) if p not in (None, ""))
                        for i in det.get("itensDetalhesLote") or []
                    ]
                    avisos = [a.get("texto", "") for a in det.get("avisosErratas") or []]
                    sem_detalhe = False
                else:
                    sem_detalhe = True
            lotes.append({
                "edital": e["edital"], "edle": edle, "cidade": e["cidade"],
                "situacao": e["situacao"], "fim": e.get("dataFimPropostas", ""),
                "lances": e.get("dataAberturaLances", ""), "lote": nr,
                "tipo": l.get("tipo", ""), "minimo": l.get("valorMinimo"),
                "avaliacao": l.get("valorAvaliacao"), "permitePF": l.get("permitePF"),
                "itens": itens, "avisos": avisos,
                "nrAvisos": l.get("nrAvisos"), "nrErratas": l.get("nrErratas"),
                "semDetalhe": sem_detalhe,
                "visto": velho["visto"] if velho else agora,
            })

    if editais and falhas == len(editais):
        sys.exit("ERRO: todos os editais falharam; não vou sobrescrever os dados antigos.")

    # ---- alertas de lotes novos ----
    novos = []
    for l in lotes:
        chave = f"{l['edle']}/{l['lote']}"
        if chave in alertados:
            continue
        texto = normalizar(" ".join([l["tipo"], *l["itens"]]))
        for nome, padrao in ALERTAS:
            if padrao.search(texto):
                novos.append((nome, chave, l))
                break

    if ALERTA.exists():
        ALERTA.unlink()
    if novos:
        linhas = []
        for nome, _, l in novos:
            minimo = f"R$ {l['minimo']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if l["minimo"] is not None else "?"
            linhas.append(f"### {nome} — Lote {l['lote']} do edital {l['edital']} ({l['cidade']})")
            linhas.append(f"- Valor mínimo: {minimo} · propostas até {l['fim']} · lances {l['lances']}")
            linhas += [f"- {i}" for i in l["itens"]]
            linhas += [f"- AVISO: {a}" for a in l["avisos"]]
            linhas.append("")
        ALERTA.write_text("\n".join(linhas), "utf-8")
        print(f"{len(novos)} lote(s) novo(s) para avisar.")

    chaves_atuais = {f"{l['edle']}/{l['lote']}" for l in lotes}
    novos_alertados = [k for k in alertados if k in chaves_atuais] + [k for _, k, _ in novos]

    saida = {"atualizado": agora, "lotes": lotes, "alertados": novos_alertados}
    tmp = DADOS.with_suffix(".tmp")
    tmp.write_text(json.dumps(saida, ensure_ascii=False, separators=(",", ":")), "utf-8")
    tmp.replace(DADOS)
    print(f"Pronto: {len(lotes)} lotes ({baixados} descrições baixadas, {falhas} editais com falha).")


if __name__ == "__main__":
    main()
