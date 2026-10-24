# -*- coding: utf-8 -*-
"""Qué hace cada botón, explicado para quien no usa la terminal.

Es la única definición del texto de ayuda: la consola lo imprime al
abrir la aplicación y cada vez que se pulsa «❔ Ayuda».
"""

GUIDE = (
    ("PARA PUBLICAR TU PROYECTO", (
        ("🚀  Publicar en GitHub",
         "sube tu carpeta al repositorio. Antes te enseña qué va a "
         "cambiar y puedes dejar archivos fuera uno a uno."),
        ("🚀🚀  Publicar seleccionados",
         "marca varios perfiles en la lista de la izquierda y los sube "
         "todos a la vez, cada uno a su repositorio."),
        ("🚀🚀  Publicar varias carpetas",
         "para subir de golpe proyectos que no tienes guardados como "
         "perfil: eliges las carpetas, le pones nombre al repositorio de "
         "cada una y se publican todas en tu cuenta. Puede crear en "
         "GitHub los repositorios que falten, y cada proyecto trae "
         "marcada la casilla para unirlo además en un único .txt, que se "
         "queda en tu equipo y no se sube."),
        ("🛡  Analizar",
         "revisa la carpeta en busca de archivos enormes y de "
         "contraseñas o claves, antes de que se suban."),
        ("🤖  Auto-publicación",
         "vigila la carpeta y publica sola cuando algo cambia; si la "
         "revisión encuentra algo, se detiene y te avisa."),
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
         "archivo de texto; «Abrir ubicación» te lleva a la carpeta donde "
         "quedó, con el archivo ya seleccionado."),
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
