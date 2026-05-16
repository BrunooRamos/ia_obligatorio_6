"""
AgentClean - Capa de coordinacion reactiva pyDatalog (SPEC 02).

Define todas las reglas derivadas (familias A-H) sobre el contrato de hechos
de SPEC 01 (modelo_datos.py) y expone tres operaciones:
  - definir_reglas():     declara vocabulario derivado + reglas.
  - planificar():         consulta las reglas, devuelve dict de decisiones.
  - calcular_metricas():  agrega en Python sobre las consultas.

Decisiones de diseno (justificacion en SPEC 02 §3):
  - Distancia Manhattan (grilla de 4 vecinos).
  - Asignacion determinista: menor distancia -> mayor energia -> menor id.
  - Recarga tiene prioridad: bajo umbral_recarga, el agente no es candidato.
  - Las tareas se derivan de hechos crudos (sucia, objeto_pesado); Python no
    asierta tareas.
  - Conflictos: la asignacion ya los evita; conflicto_potencial/4 los expone
    para metrica de presion de coordinacion.
  - Agregacion en Python, no con operadores pyDatalog (alineado con el curso).
"""

from __future__ import annotations

from pyDatalog import pyDatalog


_reglas_definidas = False


# ---------------------------------------------------------------------------
# Inicializacion de predicados opcionales
# ---------------------------------------------------------------------------
# Si una regla referencia un predicado que nunca recibio ningun hecho (porque
# el escenario tiene 0 estaciones, 0 sucias, 0 objetos, etc.), pyDatalog lanza
# "Predicate without definition" al consultar la regla derivada. Asertamos y
# retractamos un hecho phantom para "tocar" cada predicado opcional y dejarlo
# definido aunque quede sin hechos reales.
_PHANTOM = "__phantom__"

_PREDICADOS_A_INICIALIZAR = [
    ("obstaculo_fijo",   (-1, -1)),
    ("obstaculo_movil",  (-1, -1)),
    ("estacion_recarga", (-1, -1)),
    ("zona_espera",      (-1, -1)),
    ("sucia",            (-1, -1)),
    ("objeto_pesado",    (_PHANTOM, -1, -1)),
    ("posicion",         (_PHANTOM, -1, -1)),
    ("energia",          (_PHANTOM, -1)),
    ("estado_agente",    (_PHANTOM, _PHANTOM)),
    ("capacidad",        (_PHANTOM, _PHANTOM)),
    ("umbral_recarga",   (_PHANTOM, -1)),
    ("energia_maxima",   (_PHANTOM, -1)),
    ("costo_mover",      (_PHANTOM, -1)),
    ("costo_accion",     (_PHANTOM, -1)),
    ("agente",           (_PHANTOM,)),
    ("grilla",           (-1, -1)),
]


def _inicializar_predicados() -> None:
    for nombre, args in _PREDICADOS_A_INICIALIZAR:
        pyDatalog.assert_fact(nombre, *args)
        pyDatalog.retract_fact(nombre, *args)


# ---------------------------------------------------------------------------
# Definicion de reglas
# ---------------------------------------------------------------------------

def definir_reglas() -> None:
    """Declara el vocabulario derivado y define todas las reglas A-H.

    Debe llamarse UNA sola vez, despues de modelo_datos.declarar_vocabulario().
    """
    global _reglas_definidas
    if _reglas_definidas:
        return

    pyDatalog.create_terms(
        # --- Predicados derivados (A)
        "dentro_grilla, bloqueada, transitable, absdif, dist, "
        # --- (B)
        "ocupado, averiado, necesita_recarga, disponible, apto_para, "
        "estacion_candidata, existe_mejor_estacion, recarga_objetivo, "
        # --- (C)
        "tarea_limpieza, tarea_transporte, existe_tarea_pendiente, "
        # --- (D)
        "candidato_limpieza, existe_mejor_limpieza, "
        "asignacion_limpieza, agente_asignado, "
        # --- (E)
        "conflicto_potencial, tarea_disputada, "
        # --- (F)
        "apto_transporte, pareja_transporte, existe_mejor_pareja, "
        "accion_conjunta, en_accion_conjunta, "
        # --- (G)
        "replan_sin_energia, replan_averiado, agente_ocioso, "
        "a_zona_espera, requiere_replanificacion, "
        # --- (H)
        "agente_trabajando, tarea_pendiente_limpieza, "
        "cooperacion_activa, agente_recargando, "
        # --- Variables
        "A, A1, A2, B1, B2, "
        "X, Y, X1, X2, Y1, Y2, AX, AY, "
        "EX, EY, EX2, EY2, "
        "W, H, "
        "D, D1, D2, DB1, DB2, DX, DY, "
        "S, "
        "E, E2, U, "
        "Cap, Obj, Est"
    )

    _inicializar_predicados()

    # Reglas via pyDatalog.load() para evitar el problema de scoping de
    # create_terms dentro de funciones (los nombres se inyectan en globals
    # del frame caller, pero las referencias dentro de una funcion ya estan
    # resueltas en tiempo de compilacion como locals). Sintaxis equivalente
    # a la forma con operadores `<=`.
    for regla in _REGLAS:
        pyDatalog.load(regla)

    _reglas_definidas = True


# Reglas declarativas. Orden A -> B -> C -> D -> E -> F -> G -> H.
# Toda regla con `~p(..)` requiere que p este completamente definida antes.
_REGLAS = [
    # ===== A - Geometria =====
    "dentro_grilla(X, Y) <= grilla(W, H) & (X >= 0) & (Y >= 0) & (X < W) & (Y < H)",

    "bloqueada(X, Y) <= obstaculo_fijo(X, Y)",
    "bloqueada(X, Y) <= obstaculo_movil(X, Y)",

    "transitable(X, Y) <= dentro_grilla(X, Y) & ~bloqueada(X, Y)",

    # |X1-X2| via case analysis (pyDatalog no soporta abs() sobre Operation).
    "absdif(X1, X2, D) <= (X1 >= X2) & (D == X1 - X2)",
    "absdif(X1, X2, D) <= (X1 <  X2) & (D == X2 - X1)",

    # Distancia Manhattan: dist(X1, Y1, X2, Y2, D). Las 4 coords deben venir
    # ligadas desde el llamador (no se enumeran).
    "dist(X1, Y1, X2, Y2, D) <= absdif(X1, X2, DX) & absdif(Y1, Y2, DY) & (D == DX + DY)",

    # ===== B - Disponibilidad y energia =====
    "ocupado(A)  <= estado_agente(A, 'ocupado')",
    "averiado(A) <= estado_agente(A, 'averiado')",

    "necesita_recarga(A) <= energia(A, E) & umbral_recarga(A, U) & (E <= U)"
    " & estado_agente(A, Est) & (Est != 'averiado')",

    "disponible(A) <= estado_agente(A, 'libre')"
    " & energia(A, E) & umbral_recarga(A, U) & (E > U)",

    "apto_para(A, Cap) <= disponible(A) & capacidad(A, Cap)",

    "estacion_candidata(A, EX, EY, D) <= necesita_recarga(A) & posicion(A, AX, AY)"
    " & estacion_recarga(EX, EY) & dist(AX, AY, EX, EY, D)",

    # Mejor estacion: menor distancia; empate -> menor (EX, EY) lexicografico.
    # `estacion_recarga(EX, EY)` aparece en cada clausula para que las
    # variables del head queden ligadas en el cuerpo (range-restriction).
    "existe_mejor_estacion(A, EX, EY, D) <= estacion_recarga(EX, EY)"
    " & estacion_candidata(A, EX2, EY2, D2) & (D2 < D)",
    "existe_mejor_estacion(A, EX, EY, D) <= estacion_recarga(EX, EY)"
    " & estacion_candidata(A, EX2, EY2, D) & (EX2 < EX)",
    "existe_mejor_estacion(A, EX, EY, D) <="
    " estacion_candidata(A, EX, EY2, D) & (EY2 < EY)",

    "recarga_objetivo(A, EX, EY) <= estacion_candidata(A, EX, EY, D)"
    " & ~existe_mejor_estacion(A, EX, EY, D)",

    # ===== C - Tareas derivadas =====
    "tarea_limpieza(X, Y) <= sucia(X, Y)",
    "tarea_transporte(Obj, X, Y) <= objeto_pesado(Obj, X, Y)",

    "existe_tarea_pendiente() <= tarea_limpieza(X, Y)",
    "existe_tarea_pendiente() <= tarea_transporte(Obj, X, Y)",

    # ===== D - Asignacion (nucleo, req 3a-i) =====
    "candidato_limpieza(A, X, Y, D) <= apto_para(A, 'limpieza') & tarea_limpieza(X, Y)"
    " & posicion(A, AX, AY) & dist(AX, AY, X, Y, D)",

    # 'Existe otro candidato estrictamente mejor que A para (X,Y) a distancia D':
    #   (1) otro con menor distancia;
    #   (2) otro con misma distancia y mayor energia;
    #   (3) otro con misma distancia, misma energia y menor id lexicografico.
    "existe_mejor_limpieza(A, X, Y, D) <= candidato_limpieza(A2, X, Y, D2)"
    " & (D2 < D) & (A2 != A)",
    "existe_mejor_limpieza(A, X, Y, D) <= candidato_limpieza(A2, X, Y, D)"
    " & energia(A2, E2) & energia(A, E) & (E2 > E) & (A2 != A)",
    "existe_mejor_limpieza(A, X, Y, D) <= candidato_limpieza(A2, X, Y, D)"
    " & energia(A2, E) & energia(A, E) & (A2 < A)",

    "asignacion_limpieza(A, X, Y) <= candidato_limpieza(A, X, Y, D)"
    " & ~existe_mejor_limpieza(A, X, Y, D)",

    "agente_asignado(A) <= asignacion_limpieza(A, X, Y)",
    "agente_asignado(A) <= en_accion_conjunta(A)",

    # ===== E - Conflictos (req 3a-ii) =====
    "conflicto_potencial(X, Y, A1, A2) <= candidato_limpieza(A1, X, Y, D1)"
    " & candidato_limpieza(A2, X, Y, D2) & (A1 < A2)",
    "tarea_disputada(X, Y) <= conflicto_potencial(X, Y, A1, A2)",

    # ===== F - Accion conjunta (req 3a-iii) =====
    "apto_transporte(A, D, Obj) <= apto_para(A, 'transporte') & tarea_transporte(Obj, X, Y)"
    " & posicion(A, AX, AY) & dist(AX, AY, X, Y, D)",

    "pareja_transporte(Obj, A1, A2) <= apto_transporte(A1, D1, Obj)"
    " & apto_transporte(A2, D2, Obj) & (A1 < A2)",

    # Mejor pareja: menor suma; empate -> par lexicografico menor.
    # `agente(A1) & agente(A2)` liga las variables del head.
    "existe_mejor_pareja(Obj, A1, A2, S) <= agente(A1) & agente(A2)"
    " & apto_transporte(B1, DB1, Obj) & apto_transporte(B2, DB2, Obj)"
    " & (B1 < B2) & (DB1 + DB2 < S)",
    "existe_mejor_pareja(Obj, A1, A2, S) <= agente(A2)"
    " & apto_transporte(B1, DB1, Obj) & apto_transporte(B2, DB2, Obj)"
    " & (B1 < B2) & (DB1 + DB2 == S) & (B1 < A1)",
    "existe_mejor_pareja(Obj, A1, A2, S) <= apto_transporte(A1, DB1, Obj)"
    " & apto_transporte(B2, DB2, Obj) & (A1 < B2)"
    " & (DB1 + DB2 == S) & (B2 < A2)",

    "accion_conjunta(Obj, A1, A2) <= pareja_transporte(Obj, A1, A2)"
    " & apto_transporte(A1, D1, Obj) & apto_transporte(A2, D2, Obj)"
    " & (S == D1 + D2) & ~existe_mejor_pareja(Obj, A1, A2, S)",

    "en_accion_conjunta(A) <= accion_conjunta(Obj, A, A2)",
    "en_accion_conjunta(A) <= accion_conjunta(Obj, A1, A)",

    # ===== G - Replanificacion (req 3a-iv) =====
    "replan_sin_energia(A) <= agente_asignado(A) & necesita_recarga(A)",
    # NOTA: en el modelo snapshot un agente averiado nunca queda en
    # agente_asignado (asignacion_limpieza exige disponible -> libre). La
    # regla se incluye por contrato; quedara vacia salvo que en iteraciones
    # posteriores se introduzca una asignacion persistente entre ticks.
    "replan_averiado(A) <= agente_asignado(A) & averiado(A)",
    "agente_ocioso(A) <= disponible(A) & ~agente_asignado(A) & existe_tarea_pendiente()",
    "a_zona_espera(A) <= disponible(A) & ~agente_asignado(A) & ~existe_tarea_pendiente()",

    "requiere_replanificacion() <= replan_sin_energia(A)",
    "requiere_replanificacion() <= replan_averiado(A)",
    "requiere_replanificacion() <= agente_ocioso(A)",

    # ===== H - Metricas (base; agregacion en Python) =====
    "agente_trabajando(A) <= agente_asignado(A)",
    "tarea_pendiente_limpieza(X, Y) <= tarea_limpieza(X, Y)",
    "cooperacion_activa(Obj) <= accion_conjunta(Obj, A1, A2)",
    "agente_recargando(A) <= necesita_recarga(A)",
    "agente_recargando(A) <= estado_agente(A, 'recargando')",
]


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------

def _query(consulta: str) -> list[tuple]:
    """Helper: devuelve list[tuple] con los bindings; lista vacia si no hay."""
    res = pyDatalog.ask(consulta)
    if res is None:
        return []
    return [tuple(t) for t in res.answers]


def _query_bool(consulta: str) -> bool:
    """Helper para predicados 0-arios."""
    return pyDatalog.ask(consulta) is not None


def planificar() -> dict:
    """Ejecuta las consultas de coordinacion y devuelve un dict de decisiones."""
    asignaciones_limpieza = sorted(_query("asignacion_limpieza(A, X, Y)"))
    acciones_conjuntas    = sorted(_query("accion_conjunta(Obj, A1, A2)"))
    recargas              = sorted(_query("recarga_objetivo(A, EX, EY)"))
    a_espera              = sorted(t[0] for t in _query("a_zona_espera(A)"))

    replan_se = {t[0] for t in _query("replan_sin_energia(A)")}
    replan_av = {t[0] for t in _query("replan_averiado(A)")}
    ociosos   = {t[0] for t in _query("agente_ocioso(A)")}
    replanificar = sorted(replan_se | replan_av | ociosos)

    return {
        "asignaciones_limpieza": asignaciones_limpieza,
        "acciones_conjuntas":    acciones_conjuntas,
        "recargas":              recargas,
        "a_espera":              a_espera,
        "replanificar":          replanificar,
        "requiere_replan":       _query_bool("requiere_replanificacion()"),
    }


def calcular_metricas() -> dict:
    """Agrega en Python los resultados de las reglas de la familia H + E."""
    tareas_pendientes = len(_query("tarea_pendiente_limpieza(X, Y)"))

    trabajando = {t[0] for t in _query("agente_trabajando(A)")}
    agentes_trabajando = len(trabajando)

    todos = _query("agente(A)")
    total = len(todos)
    tasa_ocupacion = (agentes_trabajando / total) if total > 0 else 0.0

    energias = [t[1] for t in _query("energia(A, E)")]
    energia_promedio = (sum(energias) / len(energias)) if energias else 0.0

    cooperaciones_activas  = len(_query("cooperacion_activa(Obj)"))
    conflictos_potenciales = len(_query("conflicto_potencial(X, Y, A1, A2)"))

    recargando = {t[0] for t in _query("agente_recargando(A)")}
    agentes_recargando = len(recargando)

    return {
        "tareas_pendientes":      tareas_pendientes,
        "agentes_trabajando":     agentes_trabajando,
        "tasa_ocupacion":         tasa_ocupacion,
        "energia_promedio":       energia_promedio,
        "cooperaciones_activas":  cooperaciones_activas,
        "conflictos_potenciales": conflictos_potenciales,
        "agentes_recargando":     agentes_recargando,
    }


# ---------------------------------------------------------------------------
# Verificacion (pruebas de aceptacion)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import modelo_datos

    cfg = modelo_datos.cargar_config("config.json")
    modelo_datos.declarar_vocabulario()
    modelo_datos.cargar_hechos_estaticos(cfg)
    definir_reglas()

    estado1 = modelo_datos.estado_inicial_desde_config(cfg)
    modelo_datos.refrescar_hechos_dinamicos(estado1)

    print("=== Pruebas de aceptacion (SPEC 02) ===\n")

    # 1. dist Manhattan
    assert _query("dist(0, 0, 3, 4, D)") == [(7,)], "dist(0,0,3,4) != 7"
    assert _query("dist(5, 5, 5, 5, D)") == [(0,)], "dist(5,5,5,5) != 0"
    assert _query("dist(2, 7, 8, 1, D)") == [(12,)], "dist(2,7,8,1) != 12"
    print("[ok] 1. dist Manhattan correcto sobre 3 casos")

    # 2. transitable excluye obstaculos
    for (ox, oy) in [(3, 4), (3, 5), (10, 8), (7, 7)]:
        assert _query(f"transitable({ox}, {oy})") == [], (
            f"({ox},{oy}) no deberia ser transitable"
        )
    assert _query("transitable(0, 0)") != [], "(0,0) deberia ser transitable"
    print("[ok] 2. transitable excluye los 3 obstaculos fijos + 1 movil")

    # 3. disponible: los 4 agentes con estado_inicial='libre' y energia alta
    libres = sorted(t[0] for t in _query("disponible(A)"))
    assert libres == ["a1", "a2", "a3", "a4"], f"disponible={libres}"
    print(f"[ok] 3. disponible: {libres}")

    # 4. asignacion_limpieza: ninguna celda con dos asignados
    asigs = _query("asignacion_limpieza(A, X, Y)")
    celdas = [(x, y) for _, x, y in asigs]
    assert len(celdas) == len(set(celdas)), f"celdas duplicadas: {asigs}"
    print(f"[ok] 4. asignaciones sin celda repetida ({len(asigs)} asignaciones)")

    # 5. accion_conjunta: exactamente 1 pareja, sin duplicados simetricos
    accs = _query("accion_conjunta(Obj, A1, A2)")
    assert len(accs) == 1, f"se esperaba 1 accion conjunta, hay {len(accs)}: {accs}"
    obj, a1, a2 = accs[0]
    assert a1 < a2, f"par no ordenado {(a1, a2)}"
    assert {a1, a2} == {"a3", "a4"}, f"pareja inesperada: {(a1, a2)}"
    print(f"[ok] 5. accion conjunta unica: {obj} -> ({a1}, {a2})")

    # 6. conflicto_potencial: pares con A1 < A2
    confs = _query("conflicto_potencial(X, Y, A1, A2)")
    for (x, y, c1, c2) in confs:
        assert c1 < c2, f"conflicto mal ordenado {(x, y, c1, c2)}"
    print(f"[ok] 6. conflictos potenciales: {len(confs)} (todos con A1 < A2)")

    # 7. Todos con energia bajo umbral -> sin asignacion, todos con recarga
    estado_baja = {
        "agentes": [
            {"id": "a1", "x": 5,  "y": 5,  "energia": 10, "estado": "libre"},
            {"id": "a2", "x": 6,  "y": 5,  "energia": 10, "estado": "libre"},
            {"id": "a3", "x": 8,  "y": 8,  "energia": 10, "estado": "libre"},
            {"id": "a4", "x": 9,  "y": 9,  "energia": 10, "estado": "libre"},
        ],
        "celdas_sucias":      estado1["celdas_sucias"],
        "obstaculos_moviles": estado1["obstaculos_moviles"],
        "objetos_pesados":    estado1["objetos_pesados"],
    }
    modelo_datos.refrescar_hechos_dinamicos(estado_baja)
    assert _query("asignacion_limpieza(A, X, Y)") == [], (
        "no deberia haber asignaciones con todos bajo umbral"
    )
    recargas_ids = sorted({t[0] for t in _query("recarga_objetivo(A, EX, EY)")})
    assert recargas_ids == ["a1", "a2", "a3", "a4"], (
        f"todos deberian tener objetivo de recarga, hay: {recargas_ids}"
    )
    print("[ok] 7. todos bajo umbral -> 0 asignaciones, recarga p/ los 4")

    # 8. Estado con averiado + tareas pendientes -> requiere_replan True
    #    (la replanificacion se dispara por agente_ocioso de a3/a4 que no
    #     pueden limpiar; ver nota en G2 sobre replan_averiado.)
    estado_av = {
        "agentes": [
            {"id": "a1", "x": 0,  "y": 0,  "energia": 100, "estado": "averiado"},
            {"id": "a2", "x": 0,  "y": 0,  "energia": 100, "estado": "libre"},
            {"id": "a3", "x": 0,  "y": 14, "energia": 140, "estado": "libre"},
            {"id": "a4", "x": 14, "y": 14, "energia": 140, "estado": "libre"},
        ],
        "celdas_sucias":      [{"x": 5, "y": 5}, {"x": 6, "y": 5}],
        "obstaculos_moviles": [],
        "objetos_pesados":    [],
    }
    modelo_datos.refrescar_hechos_dinamicos(estado_av)
    plan_av = planificar()
    assert plan_av["requiere_replan"] is True, (
        f"requiere_replan deberia ser True, plan: {plan_av}"
    )
    print("[ok] 8. estado con averiado + tareas -> requiere_replan=True")

    # 9. calcular_metricas: tipos y rango
    modelo_datos.refrescar_hechos_dinamicos(estado1)
    m = calcular_metricas()
    campos = {
        "tareas_pendientes", "agentes_trabajando", "tasa_ocupacion",
        "energia_promedio", "cooperaciones_activas", "conflictos_potenciales",
        "agentes_recargando",
    }
    assert set(m.keys()) == campos, f"faltan/sobran campos: {set(m.keys())}"
    for k in ("tareas_pendientes", "agentes_trabajando",
              "cooperaciones_activas", "conflictos_potenciales",
              "agentes_recargando"):
        assert isinstance(m[k], int), f"{k} no es int: {type(m[k]).__name__}"
    assert isinstance(m["tasa_ocupacion"], float), "tasa_ocupacion no es float"
    assert isinstance(m["energia_promedio"], float), "energia_promedio no es float"
    assert 0.0 <= m["tasa_ocupacion"] <= 1.0, (
        f"tasa_ocupacion fuera de [0,1]: {m['tasa_ocupacion']}"
    )
    print("[ok] 9. metricas: 7 campos, tipos correctos, tasa_ocupacion en [0,1]")

    print("[ok] 10. script corre end-to-end sin errores")

    # ---- Salida final con estado inicial ----
    print("\n=== Plan final (estado inicial) ===")
    plan_final = planificar()
    for k, v in plan_final.items():
        print(f"  {k}: {v}")

    print("\n=== Metricas (estado inicial) ===")
    for k, v in calcular_metricas().items():
        if isinstance(v, float):
            print(f"  {k}: {v:.3f}")
        else:
            print(f"  {k}: {v}")
