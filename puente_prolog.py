"""
AgentClean - Puente Python <-> Prolog (SPEC 04).

Encapsula la integracion PySWIP + SWI-Prolog del motor offline:
  - optimizar(...) : modo periodico (rutas optimas para cada compromiso).
  - analizar(...)  : modo fin de simulacion (F3 sobre la traza completa).

Patron de robustez (continuidad de SPEC 03):
  - PySWIP es opcional. Si falta o falla, se usa el fallback Python con la
    misma firma de retorno y la simulacion no se interrumpe.
  - Cada invocacion PySWIP corre en un hilo daemon con timeout. Si supera
    el tiempo, se cancela y se cae al fallback.
  - `findall(...)` + `list(prolog.query(...))` para materializar de una vez
    (nunca iterar el generador lazy: cuelga en casos densos).
"""

from __future__ import annotations

import collections
import json
import os
import threading
from typing import Any

try:
    from pyswip import Prolog as _PrologEngine  # type: ignore
    _PYSWIP_OK = True
except Exception:  # pragma: no cover
    _PrologEngine = None  # type: ignore
    _PYSWIP_OK = False


_FORZAR_FALLBACK = os.environ.get("AGENTCLEAN_SIN_PROLOG") == "1"

_MOTOR_PL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "motor_prolog.pl")

_prolog_instance = None


def prolog_disponible() -> bool:
    """True si PySWIP esta instalado y motor_prolog.pl consulta sin error."""
    if _FORZAR_FALLBACK or not _PYSWIP_OK:
        return False
    try:
        _get_prolog()
        return True
    except Exception:
        return False


def _get_prolog():
    global _prolog_instance
    if _prolog_instance is None:
        _prolog_instance = _PrologEngine()
        _prolog_instance.consult(_MOTOR_PL)
    return _prolog_instance


def _run_con_timeout(func, timeout: float):
    """Ejecuta func() en un hilo daemon; devuelve (resultado, ok)."""
    box: dict = {"res": None, "err": None}

    def _target():
        try:
            box["res"] = func()
        except Exception as e:  # pragma: no cover
            box["err"] = e

    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return None, False  # timeout
    if box["err"] is not None:
        return None, False
    return box["res"], True


def _retract_all(prolog, predicado: str, aridad: int) -> None:
    args = ", ".join(["_"] * aridad)
    try:
        prolog.retractall(f"{predicado}({args})")
    except Exception:
        pass


# =====================================================================
# OPTIMIZAR - modo periodico
# =====================================================================

def optimizar(
    *,
    estado: dict,
    compromisos: dict,
    obstaculos_fijos: set[tuple[int, int]],
    obstaculos_moviles: set[tuple[int, int]],
    ancho: int,
    alto: int,
    timeout: float = 10.0,
) -> dict[str, list[tuple[int, int]]]:
    """Computa rutas optimas para los compromisos vigentes.

    Devuelve {agente_id: [(x, y), ...]} con la secuencia de pasos sugerida
    desde la posicion actual hacia el objetivo. Si PySWIP no esta disponible
    o el timeout se vence, usa BFS en Python (misma firma de retorno).
    """
    objetivos = _objetivos_desde_compromisos(estado, compromisos)
    if not objetivos:
        return {}

    bloqueadas = set(obstaculos_fijos) | set(obstaculos_moviles)

    if prolog_disponible():
        def _job():
            prolog = _get_prolog()
            _retract_all(prolog, "dim_grilla", 2)
            _retract_all(prolog, "bloqueada", 2)
            _retract_all(prolog, "origen", 3)
            _retract_all(prolog, "objetivo", 3)

            prolog.assertz(f"dim_grilla({ancho}, {alto})")
            for (x, y) in bloqueadas:
                prolog.assertz(f"bloqueada({x}, {y})")

            rutas: dict[str, list[tuple[int, int]]] = {}
            for aid, (ox, oy, tx, ty) in objetivos.items():
                prolog.assertz(f"origen({aid}, {ox}, {oy})")
                prolog.assertz(f"objetivo({aid}, {tx}, {ty})")
                consulta = f"ruta_optima({aid}, C)"
                resultados = list(prolog.query(consulta))
                if not resultados:
                    continue
                camino = resultados[0]["C"]
                rutas[aid] = _camino_pyswip_a_tuplas(camino)[1:]  # sin origen
            return rutas

        resultado, ok = _run_con_timeout(_job, timeout)
        if ok and resultado is not None:
            # Para los agentes no resueltos por Prolog, completar con BFS.
            faltantes = {a: v for a, v in objetivos.items() if a not in resultado}
            if faltantes:
                resultado.update(_bfs_rutas(faltantes, bloqueadas, ancho, alto))
            return resultado

    # Fallback Python.
    return _bfs_rutas(objetivos, bloqueadas, ancho, alto)


def _objetivos_desde_compromisos(
    estado: dict, compromisos: dict,
) -> dict[str, tuple[int, int, int, int]]:
    """Para cada agente comprometido, devuelve (ox, oy, tx, ty) o nada."""
    agentes_pos = {ag["id"]: (ag["x"], ag["y"]) for ag in estado["agentes"]}
    objetos_pesados = {o["id"]: (o["x"], o["y"]) for o in estado["objetos_pesados"]}

    objetivos: dict[str, tuple[int, int, int, int]] = {}
    for aid, com in compromisos.items():
        if aid not in agentes_pos:
            continue
        ox, oy = agentes_pos[aid]
        target = None
        tipo = com.get("tipo")
        if tipo in ("limpieza", "recarga"):
            target = com.get("objetivo")
        elif tipo == "transporte":
            target = objetos_pesados.get(com.get("obj"))
        if target is None or (ox, oy) == tuple(target):
            continue
        tx, ty = target
        objetivos[aid] = (ox, oy, int(tx), int(ty))
    return objetivos


def _camino_pyswip_a_tuplas(camino: Any) -> list[tuple[int, int]]:
    """Convierte la respuesta de ruta_optima/2 (lista de [X,Y]) a tuplas."""
    out: list[tuple[int, int]] = []
    for cell in camino:
        if isinstance(cell, list) and len(cell) == 2:
            out.append((int(cell[0]), int(cell[1])))
        else:
            # Otros tipos posibles (Functor c(X,Y)): no se generan en este motor.
            try:
                out.append((int(cell.args[0]), int(cell.args[1])))  # type: ignore[attr-defined]
            except Exception:
                continue
    return out


def _bfs_rutas(
    objetivos: dict[str, tuple[int, int, int, int]],
    bloqueadas: set[tuple[int, int]],
    ancho: int,
    alto: int,
) -> dict[str, list[tuple[int, int]]]:
    """Fallback Python: BFS de 4-conexion para cada agente."""
    rutas: dict[str, list[tuple[int, int]]] = {}
    for aid, (ox, oy, tx, ty) in objetivos.items():
        camino = _bfs(ox, oy, tx, ty, bloqueadas, ancho, alto)
        if camino:
            rutas[aid] = camino[1:]
    return rutas


def _bfs(
    ox: int, oy: int, tx: int, ty: int,
    bloqueadas: set[tuple[int, int]], ancho: int, alto: int,
) -> list[tuple[int, int]] | None:
    if (ox, oy) == (tx, ty):
        return [(ox, oy)]
    if (tx, ty) in bloqueadas:
        return None
    visit = {(ox, oy): None}
    cola = collections.deque([(ox, oy)])
    while cola:
        x, y = cola.popleft()
        if (x, y) == (tx, ty):
            # reconstruir
            camino = [(x, y)]
            while visit[camino[-1]] is not None:
                camino.append(visit[camino[-1]])
            camino.reverse()
            return camino
        for (nx, ny) in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not (0 <= nx < ancho and 0 <= ny < alto):
                continue
            if (nx, ny) in bloqueadas or (nx, ny) in visit:
                continue
            visit[(nx, ny)] = (x, y)
            cola.append((nx, ny))
    return None


# =====================================================================
# ANALIZAR - modo fin de simulacion
# =====================================================================

def analizar(
    traza: dict,
    config: dict | None = None,
    timeout: float = 20.0,
    salida: str = "analisis_prolog.json",
) -> dict:
    """Analisis post-mortem de la traza completa (F3).

    Devuelve un dict con desperdicio total, eficiencia, picos de conflicto,
    agente mas eficiente, correlaciones averia->huerfana y recomendaciones.
    Escribe `analisis_prolog.json`. Fallback Python si PySWIP falla.
    """
    if config is None:
        cfg_candidatos = [
            "config.json",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
        ]
        for ruta in cfg_candidatos:
            try:
                with open(ruta, "r", encoding="utf-8") as f:
                    config = json.load(f)
                break
            except Exception:
                continue

    hechos = _construir_hechos_traza(traza, config)

    via = "fallback_python"
    analisis: dict[str, Any] = {}

    if prolog_disponible():
        def _job():
            return _analizar_prolog(hechos)

        resultado, ok = _run_con_timeout(_job, timeout)
        if ok and resultado is not None:
            analisis = resultado
            via = "prolog"

    if not analisis:
        analisis = _analizar_fallback(hechos)

    analisis["via"] = via
    # Cross-check obligatorio (SPEC 04 §5.5): el desperdicio total y las
    # cooperaciones deben coincidir con el resumen.
    resumen = traza.get("resumen", {})
    analisis["cross_check"] = {
        "trabajo_desperdiciado": (
            analisis.get("desperdicio_total") == resumen.get("trabajo_desperdiciado_total")
        ),
        "cooperaciones": (
            analisis.get("total_cooperaciones") == resumen.get("cooperaciones_completadas")
        ),
        "tareas_completadas": (
            analisis.get("total_completadas") == resumen.get("tareas_completadas")
        ),
    }

    with open(salida, "w", encoding="utf-8") as f:
        json.dump(analisis, f, indent=2, ensure_ascii=False)

    return analisis


def _tarea_atom(d: dict) -> str:
    tipo = d.get("tipo")
    if tipo == "limpieza":
        return f"lim_{d['x']}_{d['y']}"
    if tipo == "transporte":
        return f"obj_{d.get('obj', 'x')}"
    if tipo == "recarga":
        return f"rec_{d['x']}_{d['y']}"
    return "tarea_desconocida"


def _construir_hechos_traza(traza: dict, config: dict | None) -> dict:
    """Extrae de la traza las tuplas que se vuelcan como hechos Prolog."""
    huerfanas: list[tuple[str, str, int, str]] = []
    completadas: list[tuple[int, str]] = []
    completadas_por: list[tuple[int, str, str]] = []
    cooperaciones: list[tuple[int, str, str, str]] = []
    eventos_av: list[tuple[int, str, str]] = []
    conflictos: list[tuple[int, int]] = []

    for t in traza["ticks"]:
        tick = t["tick"]
        for h in t["huerfanas"]:
            huerfanas.append(
                (_tarea_atom(h["tarea"]), h["agente"],
                 int(h["ticks_invertidos"]), h["causa"])
            )
        for c in t["completadas"]:
            ta = _tarea_atom(c)
            completadas.append((tick, ta))
            if c.get("agente"):
                completadas_por.append((tick, ta, c["agente"]))
            for ag in c.get("agentes", []):
                completadas_por.append((tick, ta, ag))
            if c.get("tipo") == "transporte":
                ags = sorted(c.get("agentes", []))
                if len(ags) >= 2:
                    cooperaciones.append((tick, c.get("obj", "x"), ags[0], ags[1]))
        conflictos.append((tick, int(t.get("conflictos_potenciales", 0))))

    for e in traza.get("eventos", []):
        if e.get("tipo") == "averia" and e.get("agente"):
            eventos_av.append((int(e["tick"]), "averia", e["agente"]))

    # Energia consumida por agente.
    energia_consumida: dict[str, int] = {}
    if config is not None:
        ini = {a["id"]: a["energia_inicial"] for a in config.get("agentes", [])}
        ult = traza["ticks"][-1]["energia_por_agente"] if traza["ticks"] else {}
        for aid, e0 in ini.items():
            e1 = ult.get(aid, e0)
            energia_consumida[aid] = max(0, int(e0) - int(e1))
    else:
        # Sin config: inferir consumo solo entre primer y ultimo tick.
        primero = traza["ticks"][0]["energia_por_agente"] if traza["ticks"] else {}
        ultimo  = traza["ticks"][-1]["energia_por_agente"] if traza["ticks"] else {}
        for aid in primero:
            energia_consumida[aid] = max(0, int(primero[aid]) - int(ultimo.get(aid, 0)))

    agentes = sorted(energia_consumida.keys()) or sorted({
        c[2] for c in completadas_por
    } | {h[1] for h in huerfanas})

    return {
        "huerfanas":        huerfanas,
        "completadas":      completadas,
        "completadas_por":  completadas_por,
        "cooperaciones":    cooperaciones,
        "eventos_av":       eventos_av,
        "conflictos":       conflictos,
        "energia_consumida": energia_consumida,
        "agentes":          agentes,
    }


def _analizar_prolog(h: dict) -> dict:
    prolog = _get_prolog()

    # Limpiar hechos previos.
    for p, a in [
        ("tr_huerfana", 4), ("tr_energia", 2), ("tr_conflictos", 2),
        ("tr_completada", 2), ("tr_completada_por", 3),
        ("tr_evento", 3), ("tr_cooperacion", 4), ("agente", 1),
    ]:
        _retract_all(prolog, p, a)

    # Cargar hechos.
    for aid in h["agentes"]:
        prolog.assertz(f"agente({aid})")
    for (tarea, ag, ti, ca) in h["huerfanas"]:
        prolog.assertz(f"tr_huerfana({tarea}, {ag}, {ti}, {ca})")
    for (tick, tarea) in h["completadas"]:
        prolog.assertz(f"tr_completada({tick}, {tarea})")
    for (tick, tarea, ag) in h["completadas_por"]:
        prolog.assertz(f"tr_completada_por({tick}, {tarea}, {ag})")
    for (tick, obj, a1, a2) in h["cooperaciones"]:
        prolog.assertz(f"tr_cooperacion({tick}, {obj}, {a1}, {a2})")
    for (tick, tipo, ag) in h["eventos_av"]:
        prolog.assertz(f"tr_evento({tick}, {tipo}, {ag})")
    for (tick, n) in h["conflictos"]:
        prolog.assertz(f"tr_conflictos({tick}, {n})")
    for ag, e in h["energia_consumida"].items():
        prolog.assertz(f"tr_energia({ag}, {e})")

    def _solo(c: str, var: str):
        res = list(prolog.query(c))
        return res[0][var] if res else None

    desperdicio = _solo("desperdicio_total(D)", "D")
    eficiencia  = _solo("eficiencia_global(R)", "R")
    completadas = _solo("total_completadas(N)", "N")
    huerfanas   = _solo("total_huerfanas(N)", "N")
    cooperac    = _solo("total_cooperaciones(N)", "N")

    res_pico = list(prolog.query("pico_conflictos(T, N)"))
    pico = {"tick": res_pico[0]["T"], "n": res_pico[0]["N"]} if res_pico else None

    res_eff = list(prolog.query("agente_mas_eficiente(Ag, R)"))
    mas_eficiente = (
        {"agente": str(res_eff[0]["Ag"]), "ratio": float(res_eff[0]["R"])}
        if res_eff else None
    )

    correlaciones = [
        {"agente": str(r["Ag"]), "tarea": str(r["T"]), "ticks": int(r["Ti"])}
        for r in prolog.query("averia_causo_desperdicio(Ag, T, Ti)")
    ]

    recomendaciones = [str(r["R"]) for r in prolog.query("recomendacion(R)")]

    return {
        "desperdicio_total":    int(desperdicio) if desperdicio is not None else 0,
        "eficiencia_global":    float(eficiencia) if eficiencia is not None else 0.0,
        "total_completadas":    int(completadas) if completadas is not None else 0,
        "total_huerfanas":      int(huerfanas) if huerfanas is not None else 0,
        "total_cooperaciones":  int(cooperac) if cooperac is not None else 0,
        "pico_conflictos":      pico,
        "agente_mas_eficiente": mas_eficiente,
        "averia_correlaciones": correlaciones,
        "recomendaciones":      recomendaciones,
        "energia_por_agente":   dict(h["energia_consumida"]),
    }


# ---------------------------------------------------------------------
# Fallback Python equivalente a F3
# ---------------------------------------------------------------------

def _analizar_fallback(h: dict) -> dict:
    desperdicio = sum(ti for (_, _, ti, _) in h["huerfanas"])
    nc = len(h["completadas"])
    nh = len(h["huerfanas"])
    nco = len(h["cooperaciones"])
    total = nc + nh
    eficiencia = (nc / total) if total > 0 else 1.0

    conflictos_por_tick = sorted(h["conflictos"], key=lambda p: -p[1])
    pico = (
        {"tick": conflictos_por_tick[0][0], "n": conflictos_por_tick[0][1]}
        if conflictos_por_tick else None
    )

    tareas_por_agente: dict[str, int] = collections.Counter(
        ag for (_, _, ag) in h["completadas_por"]
    )
    mas_eficiente = None
    mejor_ratio = -1.0
    for ag in h["agentes"]:
        e = h["energia_consumida"].get(ag, 0)
        if e <= 0:
            continue
        r = tareas_por_agente.get(ag, 0) / e
        if r > mejor_ratio:
            mejor_ratio = r
            mas_eficiente = {"agente": ag, "ratio": r}

    aver_agentes = {ag for (_, _, ag) in h["eventos_av"]}
    correlaciones = [
        {"agente": ag, "tarea": tarea, "ticks": ti}
        for (tarea, ag, ti, ca) in h["huerfanas"]
        if ca == "averia" and ag in aver_agentes
    ]

    recomendaciones: list[str] = []
    sin_energia_ags = sorted({
        ag for (_, ag, _, ca) in h["huerfanas"] if ca == "sin_energia"
    })
    if sin_energia_ags:
        recomendaciones.append(
            f"el umbral de recarga de {sin_energia_ags[0]} parece insuficiente "
            f"(genero huerfana por agotamiento)"
        )
    for c in correlaciones:
        if c["ticks"] > 5:
            recomendaciones.append(
                f"la averia de {c['agente']} en plena tarea desperdicio "
                f"{c['ticks']} ticks; conviene redundancia o roles cooperativos"
            )
            break
    if pico and pico["n"] >= 3:
        recomendaciones.append(
            f"pico de {pico['n']} conflictos potenciales en tick {pico['tick']} "
            f"sugiere demasiados agentes apuntando a la misma zona"
        )
    if not recomendaciones:
        recomendaciones.append(
            "sin recomendaciones criticas: el sistema operó dentro de parametros esperados"
        )

    return {
        "desperdicio_total":    desperdicio,
        "eficiencia_global":    eficiencia,
        "total_completadas":    nc,
        "total_huerfanas":      nh,
        "total_cooperaciones":  nco,
        "pico_conflictos":      pico,
        "agente_mas_eficiente": mas_eficiente,
        "averia_correlaciones": correlaciones,
        "recomendaciones":      recomendaciones,
        "energia_por_agente":   dict(h["energia_consumida"]),
    }
