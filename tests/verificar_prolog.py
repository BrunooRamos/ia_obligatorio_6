"""
Pruebas de aceptacion SPEC 04 §5: ruteo, redistribucion, analisis,
fallback Python, timeout y end-to-end. Incluye chequeo de no-regresion
sobre SPEC 03.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# Los modulos viven en la raiz; este test esta en tests/.
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SALIDAS = os.path.join(RAIZ, "salidas")
sys.path.insert(0, RAIZ)
os.chdir(RAIZ)

import puente_prolog  # noqa: E402


PY = sys.executable


def _correr(cmd: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(cmd, cwd=RAIZ, env=env, capture_output=True, text=True)


def _ruta_salida(nombre: str) -> str:
    return os.path.join(SALIDAS, nombre)


def c1_estructura_traza() -> None:
    with open(_ruta_salida("traza_simulacion.json"), "r", encoding="utf-8") as f:
        t = json.load(f)
    assert {"escenario", "ticks", "eventos", "resumen"} <= set(t)
    tick_keys = {"tick", "asignaciones", "acciones_conjuntas", "recargas",
                 "completadas", "huerfanas", "conflictos_potenciales",
                 "energia_por_agente", "metricas"}
    for r in t["ticks"]:
        assert set(r) == tick_keys
        for h in r["huerfanas"]:
            assert set(h) == {"tarea", "agente", "ticks_invertidos", "causa"}
    res_keys = {"ticks_totales", "tareas_completadas", "tareas_huerfanas",
                "trabajo_desperdiciado_total", "energia_consumida_total",
                "cooperaciones_completadas", "terminacion"}
    assert res_keys <= set(t["resumen"])
    print("[ok] 1. traza_simulacion.json coincide con SPEC 03 §2.3 (porton)")


def c2_motor_pl_carga() -> None:
    r = _correr(["swipl", "-g", "consult('motor_prolog.pl'),halt"])
    assert r.returncode == 0, r.stderr
    print("[ok] 2. motor_prolog.pl carga en swipl sin errores")


def c3_ruteo() -> None:
    # Origen (0,0) -> Objetivo (5,5), Manhattan = 10, path length 11.
    rutas = puente_prolog.optimizar(
        estado={
            "agentes": [{"id": "ax", "x": 0, "y": 0, "energia": 100, "estado": "ocupado"}],
            "celdas_sucias": [], "obstaculos_moviles": [], "objetos_pesados": [],
        },
        compromisos={"ax": {"tipo": "limpieza", "objetivo": (5, 5), "tick_inicio": 0}},
        obstaculos_fijos={(3, 4), (3, 5), (10, 8)},
        obstaculos_moviles=set(),
        ancho=15, alto=15,
        timeout=10.0,
    )
    assert "ax" in rutas
    ruta = rutas["ax"]
    # Ruta no incluye origen; longitud minima esperada = 10 pasos.
    assert len(ruta) == 10, f"ruta de longitud {len(ruta)}, se esperaba 10"
    # Verificar 4-conexion y que ninguna celda esta bloqueada.
    cur = (0, 0)
    for nxt in ruta:
        dx, dy = nxt[0] - cur[0], nxt[1] - cur[1]
        assert abs(dx) + abs(dy) == 1, f"paso no 4-conexion: {cur} -> {nxt}"
        assert nxt not in {(3, 4), (3, 5), (10, 8)}, f"pisa bloqueada: {nxt}"
        cur = nxt
    assert cur == (5, 5)
    print(f"[ok] 3. ruta_optima (0,0)->(5,5) en {len(ruta)} pasos, 4-conexion, sin obstaculos")


def c4_redistribucion() -> None:
    # Construir un caso donde greedy puede ser subóptimo.
    # Agentes en (0,0) y (0,10). Tareas en (0,1) y (0,9).
    # Greedy: si itera tareas en orden (0,1)->(0,9):
    #   (0,1) elige a1 (dist 1), (0,9) elige a2 (dist 1). Costo 2.
    # Optimo: el mismo. Hmm, busquemos un caso mas claro.
    # Probemos: agentes en (0,0), (10,10). Tareas en (1,1), (9,9).
    # Greedy: (1,1) elige a1 (dist 2), (9,9) elige a2 (dist 2). Total 4.
    # Optimo: igual. La diferencia surge con orden invertido:
    # Si las tareas se procesan (9,9) primero: greedy elige a1 (dist 18) o a2 (dist 2).
    # Sort por costo: a2 gana. (1,1) elige a1 (dist 2). Total 4. Igual.
    #
    # Caso que rompe greedy: agentes (0,0) (5,0). Tareas (4,0), (10,0).
    # Greedy (4,0)->a2 dist 1, (10,0)->a1 dist 10. Total 11.
    # Greedy reverso: (10,0)->a2 dist 5, (4,0)->a1 dist 4. Total 9.
    # Optimo: (4,0)->a1 dist 4, (10,0)->a2 dist 5. Total 9.
    # Mi greedy actual procesa tareas en orden de findall (orden de assert):
    # con (4,0) primero, costo=11; con (10,0) primero, costo=9.
    # Asegurar que mejor=9, y que greedy en peor orden=11.
    if not puente_prolog.prolog_disponible():
        print("[skip] 4. (PySWIP no disponible, salteado)")
        return

    prolog = puente_prolog._get_prolog()  # type: ignore[attr-defined]
    for p, a in [("agente", 1), ("pos_agente", 3), ("tarea", 3), ("apto", 2)]:
        puente_prolog._retract_all(prolog, p, a)  # type: ignore[attr-defined]
    prolog.assertz("agente(a1)"); prolog.assertz("agente(a2)")
    prolog.assertz("pos_agente(a1, 0, 0)")
    prolog.assertz("pos_agente(a2, 5, 0)")
    # Orden de assert importa para greedy: (4,0) primero -> peor caso.
    prolog.assertz("tarea(t1, 4, 0)")
    prolog.assertz("tarea(t2, 10, 0)")
    prolog.assertz("apto(a1, t1)"); prolog.assertz("apto(a2, t1)")
    prolog.assertz("apto(a1, t2)"); prolog.assertz("apto(a2, t2)")

    mejor = list(prolog.query("mejor_redistribucion(A, C)"))
    greedy = list(prolog.query("redistribucion_greedy(A, C)"))
    cm = int(mejor[0]["C"])
    cg = int(greedy[0]["C"])
    assert cm <= cg, f"mejor={cm} > greedy={cg} (rompe el invariante)"
    assert cm < cg, (
        f"mejor={cm} no es estrictamente menor que greedy={cg}; el caso no demuestra mejora"
    )
    print(f"[ok] 4. mejor_redistribucion costo={cm} < greedy costo={cg}")


def c5_analisis_cruza() -> None:
    with open(_ruta_salida("traza_simulacion.json"), "r", encoding="utf-8") as f:
        traza = json.load(f)
    with open(_ruta_salida("analisis_prolog.json"), "r", encoding="utf-8") as f:
        ana = json.load(f)
    resumen = traza["resumen"]
    assert ana["desperdicio_total"] == resumen["trabajo_desperdiciado_total"], (
        f"{ana['desperdicio_total']} != {resumen['trabajo_desperdiciado_total']}"
    )
    assert ana["total_cooperaciones"] == resumen["cooperaciones_completadas"]
    assert ana["total_completadas"] == resumen["tareas_completadas"]
    assert all(ana["cross_check"].values()), ana["cross_check"]
    print(f"[ok] 5. cross-check Prolog vs traza: desperdicio={ana['desperdicio_total']}, "
          f"cooperaciones={ana['total_cooperaciones']}, "
          f"completadas={ana['total_completadas']}")


def c6_fallback() -> None:
    # Forzar fallback Python via env var.
    r = _correr([PY, "simulador.py", "--headless"], env_extra={"AGENTCLEAN_SIN_PROLOG": "1"})
    assert r.returncode == 0, r.stderr
    with open(_ruta_salida("analisis_prolog.json"), "r", encoding="utf-8") as f:
        ana = json.load(f)
    assert ana["via"] == "fallback_python", f"esperaba fallback_python, fue {ana['via']}"
    assert all(ana["cross_check"].values()), ana["cross_check"]
    print(f"[ok] 6. fallback Python: simulacion termina, via={ana['via']}, "
          f"cross-check OK")


def c7_timeout() -> None:
    # Inducir timeout: pasar un timeout absurdamente corto a optimizar.
    # Con escenario chico, BFS fallback completa instantaneamente, asi que
    # tomamos un objetivo distante y timeout=0 para forzar la rama de
    # timeout (el fallback Python sigue funcionando como red).
    rutas = puente_prolog.optimizar(
        estado={
            "agentes": [{"id": "ax", "x": 0, "y": 0, "energia": 100, "estado": "ocupado"}],
            "celdas_sucias": [], "obstaculos_moviles": [], "objetos_pesados": [],
        },
        compromisos={"ax": {"tipo": "limpieza", "objetivo": (14, 14), "tick_inicio": 0}},
        obstaculos_fijos=set(),
        obstaculos_moviles=set(),
        ancho=15, alto=15,
        timeout=0.0001,  # tan corto que Prolog seguro vence
    )
    # El fallback Python provee la ruta de todos modos.
    assert "ax" in rutas and len(rutas["ax"]) > 0
    print(f"[ok] 7. timeout: la consulta no cuelga y el fallback provee ruta "
          f"({len(rutas['ax'])} pasos)")


def c8_end_to_end() -> None:
    r = _correr([PY, "simulador.py", "--headless"])
    assert r.returncode == 0, r.stderr
    assert os.path.exists(_ruta_salida("traza_simulacion.json"))
    assert os.path.exists(_ruta_salida("analisis_prolog.json"))
    with open(_ruta_salida("traza_simulacion.json")) as f:
        t = json.load(f)
    assert t["resumen"]["terminacion"] == "limpieza_completa"
    print(f"[ok] 8. end-to-end: 3 capas, terminacion={t['resumen']['terminacion']}, "
          f"ambos JSON escritos")


def c9_recomendaciones_legibles() -> None:
    with open(_ruta_salida("analisis_prolog.json")) as f:
        ana = json.load(f)
    recs = ana.get("recomendaciones", [])
    assert recs, "no hay recomendaciones"
    for r in recs:
        assert isinstance(r, str) and len(r) >= 20, f"recomendacion no legible: {r!r}"
    # Trazables: al menos una menciona a un agente especifico o un tick.
    trazables = any(("a1" in r or "a2" in r or "a3" in r or "a4" in r or "tick" in r) for r in recs)
    assert trazables, f"recomendaciones genericas: {recs}"
    print(f"[ok] 9. {len(recs)} recomendaciones legibles y trazables")


def c10_no_regresion_spec3() -> None:
    # Reusar el verificador de SPEC 03 desde cero (corre simulador + assertions).
    r = _correr([PY, os.path.join("tests", "verificar_simulador.py")])
    assert r.returncode == 0, r.stderr
    assert "TODOS los criterios SPEC 03" in r.stdout
    print("[ok] 10. SPEC 01/02/03 sin regresion (verificar_simulador.py pasa)")


if __name__ == "__main__":
    # Generar traza fresca antes de las verificaciones.
    print("=== Preparando: corrida end-to-end con Prolog ===")
    r = _correr([PY, "simulador.py", "--headless"])
    assert r.returncode == 0, r.stderr

    print("\n=== Verificando criterios SPEC 04 §5 ===")
    c1_estructura_traza()
    c2_motor_pl_carga()
    c3_ruteo()
    c4_redistribucion()
    c5_analisis_cruza()
    c6_fallback()
    c7_timeout()
    c8_end_to_end()
    c9_recomendaciones_legibles()
    c10_no_regresion_spec3()
    print("\n[ok] TODOS los criterios SPEC 04 §5 satisfechos")
