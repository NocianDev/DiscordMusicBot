import asyncio
import os
from collections import deque
from dataclasses import dataclass

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


# =========================================================
# CONFIGURACIÓN
# =========================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "No se encontró DISCORD_TOKEN. "
        "Revisa que exista el archivo .env y que tenga el token."
    )


FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn",
}


YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "noplaylist": True,
    "skip_download": True,
}


ytdlp = yt_dlp.YoutubeDL(YTDLP_OPTIONS)


# =========================================================
# MODELOS Y ESTADO
# =========================================================

@dataclass
class Song:
    title: str
    webpage_url: str
    requested_by: str


class MusicState:
    def __init__(self):
        self.queue: deque[Song] = deque()
        self.current_song: Song | None = None
        self.text_channel = None
        self.lock = asyncio.Lock()


music_states: dict[int, MusicState] = {}


def get_music_state(guild_id: int) -> MusicState:
    if guild_id not in music_states:
        music_states[guild_id] = MusicState()

    return music_states[guild_id]


# =========================================================
# BOT
# =========================================================

class NYTMusicBot(commands.Bot):
    async def setup_hook(self):
        self.tree.add_command(MusicCommands())

        synced_commands = await self.tree.sync()

        print(f"Comandos sincronizados: {len(synced_commands)}")


intents = discord.Intents.default()

bot = NYTMusicBot(
    command_prefix="!",
    intents=intents,
)


# =========================================================
# FUNCIONES DE YOUTUBE
# =========================================================

async def extract_song(query: str, requested_by: str) -> Song:
    loop = asyncio.get_running_loop()

    def extract():
        search_query = query

        if not query.startswith(("http://", "https://")):
            search_query = f"ytsearch1:{query}"

        return ytdlp.extract_info(
            search_query,
            download=False,
        )

    data = await loop.run_in_executor(None, extract)

    if not data:
        raise ValueError("YouTube no devolvió información.")

    if "entries" in data:
        entries = [
            entry
            for entry in data.get("entries", [])
            if entry
        ]

        if not entries:
            raise ValueError("No se encontraron resultados.")

        data = entries[0]

    title = data.get("title", "Canción sin título")

    webpage_url = (
        data.get("webpage_url")
        or data.get("original_url")
        or query
    )

    return Song(
        title=title,
        webpage_url=webpage_url,
        requested_by=requested_by,
    )


async def get_stream_url(song: Song) -> str:
    loop = asyncio.get_running_loop()

    def extract():
        return ytdlp.extract_info(
            song.webpage_url,
            download=False,
        )

    data = await loop.run_in_executor(None, extract)

    if not data:
        raise ValueError("No se pudo obtener información del audio.")

    stream_url = data.get("url")

    if not stream_url:
        formats = data.get("formats", [])

        audio_formats = [
            item
            for item in formats
            if item.get("url")
            and item.get("acodec") != "none"
        ]

        if not audio_formats:
            raise ValueError("No se encontró un formato de audio.")

        stream_url = audio_formats[-1]["url"]

    return stream_url


# =========================================================
# CONEXIÓN DE VOZ
# =========================================================

async def ensure_voice_connection(
    interaction: discord.Interaction,
) -> discord.VoiceClient | None:

    if not interaction.guild:
        await interaction.followup.send(
            "Este comando solamente funciona dentro de un servidor.",
            ephemeral=True,
        )
        return None

    member = interaction.user

    if not isinstance(member, discord.Member):
        await interaction.followup.send(
            "No pude identificar tu usuario dentro del servidor.",
            ephemeral=True,
        )
        return None

    if not member.voice or not member.voice.channel:
        await interaction.followup.send(
            "Primero entra a un canal de voz.",
            ephemeral=True,
        )
        return None

    target_channel = member.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel != target_channel:
            await voice_client.move_to(target_channel)

        return voice_client

    try:
        return await target_channel.connect()
    except discord.ClientException:
        await interaction.followup.send(
            "El bot ya está conectado a otro canal de voz.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "El bot no tiene permiso para conectarse o hablar.",
            ephemeral=True,
        )
    except Exception as error:
        print(f"Error conectando al canal: {error}")

        await interaction.followup.send(
            "Ocurrió un error al entrar al canal de voz.",
            ephemeral=True,
        )

    return None


# =========================================================
# REPRODUCCIÓN Y COLA
# =========================================================

async def play_next(guild: discord.Guild):
    state = get_music_state(guild.id)
    voice_client = guild.voice_client

    if not voice_client or not voice_client.is_connected():
        state.current_song = None
        return

    async with state.lock:
        if voice_client.is_playing() or voice_client.is_paused():
            return

        if not state.queue:
            state.current_song = None
            return

        song = state.queue.popleft()
        state.current_song = song

        try:
            stream_url = await get_stream_url(song)
        except Exception as error:
            print(f"Error obteniendo audio: {error}")

            if state.text_channel:
                await state.text_channel.send(
                    f"❌ No pude reproducir **{song.title}**."
                )

            state.current_song = None
            asyncio.create_task(play_next(guild))
            return

        try:
            audio_source = discord.FFmpegPCMAudio(
                stream_url,
                executable="ffmpeg",
                **FFMPEG_OPTIONS,
            )
        except Exception as error:
            print(f"Error iniciando FFmpeg: {error}")

            if state.text_channel:
                await state.text_channel.send(
                    "❌ No pude iniciar FFmpeg."
                )

            state.current_song = None
            return

        def after_playing(error):
            if error:
                print(f"Error durante la reproducción: {error}")

            future = asyncio.run_coroutine_threadsafe(
                play_next(guild),
                bot.loop,
            )

            try:
                future.result()
            except Exception as callback_error:
                print(
                    "Error intentando reproducir "
                    f"la siguiente canción: {callback_error}"
                )

        voice_client.play(
            audio_source,
            after=after_playing,
        )

    if state.text_channel:
        await state.text_channel.send(
            f"🎵 Reproduciendo: **{song.title}**\n"
            f"Solicitada por: **{song.requested_by}**"
        )


# =========================================================
# COMANDOS
# =========================================================

class MusicCommands(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="music",
            description="Comandos de NYT MusicBot",
        )

    @app_commands.command(
        name="play",
        description="Busca y reproduce una canción de YouTube",
    )
    @app_commands.describe(
        busqueda="Nombre de la canción o enlace de YouTube",
    )
    async def play(
        self,
        interaction: discord.Interaction,
        busqueda: str,
    ):
        await interaction.response.defer()

        voice_client = await ensure_voice_connection(interaction)

        if not voice_client or not interaction.guild:
            return

        state = get_music_state(interaction.guild.id)
        state.text_channel = interaction.channel

        try:
            song = await extract_song(
                busqueda,
                interaction.user.display_name,
            )
        except Exception as error:
            print(f"Error buscando canción: {error}")

            await interaction.followup.send(
                "❌ No pude encontrar esa canción."
            )
            return

        state.queue.append(song)

        if voice_client.is_playing() or voice_client.is_paused():
            await interaction.followup.send(
                f"✅ Agregada a la cola: **{song.title}**\n"
                f"Posición: **{len(state.queue)}**"
            )
            return

        await interaction.followup.send(
            f"🔎 Encontré: **{song.title}**"
        )

        await play_next(interaction.guild)

    @app_commands.command(
        name="pause",
        description="Pausa la canción actual",
    )
    async def pause(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Este comando solamente funciona en servidores.",
                ephemeral=True,
            )
            return

        voice_client = interaction.guild.voice_client

        if not voice_client or not voice_client.is_playing():
            await interaction.response.send_message(
                "No hay música reproduciéndose.",
                ephemeral=True,
            )
            return

        voice_client.pause()

        await interaction.response.send_message(
            "⏸️ Música pausada."
        )

    @app_commands.command(
        name="resume",
        description="Continúa la canción pausada",
    )
    async def resume(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Este comando solamente funciona en servidores.",
                ephemeral=True,
            )
            return

        voice_client = interaction.guild.voice_client

        if not voice_client or not voice_client.is_paused():
            await interaction.response.send_message(
                "No hay ninguna canción pausada.",
                ephemeral=True,
            )
            return

        voice_client.resume()

        await interaction.response.send_message(
            "▶️ Reproducción reanudada."
        )

    @app_commands.command(
        name="skip",
        description="Salta la canción actual",
    )
    async def skip(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Este comando solamente funciona en servidores.",
                ephemeral=True,
            )
            return

        voice_client = interaction.guild.voice_client

        if not voice_client or not (
            voice_client.is_playing()
            or voice_client.is_paused()
        ):
            await interaction.response.send_message(
                "No hay ninguna canción para saltar.",
                ephemeral=True,
            )
            return

        voice_client.stop()

        await interaction.response.send_message(
            "⏭️ Canción saltada."
        )

    @app_commands.command(
        name="queue",
        description="Muestra la cola de reproducción",
    )
    async def queue(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Este comando solamente funciona en servidores.",
                ephemeral=True,
            )
            return

        state = get_music_state(interaction.guild.id)

        lines = []

        if state.current_song:
            lines.append(
                f"🎵 **Reproduciendo:** "
                f"{state.current_song.title}"
            )
        else:
            lines.append("🎵 No hay una canción reproduciéndose.")

        if state.queue:
            lines.append("\n📋 **Próximas canciones:**")

            for index, song in enumerate(
                list(state.queue)[:10],
                start=1,
            ):
                lines.append(
                    f"{index}. {song.title} "
                    f"— {song.requested_by}"
                )
        else:
            lines.append("\nLa cola está vacía.")

        await interaction.response.send_message(
            "\n".join(lines)
        )

    @app_commands.command(
        name="nowplaying",
        description="Muestra la canción actual",
    )
    async def nowplaying(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Este comando solamente funciona en servidores.",
                ephemeral=True,
            )
            return

        state = get_music_state(interaction.guild.id)

        if not state.current_song:
            await interaction.response.send_message(
                "No hay ninguna canción reproduciéndose.",
                ephemeral=True,
            )
            return

        song = state.current_song

        await interaction.response.send_message(
            f"🎵 **Ahora suena:** {song.title}\n"
            f"Solicitada por: **{song.requested_by}**\n"
            f"{song.webpage_url}"
        )

    @app_commands.command(
        name="stop",
        description="Detiene la música y limpia la cola",
    )
    async def stop(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Este comando solamente funciona en servidores.",
                ephemeral=True,
            )
            return

        state = get_music_state(interaction.guild.id)

        state.queue.clear()
        state.current_song = None

        voice_client = interaction.guild.voice_client

        if voice_client:
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()

            await voice_client.disconnect()

        await interaction.response.send_message(
            "⏹️ Música detenida, cola eliminada "
            "y bot desconectado."
        )

    @app_commands.command(
        name="leave",
        description="Desconecta el bot del canal de voz",
    )
    async def leave(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Este comando solamente funciona en servidores.",
                ephemeral=True,
            )
            return

        voice_client = interaction.guild.voice_client

        if not voice_client:
            await interaction.response.send_message(
                "El bot no está conectado a un canal de voz.",
                ephemeral=True,
            )
            return

        state = get_music_state(interaction.guild.id)

        state.queue.clear()
        state.current_song = None

        await voice_client.disconnect()

        await interaction.response.send_message(
            "👋 NYT MusicBot salió del canal."
        )


# =========================================================
# EVENTOS
# =========================================================

@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="/music play",
        )
    )

    print("=" * 50)
    print(f"Bot conectado como: {bot.user}")
    print(f"ID del bot: {bot.user.id if bot.user else 'desconocido'}")
    print("NYT MusicBot está encendido.")
    print("=" * 50)


# =========================================================
# INICIAR BOT
# =========================================================

bot.run(TOKEN)