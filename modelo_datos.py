"""
AgentClean - Modelo de datos y configuracion (SPEC 01).

Responsabilidades:
  - Cargar y validar config.json.
  - Declarar el vocabulario unico de hechos pyDatalog.
  - Cargar hechos estaticos una sola vez.
  - Refrescar (retractar + reasertar) los hechos dinamicos en cada tick.

Fuera de alcance: reglas pyDatalog, asignacion de tareas, loop de simulacion,
PyGame, Prolog.

Contrato del dict `estado` (fotografia del mundo en un tick):
{
  "agentes": [
      {"id": str, "x": int, "y": int, "energia": int, "estado": str},
      ...
  ],
  "celdas_sucias":     [{"x": int, "y": int}, ...],
  "obstaculos_moviles":[{"id": str, "x": int, "y": int}, ...],
  "objetos_pesados":   [{"id": str, "x": int, "y": int}, ...],
}
"""

from __future__ import annotations

import json
from typing import Any, NoReturn

from pyDatalog import pyDatalog


CAPACIDADES_VALIDAS = {"limpieza", "transporte", "supervision"}
ESTADOS_VALIDOS = {"libre", "ocupado", "recargando", "averiado"}


# Vocabulario pyDatalog (seccion 3 de la spec). Cadena unica para create_terms.
_TERMINOS_ESTATICOS = [
    "grilla",
    "obstaculo_fijo",
    "estacion_recarga",
    "zona_espera",
    "agente",
    "capacidad",
    "energia_maxima",
    "costo_mover",
    "costo_accion",
    "umbral_recarga",
]

_TERMINOS_DINAMICOS = [
    "posicion",
    "energia",
    "estado_agente",
    "sucia",
    "obstaculo_movil",
    "objeto_pesado",
]


# Registro de hechos dinamicos asertados en el ultimo refresh, para poder
# retractarlos exactamente antes de volver a asertar los nuevos.
# Cada entrada: (nombre_predicado, tupla_de_args).
_hechos_dinamicos_activos: list[tuple[str, tuple]] = []

_vocabulario_declarado = False


# ---------------------------------------------------------------------------
# Carga y validacion de config
# ---------------------------------------------------------------------------

def cargar_config(ruta: str) -> dict:
    """Lee y parsea el JSON de configuracion. Valida y devuelve el dict."""
    with open(ruta, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    validar_config(cfg)
    return cfg


def _err(msg: str) -> NoReturn:
    raise ValueError(f"config invalida: {msg}")


def _es_int_no_neg(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _es_int_pos(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _validar_xy(item: Any, contexto: str, ancho: int, alto: int) -> tuple[int, int]:
    if not isinstance(item, dict) or "x" not in item or "y" not in item:
        _err(f"{contexto}: se esperaba objeto con campos 'x' e 'y', se recibio {item!r}")
    x, y = item["x"], item["y"]
    if not (_es_int_no_neg(x) and _es_int_no_neg(y)):
        _err(f"{contexto}: x e y deben ser enteros >= 0, se recibio x={x!r}, y={y!r}")
    if not (0 <= x < ancho and 0 <= y < alto):
        _err(
            f"{contexto}: coordenada ({x},{y}) fuera de la grilla "
            f"{ancho}x{alto} (rango valido x in [0,{ancho - 1}], y in [0,{alto - 1}])"
        )
    return x, y


def validar_config(cfg: dict) -> None:
    """Valida la configuracion. Lanza ValueError ante la primera violacion."""
    if not isinstance(cfg, dict):
        _err("la raiz debe ser un objeto JSON")

    # --- grilla ---
    grilla = cfg.get("grilla")
    if not isinstance(grilla, dict) or "ancho" not in grilla or "alto" not in grilla:
        _err("'grilla' debe ser un objeto con 'ancho' y 'alto'")
    ancho, alto = grilla["ancho"], grilla["alto"]
    if not (_es_int_pos(ancho) and _es_int_pos(alto)):
        _err(f"'grilla.ancho' y 'grilla.alto' deben ser enteros > 0 (recibido {ancho}, {alto})")

    # --- listas con solo {x,y} ---
    obstaculos_fijos: set[tuple[int, int]] = set()
    for i, item in enumerate(cfg.get("obstaculos_fijos", [])):
        xy = _validar_xy(item, f"obstaculos_fijos[{i}]", ancho, alto)
        if xy in obstaculos_fijos:
            _err(f"obstaculos_fijos[{i}]: celda ({xy[0]},{xy[1]}) duplicada")
        obstaculos_fijos.add(xy)

    estaciones: set[tuple[int, int]] = set()
    for i, item in enumerate(cfg.get("estaciones_recarga", [])):
        xy = _validar_xy(item, f"estaciones_recarga[{i}]", ancho, alto)
        if xy in obstaculos_fijos:
            _err(
                f"estaciones_recarga[{i}]: ({xy[0]},{xy[1]}) coincide con un "
                f"obstaculo fijo; una celda obstaculo no puede ser estacion"
            )
        if xy in estaciones:
            _err(f"estaciones_recarga[{i}]: celda ({xy[0]},{xy[1]}) duplicada")
        estaciones.add(xy)

    zonas_espera: set[tuple[int, int]] = set()
    for i, item in enumerate(cfg.get("zonas_espera", [])):
        xy = _validar_xy(item, f"zonas_espera[{i}]", ancho, alto)
        if xy in obstaculos_fijos:
            _err(
                f"zonas_espera[{i}]: ({xy[0]},{xy[1]}) coincide con un obstaculo "
                f"fijo; una celda obstaculo no puede ser zona de espera"
            )
        if xy in zonas_espera:
            _err(f"zonas_espera[{i}]: celda ({xy[0]},{xy[1]}) duplicada")
        zonas_espera.add(xy)

    sucias: set[tuple[int, int]] = set()
    for i, item in enumerate(cfg.get("celdas_sucias_iniciales", [])):
        xy = _validar_xy(item, f"celdas_sucias_iniciales[{i}]", ancho, alto)
        if xy in obstaculos_fijos:
            _err(
                f"celdas_sucias_iniciales[{i}]: ({xy[0]},{xy[1]}) coincide con un "
                f"obstaculo fijo; una celda obstaculo no puede estar sucia"
            )
        if xy in sucias:
            _err(f"celdas_sucias_iniciales[{i}]: celda ({xy[0]},{xy[1]}) duplicada")
        sucias.add(xy)

    # --- listas con {id, x, y} ---
    ids_om: set[str] = set()
    for i, item in enumerate(cfg.get("obstaculos_moviles", [])):
        if not isinstance(item, dict) or "id" not in item:
            _err(f"obstaculos_moviles[{i}]: se esperaba objeto con 'id', 'x', 'y'")
        oid = item["id"]
        if not isinstance(oid, str) or not oid:
            _err(f"obstaculos_moviles[{i}].id debe ser string no vacio (recibido {oid!r})")
        if oid in ids_om:
            _err(f"obstaculos_moviles[{i}]: id '{oid}' duplicado")
        ids_om.add(oid)
        _validar_xy(item, f"obstaculos_moviles[{i}]", ancho, alto)

    ids_obj: set[str] = set()
    objetos_pesados = cfg.get("objetos_pesados", [])
    for i, item in enumerate(objetos_pesados):
        if not isinstance(item, dict) or "id" not in item:
            _err(f"objetos_pesados[{i}]: se esperaba objeto con 'id', 'x', 'y'")
        oid = item["id"]
        if not isinstance(oid, str) or not oid:
            _err(f"objetos_pesados[{i}].id debe ser string no vacio (recibido {oid!r})")
        if oid in ids_obj:
            _err(f"objetos_pesados[{i}]: id '{oid}' duplicado")
        ids_obj.add(oid)
        _validar_xy(item, f"objetos_pesados[{i}]", ancho, alto)

    # --- agentes ---
    agentes = cfg.get("agentes")
    if not isinstance(agentes, list):
        _err("'agentes' debe ser una lista")
    if not (3 <= len(agentes) <= 5):
        _err(f"se requieren entre 3 y 5 agentes, se encontraron {len(agentes)}")

    ids_agentes: set[str] = set()
    transportistas = 0
    posiciones_iniciales: list[tuple[str, tuple[int, int]]] = []

    for i, ag in enumerate(agentes):
        if not isinstance(ag, dict):
            _err(f"agentes[{i}]: se esperaba objeto, se recibio {type(ag).__name__}")
        ctx = f"agentes[{i}]"

        aid = ag.get("id")
        if not isinstance(aid, str) or not aid:
            _err(f"{ctx}.id debe ser string no vacio (recibido {aid!r})")
        if aid in ids_agentes:
            _err(f"{ctx}: id de agente '{aid}' duplicado")
        ids_agentes.add(aid)

        caps = ag.get("capacidades")
        if not isinstance(caps, list) or len(caps) == 0:
            _err(f"{ctx}.capacidades debe ser una lista no vacia")
        for c in caps:
            if c not in CAPACIDADES_VALIDAS:
                _err(
                    f"{ctx}.capacidades: '{c}' no es valida; "
                    f"permitidas: {sorted(CAPACIDADES_VALIDAS)}"
                )
        if len(set(caps)) != len(caps):
            _err(f"{ctx}.capacidades: contiene duplicados {caps}")
        if "transporte" in caps:
            transportistas += 1

        pos = ag.get("posicion_inicial")
        x, y = _validar_xy(pos, f"{ctx}.posicion_inicial", ancho, alto)
        if (x, y) in obstaculos_fijos:
            _err(
                f"{ctx}.posicion_inicial: ({x},{y}) coincide con un obstaculo "
                f"fijo; un agente no puede iniciar sobre un obstaculo"
            )
        posiciones_iniciales.append((aid, (x, y)))

        emax = ag.get("energia_maxima")
        if not _es_int_pos(emax):
            _err(f"{ctx}.energia_maxima debe ser entero > 0 (recibido {emax!r})")
        eini = ag.get("energia_inicial")
        if not _es_int_no_neg(eini):
            _err(f"{ctx}.energia_inicial debe ser entero >= 0 (recibido {eini!r})")
        if eini > emax:
            _err(
                f"{ctx}: energia_inicial ({eini}) > energia_maxima ({emax}); "
                f"debe cumplirse 0 <= energia_inicial <= energia_maxima"
            )

        cm = ag.get("costo_mover")
        if not _es_int_no_neg(cm):
            _err(f"{ctx}.costo_mover debe ser entero >= 0 (recibido {cm!r})")
        ca = ag.get("costo_accion")
        if not _es_int_no_neg(ca):
            _err(f"{ctx}.costo_accion debe ser entero >= 0 (recibido {ca!r})")

        umb = ag.get("umbral_recarga")
        if not _es_int_no_neg(umb):
            _err(f"{ctx}.umbral_recarga debe ser entero >= 0 (recibido {umb!r})")
        if umb > emax:
            _err(
                f"{ctx}: umbral_recarga ({umb}) > energia_maxima ({emax}); "
                f"debe cumplirse 0 <= umbral_recarga <= energia_maxima"
            )

        est = ag.get("estado_inicial")
        if est not in ESTADOS_VALIDOS:
            _err(
                f"{ctx}.estado_inicial: '{est}' no es valido; "
                f"permitidos: {sorted(ESTADOS_VALIDOS)}"
            )

    # Cooperacion: si hay objetos pesados, deben existir >=2 transportistas.
    if len(objetos_pesados) > 0 and transportistas < 2:
        _err(
            f"hay {len(objetos_pesados)} objeto(s) pesado(s) pero solo "
            f"{transportistas} agente(s) con capacidad 'transporte'; "
            f"el transporte cooperativo requiere al menos 2"
        )


# ---------------------------------------------------------------------------
# Vocabulario y hechos pyDatalog
# ---------------------------------------------------------------------------

def declarar_vocabulario() -> None:
    """Declara una unica vez todos los terminos pyDatalog del contrato."""
    global _vocabulario_declarado
    if _vocabulario_declarado:
        return
    pyDatalog.create_terms(",".join(_TERMINOS_ESTATICOS + _TERMINOS_DINAMICOS))
    _vocabulario_declarado = True


def cargar_hechos_estaticos(cfg: dict) -> None:
    """Asserta los hechos estaticos (seccion 3.1) a partir de la config.

    Debe llamarse UNA sola vez por simulacion, despues de declarar_vocabulario.
    """
    if not _vocabulario_declarado:
        raise RuntimeError(
            "declarar_vocabulario() debe llamarse antes de cargar_hechos_estaticos()"
        )

    g = cfg["grilla"]
    pyDatalog.assert_fact("grilla", g["ancho"], g["alto"])

    for c in cfg.get("obstaculos_fijos", []):
        pyDatalog.assert_fact("obstaculo_fijo", c["x"], c["y"])

    for c in cfg.get("estaciones_recarga", []):
        pyDatalog.assert_fact("estacion_recarga", c["x"], c["y"])

    for c in cfg.get("zonas_espera", []):
        pyDatalog.assert_fact("zona_espera", c["x"], c["y"])

    for ag in cfg["agentes"]:
        aid = ag["id"]
        pyDatalog.assert_fact("agente", aid)
        for cap in ag["capacidades"]:
            pyDatalog.assert_fact("capacidad", aid, cap)
        pyDatalog.assert_fact("energia_maxima", aid, ag["energia_maxima"])
        pyDatalog.assert_fact("costo_mover", aid, ag["costo_mover"])
        pyDatalog.assert_fact("costo_accion", aid, ag["costo_accion"])
        pyDatalog.assert_fact("umbral_recarga", aid, ag["umbral_recarga"])


def refrescar_hechos_dinamicos(estado: dict) -> None:
    """Retracta los hechos dinamicos del tick anterior y asserta los del estado.

    Pensada para invocarse una vez por tick. Garantiza que despues de la
    llamada solo conviven los hechos dinamicos correspondientes a `estado`.
    """
    if not _vocabulario_declarado:
        raise RuntimeError(
            "declarar_vocabulario() debe llamarse antes de refrescar_hechos_dinamicos()"
        )

    # Retractar todo lo que se aserto en el refresh anterior.
    for nombre, args in _hechos_dinamicos_activos:
        pyDatalog.retract_fact(nombre, *args)
    _hechos_dinamicos_activos.clear()

    nuevos: list[tuple[str, tuple]] = []

    for ag in estado.get("agentes", []):
        nuevos.append(("posicion", (ag["id"], ag["x"], ag["y"])))
        nuevos.append(("energia", (ag["id"], ag["energia"])))
        nuevos.append(("estado_agente", (ag["id"], ag["estado"])))

    for c in estado.get("celdas_sucias", []):
        nuevos.append(("sucia", (c["x"], c["y"])))

    for o in estado.get("obstaculos_moviles", []):
        nuevos.append(("obstaculo_movil", (o["x"], o["y"])))

    for o in estado.get("objetos_pesados", []):
        nuevos.append(("objeto_pesado", (o["id"], o["x"], o["y"])))

    for nombre, args in nuevos:
        pyDatalog.assert_fact(nombre, *args)
    _hechos_dinamicos_activos.extend(nuevos)


def estado_inicial_desde_config(cfg: dict) -> dict:
    """Construye el primer `estado` del mundo a partir de la config."""
    agentes = [
        {
            "id": ag["id"],
            "x": ag["posicion_inicial"]["x"],
            "y": ag["posicion_inicial"]["y"],
            "energia": ag["energia_inicial"],
            "estado": ag["estado_inicial"],
        }
        for ag in cfg["agentes"]
    ]
    celdas_sucias = [
        {"x": c["x"], "y": c["y"]} for c in cfg.get("celdas_sucias_iniciales", [])
    ]
    obstaculos_moviles = [
        {"id": o["id"], "x": o["x"], "y": o["y"]}
        for o in cfg.get("obstaculos_moviles", [])
    ]
    objetos_pesados = [
        {"id": o["id"], "x": o["x"], "y": o["y"]}
        for o in cfg.get("objetos_pesados", [])
    ]
    return {
        "agentes": agentes,
        "celdas_sucias": celdas_sucias,
        "obstaculos_moviles": obstaculos_moviles,
        "objetos_pesados": objetos_pesados,
    }


# ---------------------------------------------------------------------------
# Verificacion manual
# ---------------------------------------------------------------------------

def _contar(predicado: str, aridad: int) -> int:
    """Cuenta hechos de un predicado consultando con variables libres."""
    vars_ = ", ".join(f"V{i}" for i in range(aridad))
    pyDatalog.create_terms(vars_)
    consulta = f"{predicado}({vars_})"
    res = pyDatalog.ask(consulta)
    return 0 if res is None else len(res.answers)


if __name__ == "__main__":
    cfg = cargar_config("config.json")
    print("[ok] config.json cargado y validado")

    declarar_vocabulario()
    print("[ok] vocabulario pyDatalog declarado")

    cargar_hechos_estaticos(cfg)
    print("[ok] hechos estaticos asertados")

    print("\nResumen de hechos estaticos:")
    print(f"  grilla:           {_contar('grilla', 2)}")
    print(f"  obstaculo_fijo:   {_contar('obstaculo_fijo', 2)}")
    print(f"  estacion_recarga: {_contar('estacion_recarga', 2)}")
    print(f"  zona_espera:      {_contar('zona_espera', 2)}")
    print(f"  agente:           {_contar('agente', 1)}")
    print(f"  capacidad:        {_contar('capacidad', 2)}")
    print(f"  energia_maxima:   {_contar('energia_maxima', 2)}")
    print(f"  costo_mover:      {_contar('costo_mover', 2)}")
    print(f"  costo_accion:     {_contar('costo_accion', 2)}")
    print(f"  umbral_recarga:   {_contar('umbral_recarga', 2)}")

    estado1 = estado_inicial_desde_config(cfg)
    refrescar_hechos_dinamicos(estado1)
    print("\nResumen de hechos dinamicos tras refresh #1 (estado inicial):")
    print(f"  posicion:         {_contar('posicion', 3)}")
    print(f"  energia:          {_contar('energia', 2)}")
    print(f"  estado_agente:    {_contar('estado_agente', 2)}")
    print(f"  sucia:            {_contar('sucia', 2)}")
    print(f"  obstaculo_movil:  {_contar('obstaculo_movil', 2)}")
    print(f"  objeto_pesado:    {_contar('objeto_pesado', 3)}")

    # Demostracion: segundo refresh con menos hechos -> los conteos no crecen.
    estado2 = {
        "agentes": [
            {"id": cfg["agentes"][0]["id"], "x": 2, "y": 3, "energia": 50, "estado": "ocupado"},
        ],
        "celdas_sucias": [{"x": 0, "y": 1}],
        "obstaculos_moviles": [],
        "objetos_pesados": [],
    }
    refrescar_hechos_dinamicos(estado2)
    print("\nResumen tras refresh #2 (estado reducido, demuestra no acumulacion):")
    print(f"  posicion:         {_contar('posicion', 3)}  (esperado 1)")
    print(f"  energia:          {_contar('energia', 2)}  (esperado 1)")
    print(f"  estado_agente:    {_contar('estado_agente', 2)}  (esperado 1)")
    print(f"  sucia:            {_contar('sucia', 2)}  (esperado 1)")
    print(f"  obstaculo_movil:  {_contar('obstaculo_movil', 2)}  (esperado 0)")
    print(f"  objeto_pesado:    {_contar('objeto_pesado', 3)}  (esperado 0)")

    print("\n[ok] ejecucion completa")
