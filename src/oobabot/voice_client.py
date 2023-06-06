# -*- coding: utf-8 -*-
"""
A replacement voice client for discord.py, using the Discrivener audio transcriber.
"""

import asyncio
import typing

import discord
import discord.backoff
import discord.gateway
import discord.guild
import discord.opus
import discord.state
import discord.types
from discord.types import voice  # this is so pylint doesn't complain

from oobabot import audio_responder
from oobabot import discrivener
from oobabot import fancy_logger
from oobabot import ooba_client
from oobabot import prompt_generator
from oobabot import transcript


class VoiceClientError(Exception):
    """
    Base exception class for voice client errors.
    """


class VoiceClient(discord.VoiceProtocol):
    """
    A replacement voice client for discord.py, using the Discrivener audio transcriber.


    You do not create these, you typically get them from
    e.g. :meth:`VoiceChannel.connect`, by passing in
    cls=voice_client.VoiceClient
    """

    ooba_client: ooba_client.OobaClient
    prompt_generator: prompt_generator.PromptGenerator
    wakewords: typing.List[str] = []

    supported_modes: typing.Tuple[voice.SupportedModes, ...] = (
        "xsalsa20_poly1305",
        "xsalsa20_poly1305_suffix",
        "xsalsa20_poly1305_lite",
    )

    def __init__(
        self,
        client: discord.client.Client,
        channel: discord.abc.Connectable,
    ) -> None:
        super().__init__(client, channel)

        if channel.guild is None:
            raise ValueError("Channel does not have a guild.")

        self._discrivener = discrivener.Discrivener(self._handle_discrivener_output)
        self._discrivener_connected = False
        self._handshaking = False
        self._oobabot_voice_connected = False
        self._potentially_reconnecting = False
        self._voice_state_complete = asyncio.Event()
        self._voice_server_complete = asyncio.Event()
        self._state: discord.state.ConnectionState = client._connection
        self._session_id = discord.utils.MISSING
        self._server_id = discord.utils.MISSING
        self._transcript = transcript.Transcript(client, self.wakewords)

        self._audio_responder = audio_responder.AudioResponder(
            self._transcript,
            self.prompt_generator,
            self.ooba_client,
        )

    @property
    def guild(self) -> discord.guild.Guild:
        """
        :class:`Guild`: The guild we're connected to.
        """
        return self.channel.guild

    @property
    def user(self) -> discord.user.ClientUser:
        """
        :class:`ClientUser`: The user connected to voice (i.e. ourselves).
        """
        return self._state.user

    @property
    def session_id(self) -> str:
        """
        :class:`str`: The session ID for this voice connection.
        """
        return self._session_id

    async def on_voice_state_update(
        self,
        data: voice.GuildVoiceState,
        /,
    ) -> None:
        self._session_id = data["session_id"]
        channel_id = data["channel_id"]

        if not self._handshaking or self._potentially_reconnecting:
            fancy_logger.get().debug(
                "Server-initiated voice state update for Channel ID %s (Guild ID %s)",
                channel_id,
                self.guild.id,
            )

            # If we're done handshaking then we just need to update ourselves
            # If we're potentially reconnecting due to a 4014, then we need to
            # differentiate
            # a channel move and an actual force disconnect
            if channel_id is None:
                # We're being disconnected so cleanup
                await self.disconnect()
            else:
                channel = self.guild.get_channel(int(channel_id))
                if channel is None:
                    fancy_logger.get().warning(
                        "Channel ID %s not found in Guild ID %s",
                        channel_id,
                        self.guild.id,
                    )
                    return
                if not isinstance(channel, discord.channel.VocalGuildChannel):
                    fancy_logger.get().warning(
                        "Channel ID %s not a VocalGuildChannel.", channel_id
                    )
                    return
                self.channel = channel
        else:
            fancy_logger.get().debug(
                "Voice state complete for Channel ID %s (Guild ID %s) during handshake",
                channel_id,
                self.guild.id,
            )
            self._voice_state_complete.set()

    async def on_voice_server_update(
        self,
        data: voice.VoiceServerUpdate,
        /,
    ) -> None:
        if self._voice_server_complete.is_set():
            fancy_logger.get().warning("Ignoring extraneous voice server update.")
            return

        token = data["token"]
        guild_id = int(data["guild_id"])
        endpoint = data.get("endpoint")

        if endpoint is None or token is None:
            fancy_logger.get().warning(
                "Awaiting endpoint... This requires waiting. "
                "If timeout occurred considering raising the timeout and reconnecting."
            )
            return

        endpoint, _, _ = endpoint.rpartition(":")
        if endpoint.startswith("wss://"):
            # Just in case, strip it off since we're going to add it later
            endpoint = endpoint[6:]

        # this will start the process, we'll get a callback when the connection
        # is made
        await self._discrivener.run(
            self.channel.id,
            endpoint,
            guild_id,
            self.session_id,
            self.user.id,
            token,
        )
        await self._audio_responder.start()
        self._voice_server_complete.set()

    async def voice_connect(
        self, self_deaf: bool = False, self_mute: bool = False
    ) -> None:
        await self.channel.guild.change_voice_state(
            channel=self.channel, self_deaf=self_deaf, self_mute=self_mute
        )

    async def voice_disconnect(self) -> None:
        fancy_logger.get().info(
            "The voice handshake is being terminated for Channel ID %s (Guild ID %s)",
            self.channel.id,
            self.guild.id,
        )
        self._oobabot_voice_connected = False
        await self.channel.guild.change_voice_state(channel=None)
        await self._discrivener.stop()
        await self._audio_responder.stop()

    async def connect(
        self,
        *,
        reconnect: bool,
        timeout: float,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        fancy_logger.get().info("Connecting to voice...")

        if self.is_connected():
            raise VoiceClientError("Already connected to a voice channel.")

        self._voice_state_complete.clear()
        self._voice_server_complete.clear()

        futures = [
            self._voice_state_complete.wait(),
            self._voice_server_complete.wait(),
        ]

        self._handshaking = True

        fancy_logger.get().info("Starting voice handshake...")

        await self.voice_connect(self_deaf, self_mute)

        try:
            await discord.utils.sane_wait_for(futures, timeout=timeout)
        except asyncio.TimeoutError as err:
            await self.disconnect(force=True)
            raise VoiceClientError(
                f"Couldn't connect to voice channel within {timeout:.2f}s"
            ) from err

        self._oobabot_voice_connected = True

        self._voice_state_complete.clear()
        self._voice_server_complete.clear()

        fancy_logger.get().info("Voice handshake complete.")
        self._handshaking = False

    async def potential_reconnect(self) -> bool:
        # Attempt to stop the player thread from playing early
        # self._potentially_reconnecting = True
        fancy_logger.get().warning("voice_client::potential_reconnect: not implemented")

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        if not force and not self.is_connected():
            return

        try:
            await self.voice_disconnect()
        finally:
            self.cleanup()

    async def move_to(self, channel: typing.Optional[discord.abc.Snowflake]) -> None:
        """|coro|

        Moves you to a different voice channel.

        Parameters
        -----------
        channel: Optional[:class:`abc.Snowflake`]
            The channel to move to. Must be a voice channel.
        """
        # todo: tell songbird to move channels
        fancy_logger.get().warning("voice_client::move_to: not implemented")
        await self.channel.guild.change_voice_state(channel=channel)

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
        if not self._oobabot_voice_connected:
            return False
        if self._discrivener is None:
            return False
        if not self._discrivener.is_running():
            return False
        return self._discrivener_connected

    def _handle_discrivener_output(self, message: discrivener.DiscrivenerMessage):
        if message.type in (
            discrivener.DiscrivenerMessageType.CONNECT,
            discrivener.DiscrivenerMessageType.RECONNECT,
        ):
            fancy_logger.get().debug("discrivener messsage: %s", message)
            self._voice_state_complete.set()
            self._discrivener_connected = True
            return

        if discrivener.DiscrivenerMessageType.DISCONNECT == message.type:
            fancy_logger.get().debug("discrivener messsage: %s", message)
            self._voice_state_complete.set()
            self._discrivener_connected = False

            # we're disconnected, so have the voice_client disconnect
            # too... however we can't call disconnect() here because
            # we would create a deadlock.  The disconnect() method
            # will terminate the process, then wait for its notification
            # threads to exit... and we're on the notification thread
            # right now.  So to break the loop, use asyncio to schedule
            # the disconnect() call to happen later.
            loop = asyncio.get_event_loop()
            if loop is None:
                fancy_logger.get().warning(
                    "No event loop to schedule voice_client.disconnect() call"
                )
                return
            loop.call_soon_threadsafe(self.disconnect)
            return

        if discrivener.DiscrivenerMessageType.USER_JOIN == message.type:
            fancy_logger.get().debug("discrivener messsage: %s", message)
            return

        if discrivener.DiscrivenerMessageType.TRANSCRIBED_MESSAGE == message.type:
            if not isinstance(message, discrivener.TranscribedMessage):
                fancy_logger.get().warning(
                    "Transcribed message type but not TranscribedMessage"
                )
                return
            self._transcript.on_transcribed_message(message)
            return

        fancy_logger.get().warning("Unknown discrivener message type: %s", message.type)
