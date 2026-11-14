"""Pruebas de consola y descargas, sin token ni acceso a la red."""

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import ejemplo


def adjunto(nombre='clip.MP4', tipo=None, identificador=10, tamano=4):
    return SimpleNamespace(filename=nombre, content_type=tipo, id=identificador, size=tamano, url='https://cdn.example/video')


def respuesta(bloques=(b'ab', b'cd'), estado=200):
    async def iterar(tamano):
        assert tamano == ejemplo.TAMANO_BLOQUE
        for bloque in bloques:
            if isinstance(bloque, BaseException):
                raise bloque
            yield bloque

    mock = MagicMock()
    mock.status = estado
    mock.content.iter_chunked.side_effect = iterar
    if estado >= 400:
        mock.raise_for_status.side_effect = aiohttp.ClientResponseError(None, (), status=estado)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


class ConsolaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.destino = Path(self.temporal.name)
        self.video = ejemplo.Video(1, 10, 'clip.mp4', 4, 'https://cdn.example/video')
        self.canal = MagicMock(spec=discord.TextChannel)
        self.canal.id = 123
        self.consola = io.StringIO()
        self.salida = contextlib.redirect_stdout(self.consola)
        self.salida.__enter__()
        self.addCleanup(self.salida.__exit__, None, None, None)

    def test_deteccion(self):
        for nombre, tipo, esperado in (
            ('clip.MP4', None, True), ('clip.bin', 'video/mp4', True),
            ('clip.mp4', 'image/png', False), ('foto.png', None, False),
        ):
            with self.subTest(nombre=nombre, tipo=tipo):
                self.assertEqual(ejemplo.es_video(adjunto(nombre, tipo)), esperado)

    def test_entradas(self):
        with patch('builtins.input', side_effect=['-1', '9' * 5000, '１２', '123']):
            self.assertEqual(ejemplo.pedir_id(), 123)
        with patch('builtins.input', return_value='cancelar'):
            self.assertIsNone(ejemplo.pedir_id())
        with patch('builtins.input', side_effect=['quizás', 'SÍ']):
            self.assertTrue(ejemplo.confirmar())
        with patch('builtins.input', return_value='n'):
            self.assertFalse(ejemplo.confirmar())

    async def test_analisis_extenso(self):
        async def historial(**kwargs):
            self.assertEqual(kwargs, {'limit': None})
            for indice in range(205):
                yield SimpleNamespace(id=indice, attachments=[adjunto(), adjunto(identificador=11), adjunto('foto.png')])
        self.canal.history.side_effect = historial
        videos = await ejemplo.analizar(self.canal)
        self.assertEqual(len(videos), 410)
        self.assertEqual(sum(v.tamano for v in videos), 1640)
        self.assertIn('Mensajes revisados: 200', self.consola.getvalue())
        self.assertIn('Peso total:', self.consola.getvalue())

    def test_destino_y_omitidos(self):
        destino, pendientes = ejemplo.preparar_destino(self.destino, 123, [self.video])
        self.assertEqual(destino, self.destino / '123')
        self.assertEqual(pendientes, [self.video])
        (destino / self.video.nombre_archivo).write_bytes(b'abcd')
        self.assertEqual(ejemplo.preparar_destino(self.destino, 123, [self.video])[1], [])
        (destino / self.video.nombre_archivo).write_bytes(b'a')
        with self.assertRaisesRegex(ValueError, 'otro tamaño'):
            ejemplo.preparar_destino(self.destino, 123, [self.video])

    def test_espacio_insuficiente_y_ruta_invalida(self):
        with patch('ejemplo.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(ValueError, 'Espacio insuficiente'):
                ejemplo.preparar_destino(self.destino, 123, [self.video])
        with patch('builtins.input', side_effect=['', 'mala', 'cancelar']):
            with patch('ejemplo.preparar_destino', side_effect=PermissionError('Sin permiso')):
                self.assertIsNone(ejemplo.pedir_destino(123, [self.video]))

    def test_nombres_seguros(self):
        for nombre in ('../../video.mp4', 'C:\\carpeta\\video.mp4', 'CON.mp4', '\x1b[31mvideo.mp4', 'ñ' * 300 + '.mp4'):
            video = ejemplo.Video(1, 10, nombre, 4, '')
            archivo = video.nombre_archivo
            self.assertEqual(Path(archivo).name, archivo)
            self.assertNotIn('\\', archivo)
            self.assertLess(len(archivo), 150)
            self.assertTrue(archivo.startswith('10_'))

    async def descargar(self, respuestas, videos=None):
        session = MagicMock()
        session.get.side_effect = respuestas
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with patch('ejemplo.aiohttp.ClientSession', return_value=session):
            with patch('ejemplo.asyncio.sleep', new_callable=AsyncMock):
                resultado = await ejemplo.descargar(self.canal, self.destino, videos or [self.video], 0)
        return resultado, session

    async def test_descarga_completa_y_progreso(self):
        resultado, _ = await self.descargar([respuesta()])
        self.assertEqual(resultado.descargados, 1)
        self.assertEqual((self.destino / self.video.nombre_archivo).read_bytes(), b'abcd')
        self.assertFalse(list(self.destino.glob('*.part')))
        self.assertIn('100.00%', self.consola.getvalue())
        self.assertIn('Descargados: 1/1', self.consola.getvalue())

    async def test_reintenta_parcial_desde_cero(self):
        resultado, session = await self.descargar([respuesta([b'ab']), respuesta()])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(resultado.descargados, 1)
        self.assertEqual((self.destino / self.video.nombre_archivo).read_bytes(), b'abcd')

    async def test_refresca_url(self):
        nuevo = adjunto()
        nuevo.url = 'https://cdn.example/nueva'
        self.canal.fetch_message = AsyncMock(return_value=SimpleNamespace(attachments=[nuevo]))
        resultado, session = await self.descargar([respuesta(estado=403), respuesta()])
        self.assertEqual(resultado.descargados, 1)
        self.canal.fetch_message.assert_awaited_once_with(1)
        self.assertEqual(session.get.call_args.args[0], nuevo.url)

    async def test_fallo_individual_continua(self):
        otro = ejemplo.Video(2, 11, 'otro.mp4', 4, self.video.url)
        resultado, _ = await self.descargar([respuesta(estado=404), respuesta()], [self.video, otro])
        self.assertEqual((resultado.descargados, resultado.fallidos), (1, 1))
        self.assertIn('terminada con errores', self.consola.getvalue())
        self.assertNotIn('100.00%', self.consola.getvalue())

    async def test_agota_tres_intentos(self):
        resultado, session = await self.descargar([respuesta(estado=503) for _ in range(3)])
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(resultado.fallidos, 1)
        self.assertFalse((self.destino / self.video.nombre_archivo).exists())

    async def test_error_disco_detiene(self):
        with patch('ejemplo.descargar_video', side_effect=OSError('Disco desconectado')) as descargar:
            resultado, _ = await self.descargar([], [self.video, self.video])
        self.assertEqual(descargar.call_count, 1)
        self.assertEqual(resultado.fallidos, 1)
        self.assertIn('Sin procesar: 1', self.consola.getvalue())

    async def test_cancelacion_cierra_y_conserva_completos(self):
        otro = ejemplo.Video(2, 11, 'otro.mp4', 4, self.video.url)
        session = MagicMock()
        session.get.side_effect = [respuesta(), respuesta([b'ab', asyncio.CancelledError()])]
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with patch('ejemplo.aiohttp.ClientSession', return_value=session):
            with self.assertRaises(asyncio.CancelledError):
                await ejemplo.descargar(self.canal, self.destino, [self.video, otro], 0)
        session.__aexit__.assert_awaited_once()
        self.assertTrue((self.destino / self.video.nombre_archivo).exists())
        self.assertFalse((self.destino / otro.nombre_archivo).exists())
        self.assertIn('Descarga cancelada', self.consola.getvalue())

    async def ejecutar(self, videos, confirmacion=True, fallo=None):
        cliente = MagicMock()
        cliente.login = AsyncMock()
        cliente.fetch_channel = AsyncMock(return_value=self.canal)
        cliente.__aenter__ = AsyncMock(return_value=cliente)
        cliente.__aexit__ = AsyncMock(return_value=False)
        with contextlib.ExitStack() as pila:
            pila.enter_context(patch('ejemplo.discord.Client', return_value=cliente))
            pila.enter_context(patch('ejemplo.pedir_id', return_value=123))
            pila.enter_context(patch('ejemplo.analizar', AsyncMock(return_value=videos, side_effect=fallo)))
            confirmar = pila.enter_context(patch('ejemplo.confirmar', return_value=confirmacion))
            destino = pila.enter_context(patch('ejemplo.pedir_destino', return_value=(self.destino, videos)))
            descargar = pila.enter_context(patch('ejemplo.descargar', AsyncMock()))
            await ejemplo.ejecutar('token-simulado')
        cliente.__aexit__.assert_awaited_once()
        return confirmar, destino, descargar

    async def test_flujo_confirmado(self):
        confirmar, destino, descargar = await self.ejecutar([self.video])
        confirmar.assert_called_once()
        destino.assert_called_once_with(123, [self.video])
        descargar.assert_awaited_once_with(self.canal, self.destino, [self.video], 0)

    async def test_rechazo_no_pide_ruta(self):
        _, destino, descargar = await self.ejecutar([self.video], confirmacion=False)
        destino.assert_not_called()
        descargar.assert_not_awaited()

    async def test_sin_videos_no_confirma(self):
        confirmar, destino, _ = await self.ejecutar([])
        confirmar.assert_not_called()
        destino.assert_not_called()

    async def test_analisis_incompleto_no_descarga(self):
        error = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Sin permiso')
        confirmar, destino, descargar = await self.ejecutar([], fallo=error)
        confirmar.assert_not_called()
        destino.assert_not_called()
        descargar.assert_not_awaited()
        self.assertIn('Análisis incompleto', self.consola.getvalue())


if __name__ == '__main__':
    unittest.main()
