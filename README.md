# 🥝 KIWI — Suite Inteligente de PDFs y Conversión a Markdown

<p align="center">
  <img src="assets/kiwi_logo.png" alt="KIWI Mascot" width="180"/>
</p>

**KIWI** es una herramienta integral y 100% local (sin dependencias en la nube ni envío de datos a servidores) diseñada para:
1. **Recolección Masiva y Deduplicación**: Recorrer árboles de carpetas con cientos de subdirectorios, identificar todos los PDFs, deduplicarlos mediante SHA-256 y organizarlos sin colisiones.
2. **Conversor Avanzado a Markdown (`pdfmd`)**: Transformar documentos PDF en archivos Markdown estructurados y limpios para Obsidian, Notion o Pandoc, con soporte de **tablas pipe-table**, **ecuaciones matemáticas LaTeX**, **exportación de imágenes** y **OCR local**.
3. **Pipeline Completo**: Recolectar, deduplicar y convertir a Markdown en un solo proceso automatizado.
4. **Conversor Web a Markdown (`htmlmd`)**: Capturar paginas web —por URL o desde un `.html` guardado— con su procedencia, conservando enlaces, tablas e imagenes.

---

## 🌟 Módulos y Características

### 1. 📁 Recolector Inteligente
- **Modo Simulacro**: Analiza y genera un informe CSV detallado de previsualización sin alterar archivos.
- **Deduplicación por SHA-256**: Identifica archivos con contenido idéntico optimizando el cálculo sobre candidatos de igual tamaño.
- **Exclusión de subcarpetas**: Omite ramas completas del árbol (backups, temporales…) desde la interfaz o la CLI.
- **Trazabilidad en CSV**: Informe compatible con Microsoft Excel (UTF-8 con BOM y separador `;`), con rutas legibles.
- **Soporte de Rutas Largas**: Compatibilidad total con rutas de Windows superiores a 260 caracteres (`\\?\`), transparente para el usuario.
- **Idempotencia**: Reejecutar sobre el mismo destino no duplica archivos; los marca como `YA_EXISTE`.

### 2. 📝 Conversor a Markdown (`pdfmd`)
- **Hipervínculos conservados**: las anotaciones `/Link` del PDF se emiten como enlace Markdown (`[texto](url)`), partiendo el span cuando el enlace cubre solo unas palabras de la línea. Los enlaces son parte del contenido. Las URL que no consiguen anclarse a ningún texto no se pierden: quedan al pie de su página como `<!-- enlaces sin ancla (p.N) -->`.
- **Diagnóstico de la capa de texto**: el frontmatter lleva siempre `chars_extraidos`, `chars_por_pagina` y `capa_texto` (`nativa` / `ocr` / `ausente`). Por debajo de 800 caracteres por página el documento se marca como `ausente` y además se escribe un **aviso visible en el cuerpo**, para que un PDF escaneado no pase por bueno en silencio.
- **Palabras partidas recompuestas**: una palabra cortada por el guión de fin de línea (`docu-` / `mentation`) vuelve a ser `documentation` —si no, `grep "documentation"` no lo encuentra—, mientras que `long-term`, `well-known` o `left-right` conservan su guión. El corte se cose aunque cruce bloque o salto de página.
- **Extracción Estructurada**: Detecta jerarquías de títulos (`#`, `##`, `###`) basadas en el tamaño de fuente y estilo.
- **Detección de Tablas**: Tablas nativas del PDF y regiones alineadas por espacios, convertidas a tablas Markdown estándar (`| Col 1 | Col 2 |`).
- **Ecuaciones LaTeX**: Las fórmulas completas se emiten como bloque `$$…$$`; los símbolos sueltos dentro de un párrafo se envuelven en línea (`$\alpha$`) sin romper la prosa.
- **Exportación de Imágenes**: Guarda las imágenes del PDF en una subcarpeta `_assets/` vinculada, reutilizando un único fichero para logotipos y marcas de agua repetidos.
- **Limpieza de Encabezados/Pies**: Elimina cabeceras, pies recurrentes y numeraciones de página sueltas.
- **Listas Normalizadas**: Convierte viñetas tipográficas (`•`, `▪`, `–`) en sintaxis Markdown y agrupa los elementos en una sola lista.
- **Notas al pie**: Se recolectan del pie del PDF y se emiten al final del documento en sintaxis Markdown nativa (`[^1]`), renumeradas de corrido y enlazadas en ambos sentidos con su llamada en el texto.
- **Citabilidad**: Marcador `<!-- p.7 -->` al inicio de cada página con el número real del PDF, en comentario HTML para no ensuciar el render.
- **Trazabilidad**: Frontmatter con URL de origen, fecha de captura y **SHA-256 del PDF**, que garantiza que el Markdown y el PDF siguen siendo el mismo documento.
- **Ligaduras deshechas**: Convierte `ﬁ`, `ﬂ`, `ﬃ` en sus letras reales, sin lo cual el Markdown no sería buscable.
- **Nombres en slug ASCII**: `Informe Anual: Situación 2026's.pdf` → `informe-anual-situacion-2026s.md`.
- **Metadatos YAML**: Frontmatter con título, autor, materia, palabras clave y fichero de origen, escapado para no romper el YAML. Incluye `idioma` detectado y `lineas_suprimidas`, la lista de cabeceras y pies borrados del cuerpo, para poder auditar la limpieza.
- **Soporte OCR Local**: Detección automática de páginas escaneadas, OCR por página con Tesseract y OCR del documento completo con OCRmyPDF.

### 3. 📋 Conversión por lotes

- **Reanudable**: se salta los PDF que ya tienen un `.md` cuyo `sha256_pdf` coincide. Una conversión de cientos de documentos no se reinicia desde cero porque falle uno a mitad. Si el PDF cambia, el hash deja de coincidir y se reconvierte.
- **Manifiesto de control**: un TSV junto a la salida con `archivo`, `paginas`, `chars`, `chars_pag`, `capa_texto`, `enlaces` y `avisos`, **ordenado por caracteres por página ascendente**: las primeras filas son exactamente los documentos que hay que revisar a mano.

### 4. 🌐 Conversor Web a Markdown (`htmlmd`)

Módulo hermano de `pdfmd`: comparte con él el modelo de bloques y el renderizador, de modo que un `.md` salido de una web y otro salido de un PDF **son el mismo formato**. Solo cambia la fase de extracción. No añade ninguna dependencia: todo se apoya en la biblioteca estándar.

- **Estructura declarada, no inferida**: en el PDF un título se adivina por el tamaño de fuente; en HTML viene dicho en `<h2>`. Encabezados, listas, tablas, citas y bloques de código se toman del propio documento en lugar de reconstruirlos.
- **Separación del artículo respecto del cromo**: se podan menús, pies, avisos de cookies, barras de compartir y bloques de «te puede interesar», y el cuerpo se localiza por `<article>`, `<main>`, `role=main` o, en último término, por densidad de prosa penalizada por densidad de enlaces. El criterio empleado queda anotado en `criterio_cuerpo` y lo suprimido en `elementos_suprimidos`, para poder auditar la limpieza. Ningún contenedor que acumule más del 40 % del texto se poda jamás: ante la duda, conservar.
- **Enlaces conservados**: los `href` relativos se resuelven a absolutos, las anclas internas (`#seccion`) y los `javascript:` se descartan del recuento, y los enlaces que viven **dentro de celdas de tabla o de definiciones `<dd>`** conservan su destino en lugar de quedar en texto plano.
- **Diagnóstico del contenido**: el equivalente web de `capa_texto`. Una página que solo se pinta con JavaScript, un muro de pago o un aviso de cookies servido en lugar del artículo producen un `.md` casi vacío; por debajo de 500 caracteres el documento se marca como `escaso` (o `ausente`) y además se escribe un **aviso visible en el cuerpo**, para que no pase por bueno en silencio.
- **Procedencia**: frontmatter con `url_origen` (la canónica o la final tras redirecciones), `url_solicitada` cuando difiere, `estado_http`, `fecha_captura` y **SHA-256 del HTML tal como vino por el cable**.
- **Metadatos del documento**: título, autor, fecha de publicación e idioma se leen de `<meta>`, Open Graph y **JSON-LD**, que en prensa digital es la fuente fiable de la firma y la fecha. El sufijo del medio se recorta del título (`Kiwi - Wikipedia` → `Kiwi`) solo cuando la cola es de verdad el nombre del sitio.
- **Imágenes con su `alt`**: se descargan a `_assets/` con `--export-images`, deduplicadas por contenido, conservando el texto alternativo —que en una web suele ser la única descripción de la figura—. Los `data:` URI y los píxeles de seguimiento se descartan.
- **Integrado en toda la aplicación**: cuarta pestaña de la interfaz, subcomando `web` de la CLI y presente en `KIWI.exe` (el `import` de `htmlmd` en `kiwi_app.py` es lo que hace que PyInstaller lo incluya).
- **Lote reanudable**: se salta las URLs cuyo `.md` ya existe, escribe un **manifiesto TSV** ordenado por caracteres ascendente (las primeras filas son las capturas que hay que revisar a mano) y espera entre peticiones para no saturar el servidor.

---

## 🚀 Guía de Uso

### Interfaz Visual (GUI)
Haz **doble clic en `KIWI.bat`** o ejecuta:
```bash
python kiwi_app.py
```

La interfaz cuenta con 4 pestañas:
- **Pestaña 1 (Recolector)**: Para escanear carpetas y generar la copia limpia y el informe CSV.
- **Pestaña 2 (Conversor a Markdown)**: Para convertir un PDF individual o una carpeta completa a Markdown.
- **Pestaña 3 (Pipeline Completo)**: Para recolectar un árbol de carpetas y exportar directamente su versión en Markdown.
- **Pestaña 4 (Web a Markdown)**: Para capturar una URL, un `.html` guardado o una lista entera de URLs.

Las rutas y las opciones marcadas se recuerdan entre sesiones en `~/.kiwi_config.json`.

---

### Línea de Comandos (CLI)

#### 0. Conversión de una página web a Markdown:
```bash
python kiwi_app.py web https://ejemplo.com/articulo -o articulo.md --export-images
```
El mismo conversor está disponible como módulo suelto, con idénticas opciones:
```bash
python -m htmlmd.cli https://ejemplo.com/articulo -o articulo.md --export-images
```
Un `.html` ya guardado, declarando su procedencia real:
```bash
python kiwi_app.py web pagina_guardada.html -o pagina.md --url https://ejemplo.com/articulo
```
Lote a partir de un fichero con una URL por línea (admite comentarios con `#`):
```bash
python kiwi_app.py web --lista urls.txt -o salida/ --pausa 2
```
Opciones: `--no-frontmatter`, `--no-tables`, `--no-links`, `--no-code-blocks`,
`--no-slug`, `--no-alt`, `--pagina-completa` (no podar nada), `--timeout`,
`--user-agent`, `--insecure`, `--refrescar`.

#### 1. Conversión de PDF a Markdown:
```bash
python kiwi_app.py convert documento.pdf -o documento.md --export-images
```
O usando el CLI de `pdfmd`:
```bash
python -m pdfmd.cli documento.pdf -o documento.md --export-images
```

Opciones de conversión: `--ocr-mode {off,auto,tesseract,ocrmypdf}`, `--ocr-lang spa`,
`--password`, `--preview-only`, `--insert-page-breaks`, `--no-tables`,
`--no-equations`, `--no-headers-footers`, `--no-frontmatter`, `--no-code-blocks`,
`--no-footnotes`, `--no-page-markers`, `--no-cover-mark`, `--no-hash`, `--no-slug`,
`--no-links`, `--no-join-hyphens`, `--no-lang`, `--min-chars-pagina N`,
`--url-origen`, `--fecha-captura`.

#### Conversión por lotes

```bash
python kiwi_app.py convert "C:\corpus\POR PROCESAR" --lote "C:\corpus\markdown"
```

También desde el ejecutable, sin Python instalado:

```bash
KIWI-cli.exe convert "C:\corpus\POR PROCESAR" --lote "C:\corpus\markdown"
```

Recorre el árbol de carpetas, convierte todos los PDF a la carpeta de salida con
nombre en slug ASCII y deja un `manifiesto_conversion.tsv`. Relanzar el mismo
comando continúa donde se quedó; con `--no-reanudar` se reconvierte todo.

#### Diagnóstico de la conversión

El frontmatter permite decidir de qué ficheros fiarse sin abrir el PDF:

```yaml
url_origen: "https://ejemplo.org/2026/07/29/..."   # null si se desconoce
fecha_captura: "2026-08-26"
sha256_pdf: "fb379aefdb0f..."
idioma: "en"
chars_extraidos: 49926
chars_por_pagina: 3120
capa_texto: "nativa"        # nativa | ocr | ausente
ocr_motor: null
enlaces: 34
lineas_suprimidas: ["Nombre de la Organización - Informe Anual"]
```

#### Notas al pie y citabilidad

El Markdown resultante está pensado para poder citarse sin volver al PDF:

```markdown
---
url_origen: "https://ejemplo.org/informe-2026.pdf"
fecha_captura: "2026-08-26"
sha256_pdf: "2aa07f9cbc2d6c93..."
---

<!-- fin-de-portada -->
<!-- p.7 -->

La tesis fue documentada por el autor[^1] en su obra.

[^1]: Véase la referencia del capítulo anterior.
```

`url_origen` se rellena solo si Windows guardó la procedencia al descargar el PDF
(flujo `Zone.Identifier`, que sobrevive a la recolección); si no, se indica con
`--url-origen`. Las notas cuya llamada no puede localizarse en el texto **no se
pierden**: van a un apartado `## Notas sin ancla` al final.

#### 2. Recolección de PDFs:
```bash
python kiwi_app.py collect "C:\origen" "C:\destino" --simulacro
```
O con el script tradicional (el subcomando `collect` es opcional):
```bash
python recolector_pdf.py "C:\origen" "C:\destino" --simulacro
```

Opciones de recolección: `--simulacro`, `--sin-dedup`, `--replicar`,
`--excluir "backups,temp"`, `--sin-validar-cabecera`.

> La validación de la cabecera `%PDF-` está **activa por defecto**: los archivos con
> extensión `.pdf` que no lo sean se descartan y quedan anotados en el informe.
> El informe CSV se guarda junto a la carpeta de destino.

---

## 🏗️ Compilación a ejecutable

Doble clic en **`build_exe.bat`** (o `python -m PyInstaller` con los mismos parámetros)
genera en `dist/`:

| Ejecutable | Uso | Consola |
|---|---|---|
| `KIWI.exe` | Interfaz gráfica, para repartir a usuarios finales | No |
| `KIWI-cli.exe` | Mismos subcomandos `convert` / `collect` que el script | Sí |

Ambos son autocontenidos (~61 MB): incluyen Python, PyMuPDF, Pillow, Tkinter y los
recursos gráficos, por lo que **no requieren Python instalado** en el equipo destino.
El primer arranque de cada sesión tarda unos segundos porque el formato *onefile* se
descomprime en una carpeta temporal.

> El OCR sigue siendo externo: si el equipo destino no tiene Tesseract u OCRmyPDF
> instalados, KIWI avisa y convierte sin OCR.

---

## 🧪 Pruebas

```bash
python run_tests.py
```

- `test_kiwi.py` — motor de recolección: simulacro, copia real e idempotencia.
- `test_pdfmd.py` — pipeline completo sobre un PDF sintético con títulos, tabla, fórmula, lista, código e imagen.
- `test_unidades.py` — heurísticas concretas: ecuaciones, paginación, defragmentación, tablas, viñetas, YAML, rutas largas y deduplicación de imágenes.
- `test_notas.py` — notas al pie (renumeración global, varias líneas, modo degradado), marcadores de página, frontmatter de procedencia, slug, ligaduras y fin de portada.
- `test_mejoras.py` — mejoras de conversión: hipervínculos, detección de capa de texto, palabras partidas, nombre ASCII, manifiesto y lote reanudable.
- `test_htmlmd.py` — conversor web: estructura, enlaces, limpieza de menús y pies, procedencia, imágenes, listas estructuradas y lote reanudable.
- `test_integracion_web.py` — el conversor web dentro de la aplicación: interfaz gráfica, CLI y ejecutable.

---

## 📦 Instalación

```bash
pip install -r requirements.txt
```

El OCR es opcional. Para activarlo instala **Tesseract** (OCR por página) y/o
**OCRmyPDF** (OCR del documento completo) en el sistema, más los paquetes de Python
comentados en `requirements.txt`. Si el motor elegido no está disponible, KIWI avisa
y continúa la conversión sin OCR.

---

## 📁 Estructura del Proyecto

```
kiwi/
├── assets/
│   ├── kiwi_logo.png         # Logotipo oficial de la mascota KIWI
│   └── kiwi_logo.ico         # Icono para ventanas y accesos directos
├── pdfmd/                    # Motor de conversión PDF -> Markdown
│   ├── __init__.py
│   ├── models.py             # Dataclasses del documento y opciones
│   ├── utils.py              # OCR, imágenes, slug, hash y ligaduras
│   ├── extract.py            # Extracción con PyMuPDF, tablas nativas y OCR
│   ├── footnotes.py          # Notas al pie -> sintaxis Markdown nativa
│   ├── batch.py              # Lote reanudable por hash y manifiesto TSV
│   ├── transform.py          # Títulos, listas, cabeceras/pies y defragmentación
│   ├── tables.py             # Detección heurística de tablas
│   ├── equations.py          # Unicode matemático -> LaTeX
│   ├── render.py             # Renderizado Markdown, frontmatter y assets
│   ├── pipeline.py           # Orquestación del proceso completo
│   └── cli.py                # CLI del motor de conversión
├── htmlmd/                   # Conversor web -> Markdown (solo biblioteca estándar)
│   ├── fetch.py              # Descarga o lectura de disco del HTML
│   ├── dom.py                # Árbol DOM mínimo sobre html.parser
│   ├── boilerplate.py        # Poda de menús, pies y avisos
│   ├── extract.py            # HTML -> modelo de bloques de pdfmd
│   ├── render.py             # Frontmatter de procedencia web
│   ├── batch.py              # Lote reanudable, secciones e INDEX.md
│   ├── pipeline.py           # Orquestación del proceso
│   └── cli.py                # CLI del conversor web
├── kiwi_app.py               # Aplicación principal con interfaz de pestañas
├── recolector_pdf.py         # Compatibilidad CLI del recolector
├── KIWI.bat                  # Lanzador gráfico
├── build_exe.bat             # Compilación de KIWI.exe y KIWI-cli.exe
├── dist/                     # Ejecutables generados (no versionado)
├── run_tests.py              # Ejecutor de la batería de pruebas
├── test_kiwi.py              # Pruebas del recolector
├── test_pdfmd.py             # Pruebas del pipeline de conversión
├── test_unidades.py          # Pruebas unitarias de heurísticas
├── test_notas.py             # Pruebas de notas al pie y citabilidad
├── test_mejoras.py           # Pruebas de las mejoras de conversión
├── test_htmlmd.py            # Pruebas del conversor web
├── test_integracion_web.py   # Pruebas de integración del conversor web
├── requirements.txt          # Dependencias
├── LICENSE                   # Licencia MIT
└── README.md                 # Documentación
```

---

## 📄 Licencia

Distribuido bajo la licencia MIT. Consulta el fichero [`LICENSE`](LICENSE).
