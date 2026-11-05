# Descargador de informes de la Contraloría

Busca cada informe del CSV en el buscador público de la Contraloría, descarga el
PDF del informe y su ficha resumen, y genera `informes_actualizado.csv` con las
columnas `url_informe`, `url_ficha`, `estado_descarga`, `ruta_pdf`, `ruta_ficha`
y `observacion`.

## Uso en Windows

1. Instala Python 3 (https://www.python.org/downloads/) marcando **"Add python.exe to PATH"**.
2. Copia tu CSV a esta carpeta con el nombre `informes.csv`.
3. Haz doble clic en `ejecutar.bat`.

Los PDF se guardan en `pdfs\` con nombres como `8640-2026-CG-GRCU-AOP.pdf` y
`8640-2026-CG-GRCU-AOP_ficha.pdf`. El detalle queda en `descarga.log`.

Si Google Chrome o Edge están instalados, el script los usa; si no, usa el
Chromium que instala Playwright.

## El CSV

Puede estar separado por comas o por punto y coma (como lo exporta Excel). El
script detecta la columna del número de informe (`numero_informe`, `nro_informe`,
`informe`…). Si no la encuentra:

```
python descargar_informes.py informes.csv --columna "Nº Informe"
```

Si hay columnas `entidad` o `titulo`, se usan como búsqueda de respaldo.

## Primera prueba recomendada

```
python descargar_informes.py informes.csv --diagnostico
```

Este comando procesa solo el primer informe, muestra el navegador y guarda en
`diagnostico\` una captura, el HTML y el tráfico de red. Si algo falla,
comparte esa carpeta para ajustar el script al buscador.

## Otras opciones

| Opción | Para qué sirve |
|---|---|
| `--url URL` | Usar otra dirección del buscador si cambia la oficial |
| `--visible` | Ver el navegador mientras trabaja |
| `--salida CARPETA` | Elegir la carpeta de los PDF |
| `--pausa 2` | Segundos de espera entre informes |
| `--reintentos 3` | Número de reintentos por informe |

Puedes volver a ejecutarlo cuando quieras: continúa desde
`informes_actualizado.csv` y omite los informes que ya tienen estado `ok`.
