"""Descarga interactiva de videos adjuntos de Discord a un disco local."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

import aiohttp
import discord
from dotenv import load_dotenv

EXTENSIONES_VIDEO = {'.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v'}
TAMANO_BLOQUE = 256 * 1024


@dataclass
class Video:
    mensaje_id: int
    adjunto_id: int
    nombre: str
    tamano: int
    url: str

    @property
    def nombre_archivo(self):
        # ASCII también mantiene los nombres dentro de los límites de Windows y Linux.
        nombre = re.sub(r'[^a-zA-Z0-9._ -]', '_', self.nombre).strip(' .')
        extension = Path(nombre).suffix[:10]
        base = Path(nombre).stem[:110].rstrip(' .') or 'video'
        return f'{self.adjunto_id}_{base}{extension}'


@dataclass
class Resultado:
    descargados: int = 0
    omitidos: int = 0
    fallidos: int = 0
    duplicados: int = 0
    bytes_descargados: int = 0


def huella_archivo(archivo):
    huella = hashlib.sha256()
    with archivo.open('rb') as entrada:
        for bloque in iter(lambda: entrada.read(TAMANO_BLOQUE), b''):
            huella.update(bloque)
    return huella.hexdigest()


class RegistroHuellas:
    """Índice por carpeta: adjuntos de Discord y la copia local que conservamos."""

    def __init__(self, destino):
        self.destino = destino
        self.ruta = destino / '.huellas.json'
        self.adjuntos = {}
        self.por_huella = {}
        anteriores = {}
        if self.ruta.is_symlink():
            raise ValueError('El registro de huellas no puede ser un enlace simbólico.')
        if self.ruta.exists():
            try:
                datos = json.loads(self.ruta.read_text(encoding='utf-8'))
                if not isinstance(datos, dict) or datos.get('version') != 1 or not isinstance(datos.get('adjuntos'), dict):
                    raise ValueError('Formato de registro desconocido.')
                anteriores = datos['adjuntos']
            except (ValueError, UnicodeError):
                print('Registro de huellas inválido; se reconstruirá desde los archivos completos.')

        # Recalcular permite incorporar descargas anteriores y descartar referencias
        # a copias borradas o modificadas, sin confiar solamente en su tamaño.
        print('Comprobando las huellas de los videos guardados...')
        archivos = {}
        for archivo in sorted(destino.iterdir()):
            if not re.match(r'^\d+_', archivo.name) or archivo.name.endswith('.part'):
                continue
            if archivo.is_symlink() or not archivo.is_file():
                continue
            tamano = archivo.stat().st_size
            huella = huella_archivo(archivo)
            archivos[archivo.name] = (huella, tamano)
            self.por_huella.setdefault((huella, tamano), archivo.name)
            identificador = archivo.name.split('_', 1)[0]
            self.adjuntos[identificador] = {'archivo': archivo.name, 'sha256': huella, 'tamano': tamano}

        for identificador, registro in anteriores.items():
            if not isinstance(registro, dict):
                continue
            nombre = registro.get('archivo')
            if not isinstance(nombre, str):
                continue
            if nombre in archivos and archivos[nombre] == (registro.get('sha256'), registro.get('tamano')):
                self.adjuntos.setdefault(identificador, registro)

    def contiene(self, video):
        registro = self.adjuntos.get(str(video.adjunto_id))
        return registro is not None and registro['tamano'] == video.tamano

    def copia_identica(self, huella, tamano):
        nombre = self.por_huella.get((huella, tamano))
        if nombre is None:
            return None
        archivo = self.destino / nombre
        if archivo.is_symlink() or not archivo.is_file() or archivo.stat().st_size != tamano:
            return None
        if huella_archivo(archivo) != huella:
            return None
        return nombre

    def registrar(self, video, huella, nombre):
        self.adjuntos[str(video.adjunto_id)] = {'archivo': nombre, 'sha256': huella, 'tamano': video.tamano}
        self.por_huella[(huella, video.tamano)] = nombre

    def guardar(self):
        temporal = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.destino,
                                             prefix='.huellas-', suffix='.tmp', delete=False) as salida:
                temporal = Path(salida.name)
                json.dump({'version': 1, 'adjuntos': self.adjuntos}, salida, ensure_ascii=True, indent=2)
            temporal.replace(self.ruta)
        finally:
            if temporal is not None and temporal.exists():
                temporal.unlink()


def es_video(adjunto):
    if adjunto.content_type:
        return adjunto.content_type.lower().startswith('video/')
    return Path(adjunto.filename).suffix.lower() in EXTENSIONES_VIDEO


def peso(tamano):
    if tamano >= 1024**3:
        return f'{tamano / 1024**3:.2f} GiB'
    return f'{tamano / 1024**2:.2f} MiB'


def pedir_id():
    while True:
        valor = input('ID del canal (o cancelar): ').strip()
        if valor.lower() == 'cancelar':
            return None
        if valor.isascii() and valor.isdecimal() and len(valor) <= 20 and 0 < int(valor) < 2**64:
            return int(valor)
        print('ID inválido. Ingresa el ID numérico del canal.')


def confirmar():
    while True:
        respuesta = input('¿Continuar con la descarga? [s/n]: ').strip().lower()
        if respuesta in ('s', 'si', 'sí'):
            return True
        if respuesta in ('n', 'no', 'cancelar'):
            return False
        print('Responde s o n.')


async def analizar(canal):
    videos: List[Video] = []
    revisados = 0
    print('Revisando el historial completo...')
    async for mensaje in canal.history(limit=None):
        revisados += 1
        for adjunto in mensaje.attachments:
            if es_video(adjunto):
                videos.append(Video(mensaje.id, adjunto.id, adjunto.filename, adjunto.size, adjunto.url))
        if revisados % 100 == 0:
            print(f'\rMensajes revisados: {revisados} | Videos encontrados: {len(videos)}', end='', flush=True)
    print(f'\rMensajes revisados: {revisados} | Videos encontrados: {len(videos)}')
    print(f'Peso total: {peso(sum(video.tamano for video in videos))}')
    return videos


def preparar_destino(ruta, canal_id, videos):
    destino = Path(ruta).expanduser().resolve() / str(canal_id)
    destino.mkdir(parents=True, exist_ok=True)
    registro = RegistroHuellas(destino)
    pendientes = []
    for video in videos:
        archivo = destino / video.nombre_archivo
        parcial = destino / (video.nombre_archivo + '.part')
        if archivo.is_symlink() or parcial.is_symlink():
            raise ValueError(f'El destino contiene un enlace simbólico para {video.nombre_archivo}.')
        if archivo.exists():
            if not archivo.is_file() or archivo.stat().st_size != video.tamano:
                raise ValueError(f'Ya existe {video.nombre_archivo} con otro tamaño. Usa otra carpeta o retíralo.')
        elif not registro.contiene(video):
            pendientes.append(video)
    with tempfile.TemporaryFile(dir=destino) as prueba:
        prueba.write(b'prueba')
        prueba.flush()
    necesarios = sum(video.tamano for video in pendientes)
    libres = shutil.disk_usage(destino).free
    if necesarios > libres:
        raise ValueError(f'Espacio insuficiente: se necesitan {peso(necesarios)} y hay {peso(libres)} libres.')
    registro.guardar()
    return destino, pendientes, registro


def pedir_destino(canal_id, videos):
    while True:
        ruta = input('Carpeta de destino (o cancelar): ').strip().strip('"')
        if ruta.lower() == 'cancelar':
            return None
        if not ruta:
            print('Ingresa una ruta de carpeta.')
            continue
        try:
            return preparar_destino(ruta, canal_id, videos)
        except (OSError, ValueError, RuntimeError) as error:
            print(f'No se puede usar esa carpeta: {error}')


class Progreso:
    def __init__(self, total_bytes, total_videos, resultado):
        self.total_bytes = total_bytes
        self.total_videos = total_videos
        self.resultado = resultado
        self.ultima_actualizacion = 0.0

    def mostrar(self, video, actuales, forzar=False):
        ahora = time.monotonic()
        if not forzar and ahora - self.ultima_actualizacion < 0.2:
            return
        self.ultima_actualizacion = ahora
        completos = self.resultado.bytes_descargados
        if self.total_bytes:
            porcentaje = min(100, 100 * (completos + actuales) / self.total_bytes)
        else:
            porcentaje = 100 if self.resultado.descargados + self.resultado.duplicados == self.total_videos else 0
        nombre = video.nombre_archivo[:55]
        print(
            f'\r{porcentaje:6.2f}% | Descargados: {self.resultado.descargados}/{self.total_videos}'
            f' | Duplicados: {self.resultado.duplicados}'
            f' | Actual: {nombre:<55}',
            end='', flush=True,
        )


async def descargar_video(session, canal, video, destino, progreso, registro):
    archivo = destino / video.nombre_archivo
    parcial = destino / (video.nombre_archivo + '.part')
    url = video.url
    actualizado = False
    for intento in range(3):
        recibidos = 0
        huella = hashlib.sha256()
        try:
            # La sesión del CDN no lleva el token del bot.
            async with session.get(url) as respuesta:
                if respuesta.status == 403 and not actualizado:
                    actualizado = True
                    mensaje = await canal.fetch_message(video.mensaje_id)
                    nuevo = next((a for a in mensaje.attachments if a.id == video.adjunto_id), None)
                    if nuevo is None:
                        raise ValueError('El adjunto ya no existe.')
                    url = nuevo.url
                    continue
                respuesta.raise_for_status()
                with parcial.open('wb') as salida:
                    async for bloque in respuesta.content.iter_chunked(TAMANO_BLOQUE):
                        recibidos += len(bloque)
                        if recibidos > video.tamano:
                            raise ValueError('El contenido recibido supera el tamaño esperado.')
                        salida.write(bloque)
                        huella.update(bloque)
                        progreso.mostrar(video, recibidos)
            if recibidos != video.tamano:
                raise aiohttp.ClientPayloadError('El archivo recibido está incompleto.')
            codigo = huella.hexdigest()
            copia = registro.copia_identica(codigo, video.tamano)
            if copia is not None:
                registro.registrar(video, codigo, copia)
                registro.guardar()
                parcial.unlink()
                return True
            parcial.replace(archivo)
            registro.registrar(video, codigo, archivo.name)
            registro.guardar()
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException) as error:
            error_http = isinstance(error, (aiohttp.ClientResponseError, discord.HTTPException))
            transitorio = not error_http or error.status in (408, 429) or error.status >= 500
            if not transitorio or intento == 2:
                raise
            progreso.mostrar(video, 0, forzar=True)
            print(f'\nReintentando {video.nombre_archivo} ({intento + 2}/3)...')
            await asyncio.sleep(2**intento)
    raise ValueError('No se pudo obtener el adjunto actualizado.')


def mostrar_resumen(resultado, total, destino, estado):
    print(
        f'\n{estado}\nDescargados: {resultado.descargados} | Omitidos: {resultado.omitidos}'
        f' | Duplicados: {resultado.duplicados} | Fallidos: {resultado.fallidos}'
        f' | Sin procesar: {total - resultado.descargados - resultado.omitidos - resultado.duplicados - resultado.fallidos}'
        f'\nCarpeta: {destino}'
    )


async def descargar(canal, destino, pendientes, omitidos, registro=None):
    resultado = Resultado(omitidos=omitidos)
    total = len(pendientes) + omitidos
    progreso = Progreso(sum(v.tamano for v in pendientes), len(pendientes), resultado)
    print(f'Omitidos: {omitidos} | Pendientes: {len(pendientes)} | Por descargar: {peso(progreso.total_bytes)}')
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
    try:
        if registro is None:
            registro = RegistroHuellas(destino)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            for video in pendientes:
                progreso.mostrar(video, 0, forzar=True)
                try:
                    duplicado = await descargar_video(session, canal, video, destino, progreso, registro)
                except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException, ValueError) as error:
                    resultado.fallidos += 1
                    progreso.mostrar(video, 0, forzar=True)
                    detalle = f'HTTP {error.status}' if isinstance(error, (aiohttp.ClientResponseError, discord.HTTPException)) else type(error).__name__
                    print(f'\nFalló: {video.nombre_archivo} ({detalle}).')
                    continue
                if duplicado:
                    resultado.duplicados += 1
                else:
                    resultado.descargados += 1
                resultado.bytes_descargados += video.tamano
                progreso.mostrar(video, 0, forzar=True)
                if duplicado:
                    print(f'\nDuplicado: {video.nombre_archivo}. Se conserva la copia existente.')
                else:
                    print(f'\nCompletado: {video.nombre_archivo}')
    except OSError as error:
        resultado.fallidos += 1
        mostrar_resumen(resultado, total, destino, f'Descarga detenida por un error del disco: {error}')
        return resultado
    except asyncio.CancelledError:
        mostrar_resumen(resultado, total, destino, 'Descarga cancelada. Se conservan los archivos completos.')
        raise
    estado = 'Descarga terminada con errores.' if resultado.fallidos else 'Descarga completada.'
    mostrar_resumen(resultado, total, destino, estado)
    return resultado


async def ejecutar(token):
    intents = discord.Intents.default()
    intents.message_content = True
    async with discord.Client(intents=intents) as cliente:
        # Solo REST: no abre un websocket ni necesita mantener Discord abierto.
        await cliente.login(token)
        canal_id = pedir_id()
        if canal_id is None:
            print('Operación cancelada.')
            return
        try:
            canal = await cliente.fetch_channel(canal_id)
        except (discord.NotFound, discord.Forbidden):
            print('El canal no existe o el bot no tiene acceso.')
            return
        if not isinstance(canal, (discord.TextChannel, discord.Thread)):
            print('El ID debe corresponder a un canal de texto o hilo.')
            return
        try:
            videos = await analizar(canal)
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError):
            print('\nAnálisis incompleto: no se pudo leer todo el historial. No se descargarán archivos.')
            return
        if not videos:
            print('No hay videos para descargar. Comprueba también que el bot tenga acceso al historial y Message Content Intent habilitado.')
            return
        if not confirmar():
            print('Operación cancelada.')
            return
        seleccion = pedir_destino(canal_id, videos)
        if seleccion is None:
            print('Operación cancelada.')
            return
        destino, pendientes, registro = seleccion
        await descargar(canal, destino, pendientes, len(videos) - len(pendientes), registro)


def main():
    load_dotenv(Path(__file__).with_name('.env'))
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token:
        print('Configura DISCORD_TOKEN en el entorno o en el archivo .env junto a ejemplo.py.')
        return
    try:
        asyncio.run(ejecutar(token))
    except (KeyboardInterrupt, EOFError):
        print('\nOperación cancelada.')
    except discord.LoginFailure:
        print('No se pudo autenticar el bot. Revisa DISCORD_TOKEN.')
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError):
        print('No se pudo completar la conexión con Discord. Comprueba el acceso y vuelve a intentarlo.')


if __name__ == '__main__':
    main()
