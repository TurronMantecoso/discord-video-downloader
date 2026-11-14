# Descargar videos desde la consola

El programa consulta todo el historial de un canal de texto o hilo, muestra
cuántos videos adjuntos encontró y cuánto pesan, y pide confirmación antes
de solicitar una carpeta y descargar. No incluye enlaces externos ni los
hilos que cuelgan del canal indicado.

## Preparación

Con Python 3.8 o superior, desde la carpeta de este proyecto:

```sh
python -m venv .venv
```

Activa el entorno:

- Windows (PowerShell): `.venv\Scripts\Activate.ps1`
- Linux: `source .venv/bin/activate`

Instala las dependencias:

```sh
python -m pip install -r requirements.txt
```

Agrega el bot al servidor con permisos para ver el canal y leer el historial.
En Discord Developer Portal, habilita **Message Content Intent** para el bot;
sin ese acceso Discord puede ocultar los adjuntos de los mensajes.

Copia `.env.example` como `.env` junto a `ejemplo.py` y configura tu token:

```dotenv
DISCORD_TOKEN=token_del_bot
```

No compartas ese archivo ni lo subas al repositorio. También puedes definir
`DISCORD_TOKEN` como variable de entorno. Usa el token del bot.

## Uso

```sh
python ejemplo.py
```

1. Ingresa el ID del canal. Puedes copiarlo desde Discord con el modo
   desarrollador habilitado.
2. Espera el conteo y la suma de tamaños; responde `s` o `n`.
3. Ingresa la carpeta del disco, por ejemplo `E:\Videos` o
   `/media/usuario/Disco/Videos`. Puedes escribir `cancelar`.
4. Los videos quedarán dentro de `<carpeta>/<id_canal>/`.

El progreso muestra el porcentaje por bytes y los videos completados sobre
los pendientes de esta ejecución. Los archivos ya presentes con el mismo
nombre y tamaño se omiten. Cada nombre incluye el ID del adjunto para evitar
colisiones; algunos caracteres se sustituyen para permitir su uso en Windows
y Linux.

## Videos duplicados

Durante la descarga se calcula la huella **SHA-256** por bloques. Si coincide
con la de un archivo completo de la misma carpeta del canal, se conserva la
copia existente y se elimina únicamente el archivo temporal recién descargado.
Esto funciona aunque los videos tengan nombres distintos o estén en mensajes
distintos. Tener el mismo tamaño por sí solo no los convierte en duplicados.

El resumen distingue **descargados**, **omitidos**, **duplicados** y **fallidos**.
El porcentaje incluye los bytes transferidos de los duplicados: la primera
vez hay que descargarlos para conocer su huella. El total y el peso mostrados
antes de descargar incluyen todos los adjuntos; todavía no se conocen los
duplicados nuevos.

El archivo `.huellas.json`, dentro de la carpeta del canal, guarda la relación
entre cada adjunto y la copia conservada. En las siguientes ejecuciones, los
duplicados ya reconocidos se cuentan como omitidos y no se descargan otra vez.
Se vuelven a comprobar las huellas de los archivos locales antes de empezar;
esto puede tardar si hay muchos videos. Si la copia se borró o modificó,
sus referencias anteriores dejan de omitirse. Si se pierde el registro, se
reconstruye desde los archivos, pero habrá que reconocer de nuevo los adjuntos
duplicados que no tengan su propio archivo.

La comparación abarca la carpeta del canal elegida. No elimina duplicados
que ya estuvieran guardados antes de esta actualización. Videos recortados,
recomprimidos o con metadatos distintos tienen huellas distintas.

`Ctrl+C` detiene el programa conservando los videos completos. Los archivos
`.part` se reinician al repetir la descarga. Si falla un video, se continúa
con los demás; si falla el disco, se detiene la tarea. Una descarga con
errores no se anuncia como completada correctamente.

El disco debe estar conectado durante toda la tarea. No hace falta tener
abierta la aplicación de Discord. La aplicación termina después del resumen.

## Pruebas sin conexión

```sh
python -m unittest discover -s tests -p test_ejemplo.py -v
```
