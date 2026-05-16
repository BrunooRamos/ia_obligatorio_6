"""
Genera salidas/leyenda.png con la referencia visual de la grilla.
Los colores se mantienen sincronizados con _render() de simulador.py.

Uso:
    python leyenda.py
"""

import os
import pygame


CP = 36          # tamaño de la celda de ejemplo (px)
PAD = 18
FILA_H = 56
ANCHO = 640
SECCION_H = 30

# Colores - tienen que coincidir EXACTO con simulador.py _render()
COL_BG          = (24, 24, 28)
COL_TITULO      = (240, 240, 240)
COL_TEXTO       = (210, 210, 210)
COL_SECCION     = (160, 200, 255)

COL_TRANSITABLE = (60, 60, 60)
COL_OBST_FIJO   = (90, 90, 90)
COL_OBST_MOVIL  = (160, 90, 30)
COL_SUCIA       = (140, 100, 30)
COL_ESTACION    = (40, 140, 90)
COL_ZONA_ESPERA = (50, 80, 140)
COL_OBJ_PESADO  = (200, 200, 50)
COL_AG_LIMP     = (200, 80, 80)
COL_AG_TRANS    = (80, 160, 200)
COL_AG_SUPER    = (180, 130, 220)
COL_AG_AVER     = (60, 60, 60)
COL_ENERGIA     = (50, 200, 50)
COL_ID_LABEL    = (240, 240, 240)


def dibujar_celda(surf, x, y, color):
    pygame.draw.rect(surf, color, (x, y, CP, CP))


def dibujar_agente(surf, x, y, color, label=None, energia_ratio=None, font=None):
    """Dibuja agente como el simulador: cuadrado de fondo + circulo + label + barra."""
    dibujar_celda(surf, x, y, COL_TRANSITABLE)
    pygame.draw.circle(surf, color, (x + CP // 2, y + CP // 2), CP // 3)
    if label and font:
        txt = font.render(label, True, COL_ID_LABEL)
        surf.blit(txt, (x + CP // 4, y))
    if energia_ratio is not None:
        bar_w = int(CP * energia_ratio)
        pygame.draw.rect(surf, COL_ENERGIA, (x, y + CP - 3, bar_w, 2))


def dibujar_objeto_pesado(surf, x, y):
    """Como en el simulador: rect transitable + circulo amarillo solo contorno."""
    dibujar_celda(surf, x, y, COL_TRANSITABLE)
    pygame.draw.circle(surf, COL_OBJ_PESADO, (x + CP // 2, y + CP // 2), CP // 3, 3)


def main():
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    font      = pygame.font.SysFont("monospace", 14)
    font_sec  = pygame.font.SysFont("monospace", 15, bold=True)
    font_tit  = pygame.font.SysFont("monospace", 20, bold=True)

    entradas = [
        ("__SECCION__", "Celdas (fondo de la grilla)"),
        (COL_TRANSITABLE, "celda transitable (vacia)"),
        (COL_OBST_FIJO,   "obstaculo fijo - no se mueve, no se transita"),
        (COL_OBST_MOVIL,  "obstaculo movil - cambia de posicion segun patron"),
        (COL_SUCIA,       "celda sucia - tarea de limpieza pendiente"),
        (COL_ESTACION,    "estacion de recarga"),
        (COL_ZONA_ESPERA, "zona de espera (refugio sin tareas)"),

        ("__SECCION__", "Objetos"),
        ("__OBJ__",       "objeto pesado - requiere 2 agentes con capacidad transporte"),

        ("__SECCION__", "Agentes (circulo dentro de la celda, id arriba, barra de energia abajo)"),
        ("__AG_LIMP__",   "agente con capacidad limpieza (a1, a2)"),
        ("__AG_TRANS__",  "agente con capacidad transporte (a3)"),
        ("__AG_SUPER__",  "agente con capacidad supervision (color visible si es la 1er cap)"),
        ("__AG_AVER__",   "agente averiado - no se mueve, deja su tarea huerfana"),

        ("__SECCION__", "Detalles"),
        ("__ENERGIA__",   "barra verde inferior: energia restante (proporcional a la maxima)"),
    ]

    # Calcular alto total
    h = PAD * 2 + 40  # titulo
    for tipo, _ in entradas:
        h += SECCION_H if tipo == "__SECCION__" else FILA_H

    surf = pygame.Surface((ANCHO, h))
    surf.fill(COL_BG)

    # Titulo
    tit = font_tit.render("AgentClean - Leyenda de la grilla", True, COL_TITULO)
    surf.blit(tit, (PAD, PAD))
    y = PAD + 40

    for tipo, texto in entradas:
        if tipo == "__SECCION__":
            t = font_sec.render(texto, True, COL_SECCION)
            surf.blit(t, (PAD, y + 6))
            y += SECCION_H
            continue

        x = PAD
        if tipo == "__OBJ__":
            dibujar_objeto_pesado(surf, x, y)
        elif tipo == "__AG_LIMP__":
            dibujar_agente(surf, x, y, COL_AG_LIMP, "a1", 0.9, font)
        elif tipo == "__AG_TRANS__":
            dibujar_agente(surf, x, y, COL_AG_TRANS, "a3", 0.7, font)
        elif tipo == "__AG_SUPER__":
            dibujar_agente(surf, x, y, COL_AG_SUPER, "ax", 0.5, font)
        elif tipo == "__AG_AVER__":
            dibujar_agente(surf, x, y, COL_AG_AVER, "a1", 0.1, font)
        elif tipo == "__ENERGIA__":
            dibujar_agente(surf, x, y, COL_AG_LIMP, None, 0.4, font)
        elif isinstance(tipo, tuple):
            dibujar_celda(surf, x, y, tipo)

        t = font.render(texto, True, COL_TEXTO)
        surf.blit(t, (x + CP + 16, y + CP // 2 - 7))
        y += FILA_H

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "salidas", "leyenda.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pygame.image.save(surf, out)
    print(f"leyenda escrita en: {out}")


if __name__ == "__main__":
    main()
