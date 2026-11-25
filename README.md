# ☁️ FileJump Sync Studio

> **Descripción corta sugerida para GitHub (About):**
> Explorador dual estilo Windows, sincronizador de archivos de alta velocidad y centro multimedia web para la nube de FileJump construido en Python (Flask) + Tailwind CSS.
>
> **Topics sugeridos:** `filejump`, `cloud-sync`, `python`, `flask`, `web-audio-api`, `file-explorer`, `mp3-player`, `equalizer`

---

**FileJump Sync Studio** es una suite web local desarrollada en Python y Flask que combina un **comparador y sincronizador bidireccional/incremental** para **FileJump REST API** con un **explorador dual estilo Windows** (Equipo Local vs. Nube FileJump) y un **reproductor multimedia integrado** con lectura de etiquetas ID3, ecualizador gráfico de 5 bandas y visualizador rítmico en tiempo real.

---

## ✨ Características Principales

### 🔄 Sincronización y Comparación Inteligente
* **Vista Dual Comparativa (Local vs. Nube):** Identifica de un vistazo archivos **Sincronizados**, **Nuevos** (pendientes de subir), **Modificados** (por tamaño o fecha UTC) y **Solo en FileJump**.
* **Subida Paralela de Alta Velocidad:** Soporte multihilo (`ThreadPoolExecutor`) y conexión directa al endpoint de subida rápida de FileJump.
* **Protección contra Cambios Masivos:** Umbral de impacto configurable (%) que solicita confirmación explícita antes de sobrescribir o alterar grandes volúmenes de archivos.
* **Gestión Segura de Credenciales:** Configuración visual de tokens `Bearer` y URLs almacenadas localmente en `.env`.

### 🗂️ Explorador Dual Estilo Windows
* **Panel Dividido (Árbol + Contenido):** Árbol jerárquico lateral izquierdo con **carga perezosa (`+` / `−`)** bajo demanda y tabla de contenido a la derecha con barra de ruta (*breadcrumbs*) y navegación por doble clic.
* **Exploración Completa de Discos Locales:** Detección automática de unidades locales (`C:\`, `D:\`, `E:\`, montajes Unix) con apertura directa sin duplicar archivos en caché.

### 🎬 Visor Universal de Archivos y Caché Temporal Automática
* **Caché Temporal Inteligente (`temp_cache`):** Descarga archivos remotos únicamente bajo demanda al abrirlos y **elimina automáticamente** toda la carpeta temporal al cerrar el servidor (`atexit` + manejadores de señales).
* **Soporte Multiformato:**
  * **PDF:** Apertura directa en nueva ventana con visor nativo.
  * **Ofimática (`.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`):** Extracción y renderizado de vista previa estructurada sin dependencias externas pesadas.
  * **Texto y Código (`.txt`, `.log`, `.json`, `.py`, `.md`, `.csv`, `.sql`, etc.):** Lector monoespaciado integrado.
  * **Imágenes y Videos:** Visor integrado con carrusel (`◀ Anterior` / `Siguiente ▶` y atajos de teclado `←` `→` `Esc`) y soporte opcional de transcodificación con FFmpeg para formatos `.avi`/`.mkv`.

### 🎵 Reproductor Musical Flotante (ID3 + EQ 5 Bandas + Visualizador)
* **3 Modos de Vista:** Minimizado en burbuja compacta tipo chat en vivo, panel flotante normal o vista **Maximizada** con carátula en gran tamaño y lista de reproducción (*Playlist*).
* **Lector Nativo de Metadatos ID3v2 / ID3v1:** Extrae Título, Artista, Álbum y **carátula incrustada (`APIC`)** directamente desde los MP3 locales o remotos.
* **Precarga en Caché + Modo Aleatorio:** Genera colas aleatorias y descarga en segundo plano la siguiente canción remota para una reproducción sin interrupciones.
* **Ecualizador Gráfico de 5 Bandas (`Web Audio API`):** Bandas de `60Hz`, `230Hz`, `910Hz`, `3.6kHz` y `14kHz` (`-12 dB` a `+12 dB`) con presets (*Plano, Bass Boost, Electrónica, Pop, Rock, Vocal*).
* **Visualizador Rítmico en Tiempo Real:** Analizador de espectro en `<canvas>` activable/desactivable con 3 modos visuales (*Barras, Espejo, Onda*).

---

## 🛠️ Requisitos e Instalación

1. **Clonar el repositorio:**
   ```bash
   git clone https://github.com/totoday72/FileJump-Sync-Web-Studio.git
   cd FileJump-Sync-Web-Studio
   ```

2. **Instalar dependencias de Python:**
   ```bash
   pip install -r requirements.txt
   ```
   *(Opcional)* Instala **FFmpeg** en tu sistema si deseas convertir al vuelo videos `.avi` o `.mkv` dentro del navegador.

3. **Ejecutar la aplicación:**
   ```bash
   python filejump_sync_web_studio.py
   ```

4. **Abrir en el navegador:**
   Ingresa a `http://127.0.0.1:5000`, abre **Configuración & .env**, pega tu `FILEJUMP_API_KEY` y añade tus carpetas locales a sincronizar.

---

## 📁 Estructura del Proyecto

```text
├── filejump_sync_web_studio.py # Servidor Flask, UI, visor y reproductor Web Audio
├── requirements.txt        # Dependencias de Python
├── .env                    # Credenciales API (ignorado en Git por seguridad)
├── sync_config.json        # Tareas y parámetros de sincronización
├── sync_records/           # Historial JSON de archivos sincronizados por tarea
└── temp_cache/             # Caché temporal de descargas (se auto-elimina al cerrar)
```

---

## 🔒 Datos locales

.env, `sync_config.json`, `sync_records/` y `temp_cache/` contienen credenciales o datos locales y están excluidos del repositorio mediante `.gitignore`.