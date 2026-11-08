# -*- coding: utf-8 -*-
"""Qué hace cada botón, explicado para quien no usa la terminal.

Es la única definición del texto de ayuda: la consola lo imprime al
abrir la aplicación y cada vez que se pulsa «❔ Ayuda».
"""

GUIDE = (
    ("PARA PUBLICAR TU PROYECTO", (
        ("🚀  Publicar en GitHub",
         "sube tu carpeta al repositorio. Antes te enseña qué va a "
         "cambiar y puedes dejar archivos fuera uno a uno. Lo que pongas "
         "en «Archivos y carpetas a ignorar» se queda fuera sin escribirse "
         "en el .gitignore: en GitHub no aparece ni su nombre. Si la "
         "carpeta ya publica en OTRO repositorio que el de la URL, te "
         "pregunta en cuál antes de tocar nada."),
        ("🚀🚀  Publicar seleccionados",
         "marca varios perfiles en la lista de la izquierda y los sube "
         "todos a la vez, cada uno a su repositorio."),
        ("🚀🚀  Publicar varias carpetas",
         "para subir de golpe proyectos que no tienes guardados como "
         "perfil: eliges las carpetas, le pones nombre al repositorio de "
         "cada una y se publican todas en tu cuenta. Puede crear en "
         "GitHub los repositorios que falten, y cada proyecto trae "
         "marcada la casilla para unirlo además en un único .txt, que se "
         "queda en tu equipo y no se sube. Desmarca la casilla de su número "
         "para no publicar un proyecto esta vez sin borrarlo de la lista."),
        ("🛡  Analizar",
         "revisa la carpeta en busca de archivos enormes y de "
         "contraseñas o claves, antes de que se suban. Si un archivo pasa "
         "de 100 MB (GitHub lo rechaza), al publicar puedes dejarlo fuera o "
         "subirlo con Git LFS."),
        ("🕵  Revisar historial",
         "busca contraseñas, tokens y llaves en TODOS los commits, también "
         "en archivos que ya borraste: lo que se publicó alguna vez sigue "
         "en GitHub. Te dice en qué commit entró cada uno y si sigue en la "
         "versión actual; una clave que llegó a GitHub hay que cambiarla en "
         "su servicio, porque borrarla no la quita del historial."),
        ("❓  ¿Por qué no se sube?",
         "eliges un archivo y te dice si se publica y, si no, qué regla "
         "exacta lo deja fuera: el .gitignore (y en qué línea), el cuadro de "
         "ignorar, tu lista global de git, otro repositorio dentro del "
         "proyecto…"),
        ("🤖  Auto-publicación",
         "vigila la carpeta y publica sola cuando algo cambia; si la "
         "revisión encuentra algo, o la carpeta publica en otro "
         "repositorio, se detiene y te avisa. Si cambias de carpeta, se "
         "pone en pausa: nunca pasa sola a vigilar la nueva."),
    )),
    ("SI EDITASTE EN OTRO SITIO O ALGO SALIÓ MAL", (
        ("⬇⬆  Traer cambios de GitHub",
         "lo contrario de publicar: baja a tu carpeta lo que hay en "
         "GitHub. Tu versión actual se guarda antes por si acaso, y si "
         "algo cambia en la carpeta mientras trabaja (o dentro hay otro "
         "repositorio que se perdería), se detiene sin tocar nada."),
        ("⏪  Versiones",
         "lista de todas tus publicaciones; puedes devolver la carpeta a "
         "cualquiera de ellas. Lo que tengas ahora no se pierde."),
    )),
    ("PARA EMPEZAR O LLEVARTE EL PROYECTO", (
        ("⬇  Clonar repositorio",
         "descarga a tu PC un repositorio que ya está en GitHub."),
        ("⬇⬇  Clonar perfil completo",
         "descarga de una vez todos los repositorios de un usuario; puedes "
         "elegir entre clonar con git (completo) o bajar instantáneas "
         ".tar.gz (mucho más rápido, pero sin historial), y si repites la "
         "descarga, omitir lo que ya tienes o ponerlo al día trayendo solo "
         "lo que cambió."),
        ("➕  Crear repositorio nuevo",
         "lo crea en GitHub con tu token y rellena la URL por ti."),
        ("📄  Consolidar a TXT",
         "junta todo el proyecto (estructura y contenido) en un único "
         "archivo de texto, sin límite de tamaño: cada archivo de texto "
         "entra entero, mida lo que mida, y los binarios se mencionan con "
         "su tamaño. Muestra el progreso y puedes detenerlo sin perder el "
         "TXT anterior; «Abrir ubicación» te lleva a la carpeta donde "
         "quedó, con el archivo ya seleccionado. Para pasárselo a una IA: "
         "puede llevar solo lo que cambió desde la última publicación "
         "(enteros o solo sus diferencias), dejar fuera lo que no aporta "
         "(bloqueos de dependencias, minificados, código de terceros), "
         "tachar los secretos como «••••••••» (viene marcado), decirte "
         "cuántos tokens ocupa más o menos, repartirse en partes que "
         "quepan en la conversación y copiarse al portapapeles con «📋» "
         "(parte a parte)."),
        ("📥  Aplicar TXT",
         "el camino de vuelta: pegas lo que te devolvió una IA (los "
         "archivos en el mismo formato «MODULO: ruta») y te enseña, archivo "
         "a archivo, qué cambiaría, con las líneas que entran y salen. Solo "
         "cambia las líneas que cambiaron: el resto del archivo queda byte "
         "a byte igual (finales de línea, codificación). Nunca escribe fuera "
         "de la carpeta, nunca borra ni vacía nada, guarda antes una copia "
         "exacta y no marca lo que parece un resumen de la IA («… resto "
         "igual …»)."),
        ("🗂  Issues y PR a TXT",
         "todo lo que se habló en un repositorio de GitHub en un solo "
         "archivo de texto: cada issue y cada pull request (abiertos y "
         "cerrados) con quién lo abrió, cada comentario y respuesta con su "
         "autor y su hora, las revisiones, los comentarios en el código con "
         "su trozo de código, los commits, quién cerró o fusionó y cuándo, "
         "las etiquetas y asignaciones… en orden. Con tu token va mucho "
         "más rápido: todas las partes a la vez, por varias conexiones "
         "(tantas como aguante GitHub), y las revisiones, los commits y "
         "las menciones por lotes (un repositorio con miles de pull "
         "requests, en minutos). Si GitHub pide esperar, espera y sigue; "
         "si los lotes no se pueden usar (y todo va más lento), lo dice y "
         "por qué; y si lo detienes, la próxima vez continúa donde se "
         "quedó. Puede tachar los secretos que alguien pegó en un issue."),
        ("📊  Analizar proyecto",
         "de qué está hecho tu proyecto: lenguajes y líneas de código, "
         "comentario y en blanco; cuánto es tuyo y cuánto generado o de "
         "terceros; archivos duplicados, los más grandes y los TODO "
         "pendientes; y, si es un repositorio, quién ha trabajado, cuándo "
         "y qué archivos se tocan más. Lo guarda como informe HTML con "
         "gráficos que no se conecta a ningún sitio."),
    )),
    ("OTRAS HERRAMIENTAS", (
        ("🌍  Publicar como web",
         "activa GitHub Pages: tu proyecto queda como una página con "
         "enlace propio."),
        ("🏷  Publicar versión",
         "marca un punto del proyecto (v1.0.0) y crea una descarga en "
         "GitHub."),
        ("🌿  Ramas",
         "elige a qué rama de GitHub quieres publicar."),
        ("🌐  Abrir repositorio",
         "abre la página del repositorio en tu navegador."),
        ("🩺  Registro de diagnóstico",
         "abre la carpeta donde se guarda lo que pasa por esta consola "
         "(sin tokens ni contraseñas), para entender un fallo aunque ya "
         "hayas cerrado la app."),
        ("⚖️  Licencia  (en el formulario)",
         "añade un archivo LICENSE al publicar, si el proyecto no tiene "
         "ninguno; viene Apache 2.0 elegida y nunca toca la que ya exista."),
        ("🔑  Conectarme con mi token  (en Opciones)",
         "usa el token que guardaste para hablar con GitHub, así no te "
         "pregunta la cuenta cada vez que publicas."),
    )),
)

# Atajos de teclado: (secuencia de Tk, método, tecla para la ayuda,
# qué hace). Es la única definición: la ventana los conecta desde aquí
# y la ayuda los lista desde aquí (los que no tienen texto no se listan).
SHORTCUTS = (
    ("<Control-Return>", "start_publish", "Ctrl+Enter", "publicar"),
    ("<Control-KP_Enter>", "start_publish", "", ""),
    ("<F5>", "start_scan", "F5", "analizar (revisión de seguridad)"),
    ("<Control-s>", "save_profile", "Ctrl+S", "guardar el perfil"),
    ("<Control-t>", "open_consolidate", "Ctrl+T", "consolidar a TXT"),
    ("<Control-r>", "open_apply_txt", "Ctrl+R", "aplicar un TXT"),
    ("<F1>", "show_help", "F1", "esta ayuda"),
)

DESKTOP = (
    ("⌨  Atajos",
     " · ".join(f"{key}: {what}" for _seq, _m, key, what in SHORTCUTS
                if key)),
    ("🖱  Arrastrar y soltar  (Windows)",
     "suelta una carpeta en la ventana y pasa a ser la del proyecto; "
     "suelta un TXT y se abre «Aplicar TXT» con él."),
    ("🪟  Bandeja e inicio  (Windows)",
     "con la auto-publicación en marcha aparece un icono junto al reloj "
     "que avisa de cada publicación; al minimizar, la ventana se guarda "
     "ahí. En la tarjeta de auto-publicación puedes hacer que la app se "
     "abra al iniciar Windows vigilando tu última carpeta."),
    ("🆕  Versiones nuevas",
     "si en Opciones escribes el repositorio de la app (usuario/"
     "repositorio), una vez al día mira si hay una versión más nueva y te "
     "avisa aquí. Sin repositorio no pregunta a nadie."),
)

GUIDE = GUIDE + (("ESCRITORIO", DESKTOP),)

FOOTER = ("Puedes seleccionar este texto y copiarlo con Ctrl+C, o usar el "
          "botón «📋 Copiar». Para volver a leer esto, pulsa «❔ Ayuda».")


def guide_parts():
    """Cada línea como lista de tramos [(texto, etiqueta), …], lista para
    la consola: el nombre del botón resaltado y la explicación en gris."""
    lines = [[("¿Qué hace cada botón?", "ok")]]
    for section, entries in GUIDE:
        lines.append([("", "dim")])
        lines.append([(section, "cmd")])
        for name, what in entries:
            lines.append([("  " + name + "  ", "out"), ("· " + what, "dim")])
    lines.append([("", "dim")])
    lines.append([(FOOTER, "dim")])
    return lines
