#!/usr/bin/env python3
"""Genera un escenario aleatorio (random_config.json) y corre la simulacion.

Toma config.json como plantilla y conserva todo lo que define la "identidad"
del escenario feliz (ids de agentes/objetos, capacidades, energias, costos,
umbrales, conteos de cada categoria y parametros de simulacion). Lo unico que
randomiza son las POSICIONES de inicio de cada elemento espacial:

  - obstaculos_fijos
  - obstaculos_moviles (posicion inicial + ruta del patron)
  - estaciones_recarga
  - zonas_espera
  - celdas_sucias_iniciales
  - objetos_pesados
  - agentes (posicion_inicial)
  - eventos.nuevas_sucias

NO modifica config.json. Escribe random_config.json y luego invoca
simulador.py --config random_config.json (reenviando --headless / --max-ticks).

Las posiciones generadas respetan las invariantes que valida
modelo_datos.validar_config:
  - todo dentro de la grilla
  - sin celdas duplicadas por categoria
  - estaciones / zonas / sucias / agentes no caen sobre obstaculos fijos
  - se preservan ids y la cantidad de transportistas (>=2 si hay objetos pesados)

Ademas, para generar un escenario "limpio", se evita que dos elementos
distintos ocupen exactamente la misma celda inicial (salvo la ruta del
obstaculo movil, que naturalmente pasa por celdas ya usadas).

Uso:
    python simular_aleatorio.py                      # genera + corre (ventana)
    python simular_aleatorio.py --headless           # sin ventana PyGame
    python simular_aleatorio.py --seed 7             # reproducible
    python simular_aleatorio.py --solo-generar       # genera y no corre
    python simular_aleatorio.py --max-ticks 100      # se reenvia al simulador
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import subprocess
import sys

import modelo_datos

AQUI = os.path.dirname(os.path.abspath(__file__))
PLANTILLA = os.path.join(AQUI, "config.json")
SALIDA = os.path.join(AQUI, "random_config.json")


def _celdas_libres(ancho: int, alto: int, ocupadas: set[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(x, y) for x in range(ancho) for y in range(alto) if (x, y) not in ocupadas]


def _tomar(rng: random.Random, ancho: int, alto: int, ocupadas: set[tuple[int, int]],
           prohibidas: set[tuple[int, int]] | None = None) -> tuple[int, int]:
    """Devuelve una celda libre al azar, marcandola como ocupada.

    `ocupadas` impide reutilizar la celda; `prohibidas` (p. ej. obstaculos
    fijos) la excluye sin marcarla, por si otra categoria si pudiera usarla.
    """
    veto = ocupadas | (prohibidas or set())
    libres = _celdas_libres(ancho, alto, veto)
    if not libres:
        raise RuntimeError("no quedan celdas libres para ubicar todos los elementos")
    celda = rng.choice(libres)
    ocupadas.add(celda)
    return celda


def _ruta_aleatoria(rng: random.Random, inicio: tuple[int, int], largo: int,
                    ancho: int, alto: int, obstaculos: set[tuple[int, int]]) -> list[dict]:
    """Camina al azar por celdas adyacentes validas (no obstaculos fijos)."""
    ruta = [inicio]
    actual = inicio
    for _ in range(max(0, largo - 1)):
        cx, cy = actual
        vecinos = [
            (nx, ny)
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1))
            if 0 <= nx < ancho and 0 <= ny < alto and (nx, ny) not in obstaculos
        ]
        if not vecinos:
            break
        actual = rng.choice(vecinos)
        ruta.append(actual)
    return [{"x": x, "y": y} for (x, y) in ruta]


def generar_config_aleatoria(plantilla: dict, seed: int) -> dict:
    """Devuelve una copia de la plantilla con posiciones aleatorias."""
    rng = random.Random(seed)
    cfg = copy.deepcopy(plantilla)

    ancho = cfg["grilla"]["ancho"]
    alto = cfg["grilla"]["alto"]

    ocupadas: set[tuple[int, int]] = set()

    # 1) Obstaculos fijos primero: condicionan al resto.
    obstaculos_fijos: set[tuple[int, int]] = set()
    for o in cfg.get("obstaculos_fijos", []):
        x, y = _tomar(rng, ancho, alto, ocupadas)
        o["x"], o["y"] = x, y
        obstaculos_fijos.add((x, y))

    # 2) Estaciones, zonas, sucias, objetos pesados, agentes: celdas unicas
    #    y fuera de obstaculos fijos (los obstaculos ya estan en `ocupadas`).
    for clave in ("estaciones_recarga", "zonas_espera", "celdas_sucias_iniciales",
                  "objetos_pesados"):
        for item in cfg.get(clave, []):
            x, y = _tomar(rng, ancho, alto, ocupadas)
            item["x"], item["y"] = x, y

    for ag in cfg.get("agentes", []):
        x, y = _tomar(rng, ancho, alto, ocupadas)
        ag["posicion_inicial"] = {"x": x, "y": y}

    # 3) Obstaculos moviles: posicion inicial unica.
    for om in cfg.get("obstaculos_moviles", []):
        x, y = _tomar(rng, ancho, alto, ocupadas)
        om["x"], om["y"] = x, y

    # 4) Eventos: nuevas celdas sucias (fuera de obstaculos fijos, sin repetir).
    for ev in cfg.get("eventos", {}).get("nuevas_sucias", []):
        x, y = _tomar(rng, ancho, alto, ocupadas, prohibidas=obstaculos_fijos)
        ev["x"], ev["y"] = x, y

    # 5) Patron del obstaculo movil: ruta = camino aleatorio desde su inicio,
    #    mismo largo que en la plantilla. Las celdas de la ruta pueden coincidir
    #    con otros elementos (el obstaculo simplemente pasa por ahi).
    inicio_por_id = {om["id"]: (om["x"], om["y"]) for om in cfg.get("obstaculos_moviles", [])}
    for patron in cfg.get("eventos", {}).get("obstaculos_moviles_patron", []):
        inicio = inicio_por_id.get(patron["id"])
        if inicio is None:
            continue
        largo = len(patron.get("ruta", [])) or 1
        patron["ruta"] = _ruta_aleatoria(rng, inicio, largo, ancho, alto, obstaculos_fijos)

    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Genera random_config.json (posiciones aleatorias) y corre la simulacion."
    )
    parser.add_argument("--seed", type=int, default=None,
                        help="semilla para reproducir el escenario aleatorio")
    parser.add_argument("--solo-generar", action="store_true",
                        help="solo escribe random_config.json, no corre el simulador")
    parser.add_argument("--headless", action="store_true",
                        help="se reenvia al simulador: sin ventana PyGame")
    parser.add_argument("--max-ticks", type=int, default=None,
                        help="se reenvia al simulador: cap de ticks")
    args = parser.parse_args()

    # Semilla: la pedida o una al azar (se imprime para poder reproducir).
    seed = args.seed if args.seed is not None else random.randrange(1_000_000)

    with open(PLANTILLA, encoding="utf-8") as f:
        plantilla = json.load(f)

    cfg = generar_config_aleatoria(plantilla, seed)

    # Validamos con el mismo contrato del simulador antes de escribir/correr.
    modelo_datos.validar_config(cfg)

    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"[ok] random_config.json generado (seed={seed})")
    print(f"     reproducible con: python simular_aleatorio.py --seed {seed}")

    if args.solo_generar:
        return 0

    cmd = [sys.executable, os.path.join(AQUI, "simulador.py"), "--config", SALIDA]
    if args.headless:
        cmd.append("--headless")
    if args.max_ticks is not None:
        cmd += ["--max-ticks", str(args.max_ticks)]

    print(f"[run] {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=AQUI)


if __name__ == "__main__":
    raise SystemExit(main())
