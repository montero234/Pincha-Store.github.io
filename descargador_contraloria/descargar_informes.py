# -*- coding: utf-8 -*-
"""
Descargador de informes de la Contraloria General de la Republica (Peru).

Lee un CSV con los numeros de informe, busca cada uno en el buscador publico,
extrae los enlaces del PDF del informe y de su ficha resumen, los descarga y
genera un CSV actualizado con las columnas:
    url_informe, url_ficha, estado_descarga, ruta_pdf, ruta_ficha, observacion

Uso basico (Windows, desde la carpeta del script):
    python descargar_informes.py informes.csv

Opciones utiles:
    --visible            muestra el navegador mientras trabaja
    --diagnostico        procesa solo el primer informe y guarda capturas,
                         HTML y trafico de red en la carpeta "diagnostico"
    --url URL            URL del buscador (si cambia la oficial)
    --columna NOMBRE     nombre de la columna con el numero de informe
    --salida CARPETA     carpeta donde se guardan los PDF (por defecto "pdfs")

El script se puede volver a ejecutar: salta los informes ya descargados.
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Falta Playwright. Ejecuta:  pip install playwright  y luego  "
          "python -m playwright install chromium")
    sys.exit(1)

URL_BUSCADOR_DEFECTO = "https://appbp.contraloria.gob.pe/BuscadorCGR/Informes/inicio.html"

COLUMNAS_NUEVAS = ["url_informe", "url_ficha", "estado_descarga",
                   "ruta_pdf", "ruta_ficha", "observacion"]

# Nombres probables de la columna con el numero de informe
CANDIDATAS_NUMERO = ["numero_informe", "nro_informe", "n_informe", "num_informe",
                     "informe", "numero", "nro", "codigo_informe", "codigo"]
CANDIDATAS_ENTIDAD = ["entidad", "nombre_entidad", "entidad_auditada"]
CANDIDATAS_TITULO = ["titulo", "asunto", "titulo_informe", "nombre_informe"]

# Patron de numero de informe: 8640-2026-CG/GRCU-AOP, 012-2025-2-0215-SCE, etc.
PATRON_NUMERO = re.compile(r"\b\d{1,6}-\d{4}-[A-Z0-9][A-Z0-9/\-_.]*", re.I)

PALABRAS_FICHA = ("ficha", "resumen", "fichaResumen".lower())
PALABRAS_PDF = (".pdf", "descarga", "download", "documento", "archivo", "verpdf",
                "getfile", "obtenerarchivo", "visor")

log = logging.getLogger("descargador")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")


def clave_numero(numero):
    """Forma comparable de un numero de informe (sin espacios ni separadores)."""
    return re.sub(r"[^A-Z0-9]", "", str(numero).upper())


def nombre_archivo(numero, sufijo=""):
    base = re.sub(r"[\\/:*?\"<>|\s]+", "-", str(numero).strip())
    base = re.sub(r"-{2,}", "-", base).strip("-.") or "sin_numero"
    return f"{base}{sufijo}.pdf"


def leer_csv(ruta):
    datos = Path(ruta).read_bytes()
    for codificacion in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            texto = datos.decode(codificacion)
            break
        except UnicodeDecodeError:
            continue
    muestra = texto[:5000]
    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;\t|")
        delim = dialecto.delimiter
    except csv.Error:
        delim = ";" if muestra.count(";") > muestra.count(",") else ","
    lector = csv.DictReader(texto.splitlines(), delimiter=delim)
    filas = [dict(f) for f in lector]
    return filas, list(lector.fieldnames or []), delim


def escribir_csv(ruta, filas, columnas, delim):
    tmp = Path(str(ruta) + ".tmp")
    # utf-8-sig para que Excel en Windows muestre bien las tildes
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columnas, delimiter=delim, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    tmp.replace(ruta)


def elegir_columna(columnas, candidatas, preferida=None):
    if preferida:
        if preferida in columnas:
            return preferida
        raise SystemExit(f"La columna '{preferida}' no existe. Columnas: {columnas}")
    norm = {normalizar(c): c for c in columnas}
    for cand in candidatas:
        if cand in norm:
            return norm[cand]
    for cand in candidatas:
        for n, original in norm.items():
            if cand in n:
                return original
    return None


def detectar_columna_numero(filas, columnas, preferida):
    col = elegir_columna(columnas, CANDIDATAS_NUMERO, preferida)
    if col:
        return col
    # Ultimo recurso: la columna donde mas celdas parecen numero de informe
    mejor, puntos = None, 0
    for c in columnas:
        p = sum(1 for f in filas[:50] if PATRON_NUMERO.search(f.get(c) or ""))
        if p > puntos:
            mejor, puntos = c, p
    if not mejor:
        raise SystemExit("No encontre la columna con el numero de informe. "
                         "Indicala con --columna NOMBRE")
    return mejor


# --------------------------------------------------------------------------- #
# Extraccion de enlaces
# --------------------------------------------------------------------------- #
URL_EN_TEXTO = re.compile(r"""https?://[^\s"'<>\\]+""", re.I)


def es_enlace_documento(url):
    u = url.lower()
    return any(p in u for p in PALABRAS_PDF)


def es_ficha(url, texto=""):
    t = (url + " " + (texto or "").split("||")[0]).lower()
    return any(p in t for p in PALABRAS_FICHA)


def urls_en_json(obj, encontrados, base):
    """Recorre un JSON y recoge todo valor que parezca enlace a documento."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                v2 = v.strip()
                kl = k.lower()
                if v2.lower().startswith(("http", "/")) and (
                        es_enlace_documento(v2) or "url" in kl or "ruta" in kl
                        or "link" in kl or "archivo" in kl or "pdf" in kl):
                    # contexto = clave || registro completo (para saber a que informe pertenece)
                    registro = json.dumps(obj, ensure_ascii=False)[:3000]
                    encontrados.append((urljoin(base, v2), k + "||" + registro))
            else:
                urls_en_json(v, encontrados, base)
    elif isinstance(obj, list):
        for v in obj:
            urls_en_json(v, encontrados, base)


class Recolector:
    """Escucha el trafico de red de la pagina para capturar enlaces de PDF."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.candidatos = []   # (url, contexto)
        self.trafico = []      # para diagnostico

    def on_response(self, resp):
        try:
            url = resp.url
            ctype = (resp.headers or {}).get("content-type", "").lower()
            self.trafico.append({"url": url, "status": resp.status, "tipo": ctype})
            if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
                self.candidatos.append((url, "respuesta_pdf"))
            elif "json" in ctype or "javascript" in ctype or "text" in ctype:
                if resp.request.resource_type not in ("xhr", "fetch"):
                    return
                cuerpo = resp.text()
                try:
                    urls_en_json(json.loads(cuerpo), self.candidatos, url)
                except ValueError:
                    for u in URL_EN_TEXTO.findall(cuerpo):
                        if es_enlace_documento(u):
                            self.candidatos.append((u, "texto_respuesta"))
        except Exception:  # la pagina puede cerrarse mientras leemos
            pass


def enlaces_dom(page):
    """Devuelve (url, texto) de anchors/botones con pinta de documento."""
    try:
        return page.evaluate("""() => {
            const out = [];
            const attrs = ['href','data-url','data-href','data-file','onclick','src'];
            document.querySelectorAll('a, button, iframe, embed, object, [onclick], [data-url]')
              .forEach(el => {
                const txt = (el.innerText || el.title || el.getAttribute('aria-label') || '').trim();
                attrs.forEach(a => {
                  const v = el.getAttribute(a);
                  if (!v) return;
                  const m = v.match(/(https?:\\/\\/[^'"\\s)]+|\\/[^'"\\s)]+\\.pdf[^'"\\s)]*)/i);
                  if (m) out.push([new URL(m[1], location.href).href, txt]);
                });
              });
            return out;
        }""")
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Busqueda en el buscador
# --------------------------------------------------------------------------- #
SELECTORES_CAJA = [
    "input[placeholder*='nforme' i]",
    "input[placeholder*='mero' i]",
    "input[name*='informe' i]",
    "input[id*='informe' i]",
    "input[placeholder*='uscar' i]",
    "input[type='search']",
    "input[type='text']:visible",
]


def buscar_caja(page):
    for sel in SELECTORES_CAJA:
        loc = page.locator(sel)
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible() and el.is_enabled():
                    return el
            except Exception:
                continue
    return None


def pulsar_buscar(page, caja):
    caja.press("Enter")
    for nombre in ("Buscar", "BUSCAR", "Consultar", "Filtrar"):
        btn = page.get_by_role("button", name=nombre)
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3000)
                return
        except Exception:
            pass


def esperar_resultados(page, segundos=20):
    try:
        page.wait_for_load_state("networkidle", timeout=segundos * 1000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)


def abrir_botones_resultado(page, context, recolector, numero):
    """Hace clic en botones de 'ver/descargar' del resultado para provocar
    la carga del PDF cuando el sitio no expone el enlace en el HTML."""
    clave = clave_numero(numero)
    patrones = re.compile(r"(descarg|ver informe|ver pdf|ficha|resumen|pdf|documento)", re.I)
    botones = page.locator("a, button").filter(has_text=patrones)
    try:
        n = min(botones.count(), 6)
    except Exception:
        return
    for i in range(n):
        b = botones.nth(i)
        try:
            if not b.is_visible():
                continue
            # Solo botones dentro de la fila/tarjeta que menciona el informe
            contenedor = b.evaluate(
                "el => (el.closest('tr, li, .card, article, .resultado, div') || el).innerText")
            if clave and clave not in clave_numero(contenedor):
                continue
            texto = b.inner_text().strip()
            nuevas = []
            context.on("page", nuevas.append)
            try:
                with page.expect_download(timeout=8000) as dl_info:
                    b.click(timeout=5000)
                dl = dl_info.value
                recolector.candidatos.append((dl.url, "descarga " + texto))
            except PWTimeout:
                pass
            finally:
                context.remove_listener("page", nuevas.append)
            for p in nuevas:
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=10000)
                    recolector.candidatos.append((p.url, "popup " + texto))
                    p.close()
                except Exception:
                    pass
        except Exception as e:
            log.debug("Boton %s fallo: %s", i, e)


def clasificar(candidatos, numero):
    """Elige la mejor URL de informe y de ficha entre los candidatos."""
    clave = clave_numero(numero)
    vistos, url_inf, url_fic = set(), None, None
    puntuados = []
    for url, ctx in candidatos:
        if not url or url.startswith(("blob:", "data:", "about:")) or url in vistos:
            continue
        vistos.add(url)
        if not (es_enlace_documento(url) or "pdf" in (ctx or "").lower()
                or ctx.startswith(("descarga", "popup", "respuesta_pdf"))):
            continue
        p = 0
        if url.lower().split("?")[0].endswith(".pdf") or ctx == "respuesta_pdf":
            p += 2
        if clave and clave[:8] in clave_numero(url):
            p += 3
        if clave and "||" in (ctx or ""):
            # registro JSON: premia si menciona el informe, penaliza si no
            p += 5 if clave in clave_numero(ctx.split("||", 1)[1]) else -5
        puntuados.append((p, url, ctx))
    puntuados.sort(key=lambda t: -t[0])
    for _, url, ctx in puntuados:
        if es_ficha(url, ctx):
            url_fic = url_fic or url
        else:
            url_inf = url_inf or url
    return url_inf, url_fic


def descargar(context, url, destino, intentos=3):
    ultimo_error = ""
    for n in range(1, intentos + 1):
        try:
            resp = context.request.get(url, timeout=120_000)
            cuerpo = resp.body()
            if resp.ok and cuerpo[:5] == b"%PDF-":
                destino.write_bytes(cuerpo)
                return True, ""
            ultimo_error = f"HTTP {resp.status}, no es PDF ({resp.headers.get('content-type','')})"
        except Exception as e:
            ultimo_error = str(e)[:200]
        time.sleep(2 ** n)
    return False, ultimo_error


def procesar_informe(page, context, recolector, url_buscador, numero, extra, diag_dir=None):
    recolector.reset()
    page.goto(url_buscador, wait_until="domcontentloaded", timeout=60_000)
    esperar_resultados(page, 15)

    caja = buscar_caja(page)
    if caja is None:
        raise RuntimeError("No encontre la caja de busqueda (usa --diagnostico)")
    caja.fill("")
    caja.fill(str(numero))
    pulsar_buscar(page, caja)
    esperar_resultados(page)

    texto = page.inner_text("body")
    encontrado = clave_numero(numero) in clave_numero(texto)
    if not encontrado and extra:
        # Respaldo: buscar por entidad / titulo
        caja = buscar_caja(page)
        if caja:
            caja.fill(extra[:120])
            pulsar_buscar(page, caja)
            esperar_resultados(page)
            texto = page.inner_text("body")
            encontrado = clave_numero(numero) in clave_numero(texto)

    if diag_dir:
        diag_dir.mkdir(exist_ok=True)
        page.screenshot(path=str(diag_dir / "resultado.png"), full_page=True)
        (diag_dir / "resultado.html").write_text(page.content(), encoding="utf-8")

    if not encontrado:
        return None, None, "no_encontrado"

    for url, txt in enlaces_dom(page):
        recolector.candidatos.append((url, txt))
    url_inf, url_fic = clasificar(recolector.candidatos, numero)
    if not url_inf:
        abrir_botones_resultado(page, context, recolector, numero)
        for url, txt in enlaces_dom(page):
            recolector.candidatos.append((url, txt))
        url_inf, url_fic = clasificar(recolector.candidatos, numero)

    if diag_dir:
        (diag_dir / "trafico_red.json").write_text(
            json.dumps({"trafico": recolector.trafico,
                        "candidatos": recolector.candidatos}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    return url_inf, url_fic, "encontrado"


# --------------------------------------------------------------------------- #
# Programa principal
# --------------------------------------------------------------------------- #
def lanzar_navegador(p, visible):
    # Intenta usar Google Chrome instalado; si no, el Chromium de Playwright.
    for canal in ("chrome", "msedge", None):
        try:
            kwargs = {"headless": not visible}
            if canal:
                kwargs["channel"] = canal
            nav = p.chromium.launch(**kwargs)
            log.info("Navegador: %s", canal or "chromium de Playwright")
            return nav
        except Exception:
            continue
    raise SystemExit("No pude abrir ningun navegador. Ejecuta: "
                     "python -m playwright install chromium")


def main():
    ap = argparse.ArgumentParser(description="Descarga informes de la Contraloria")
    ap.add_argument("csv", help="CSV con los informes")
    ap.add_argument("--url", default=URL_BUSCADOR_DEFECTO, help="URL del buscador")
    ap.add_argument("--columna", help="columna con el numero de informe")
    ap.add_argument("--salida", default="pdfs", help="carpeta de descarga")
    ap.add_argument("--csv-salida", help="CSV resultado (por defecto <csv>_actualizado.csv)")
    ap.add_argument("--visible", action="store_true", help="mostrar el navegador")
    ap.add_argument("--diagnostico", action="store_true",
                    help="procesar solo el primer informe y guardar detalles")
    ap.add_argument("--reintentos", type=int, default=3)
    ap.add_argument("--pausa", type=float, default=2.0, help="segundos entre informes")
    args = ap.parse_args()

    ruta_csv = Path(args.csv)
    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)
    csv_salida = Path(args.csv_salida or ruta_csv.with_name(ruta_csv.stem + "_actualizado.csv"))

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler("descarga.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])

    # Si ya existe un resultado previo, se continua desde ahi.
    fuente = csv_salida if csv_salida.exists() else ruta_csv
    filas, columnas, delim = leer_csv(fuente)
    col_num = detectar_columna_numero(filas, columnas, args.columna)
    col_ent = elegir_columna(columnas, CANDIDATAS_ENTIDAD)
    col_tit = elegir_columna(columnas, CANDIDATAS_TITULO)
    for c in COLUMNAS_NUEVAS:
        if c not in columnas:
            columnas.append(c)
    log.info("Leidos %d informes de %s (columna numero: '%s')", len(filas), fuente, col_num)

    diag_dir = Path("diagnostico") if args.diagnostico else None
    if args.diagnostico:
        args.visible = True

    with sync_playwright() as p:
        nav = lanzar_navegador(p, args.visible)
        context = nav.new_context(accept_downloads=True, locale="es-PE")
        page = context.new_page()
        recolector = Recolector()
        page.on("response", recolector.on_response)

        for i, fila in enumerate(filas, 1):
            numero = (fila.get(col_num) or "").strip()
            m = PATRON_NUMERO.search(numero)
            numero = m.group(0) if m else numero
            if not numero:
                fila["estado_descarga"] = "sin_numero"
                continue
            if fila.get("estado_descarga") == "ok" and fila.get("ruta_pdf") \
                    and Path(fila["ruta_pdf"]).exists():
                log.info("[%d/%d] %s ya descargado, se omite", i, len(filas), numero)
                continue

            extra = " ".join(x for x in (fila.get(col_ent) if col_ent else "",
                                         fila.get(col_tit) if col_tit else "") if x)
            log.info("[%d/%d] Buscando %s", i, len(filas), numero)
            url_inf = url_fic = None
            estado, obs = "error", ""
            for intento in range(1, args.reintentos + 1):
                try:
                    url_inf, url_fic, estado = procesar_informe(
                        page, context, recolector, args.url, numero, extra, diag_dir)
                    break
                except Exception as e:
                    obs = f"intento {intento}: {str(e)[:200]}"
                    log.warning("  %s", obs)
                    time.sleep(2 ** intento)

            fila["url_informe"] = url_inf or ""
            fila["url_ficha"] = url_fic or ""
            fila["ruta_pdf"] = fila["ruta_ficha"] = ""

            if estado == "encontrado" and url_inf:
                destino = salida / nombre_archivo(numero)
                ok, err = descargar(context, url_inf, destino, args.reintentos)
                if ok:
                    fila["ruta_pdf"] = str(destino)
                    estado, obs = "ok", ""
                else:
                    estado, obs = "error_descarga", err
                if url_fic:
                    destino_f = salida / nombre_archivo(numero, "_ficha")
                    ok_f, err_f = descargar(context, url_fic, destino_f, args.reintentos)
                    if ok_f:
                        fila["ruta_ficha"] = str(destino_f)
                    else:
                        obs = (obs + " | ficha: " + err_f).strip(" |")
            elif estado == "encontrado":
                estado, obs = "sin_enlace_pdf", "informe hallado pero sin enlace PDF visible"

            fila["estado_descarga"] = estado
            fila["observacion"] = obs
            log.info("  -> %s %s", estado, fila["ruta_pdf"] or obs)
            escribir_csv(csv_salida, filas, columnas, delim)  # guardado incremental

            if args.diagnostico:
                log.info("Modo diagnostico: revisa la carpeta 'diagnostico'.")
                break
            time.sleep(args.pausa)

        nav.close()

    escribir_csv(csv_salida, filas, columnas, delim)
    resumen = {}
    for f in filas:
        resumen[f.get("estado_descarga") or "pendiente"] = \
            resumen.get(f.get("estado_descarga") or "pendiente", 0) + 1
    log.info("Terminado. Resumen: %s", resumen)
    log.info("CSV actualizado: %s", csv_salida)


if __name__ == "__main__":
    main()
