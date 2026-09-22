"""Unix Domain Socket server — communicates with fcitx5 plugin"""
import asyncio
import json
import logging
import os
import struct
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("aoide.server")


class UnixSocketServer:
    """Unix Domain Socket server"""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: set = set()
        self._running = False

        # Callbacks
        self.on_audio_data: Optional[Callable] = None
        self.on_start_listening: Optional[Callable] = None
        self.on_stop_listening: Optional[Callable] = None
        self.on_interrupt: Optional[Callable] = None  # ★ 打断收尾回调
        self.on_toggle: Optional[Callable] = None
        self.on_config: Optional[Callable] = None
        self.on_reset: Optional[Callable] = None
        self.on_commit_now: Optional[Callable] = None  # ★ commit_now 回调

    async def serve(self):
        self._running = True
        parent = Path(self.socket_path).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.stat().st_uid != os.getuid():
            raise PermissionError(f"Socket directory is not owned by this user: {parent}")
        parent.chmod(0o700)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        os.chmod(self.socket_path, 0o600)
        logger.info(f"Unix socket server started on {self.socket_path}")

    async def _handle_client(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        logger.info(f"Client connected: {addr}")
        self._clients.add(writer)

        try:
            while self._running:
                length_data = await reader.readexactly(4)
                msg_len = struct.unpack("!I", length_data)[0]

                if msg_len == 0:
                    continue

                if msg_len > 10 * 1024 * 1024:
                    logger.warning(f"Message too large: {msg_len} bytes")
                    continue

                data = await reader.readexactly(msg_len)
                await self._process_message(data, writer)
            logger.info(f"Client handler exiting: self._running=False")

        except asyncio.IncompleteReadError as e:
            logger.info(f"Client handler: IncompleteReadError (client closed): {e}")
        except ConnectionResetError as e:
            logger.info(f"Client handler: ConnectionResetError: {e}")
        except Exception as e:
            logger.error(f"Client handler error: {e}", exc_info=True)
        finally:
            self._clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(f"Client disconnected: {addr}")

    async def _process_message(self, data: bytes, writer: asyncio.StreamWriter):
        try:
            message = data.decode("utf-8", errors="replace")

            # Check for audio message (JSON header + \0 + PCM)
            null_pos = message.find('\0')
            if null_pos >= 0:
                json_part = message[:null_pos]
                pcm_part = data[len(json_part) + 1:]
            else:
                json_part = message
                pcm_part = None

            try:
                cmd = json.loads(json_part)
            except json.JSONDecodeError:
                logger.debug(f"Invalid JSON: {json_part[:100]}")
                return

            msg_type = cmd.get("type", "")

            if msg_type == "audio" and pcm_part:
                logger.debug(f"Received external audio: {cmd.get('samples', 0)} samples")
                if self.on_audio_data:
                    await self.on_audio_data(pcm_part)

            elif msg_type == "start_listening":
                logger.debug("Command: start_listening")
                if self.on_start_listening:
                    await self.on_start_listening()

            elif msg_type == "stop_listening":
                logger.debug("Command: stop_listening")
                if self.on_stop_listening:
                    await self.on_stop_listening()

            elif msg_type == "interrupt":
                logger.debug("Command: interrupt")
                if self.on_interrupt:
                    await self.on_interrupt()

            elif msg_type == "toggle":
                logger.debug("Command: toggle")
                if self.on_toggle:
                    await self.on_toggle()

            elif msg_type == "reset":
                logger.debug("Command: reset")
                if self.on_reset:
                    await self.on_reset()
                await self.broadcast({"type": "reset"})

            elif msg_type == "commit_now":
                logger.debug("Command: commit_now")
                # ★ 通过回调让 pipeline 真正 commit buffer 内容
                if self.on_commit_now:
                    await self.on_commit_now()
                else:
                    await self.broadcast({"type": "commit"})

            elif msg_type == "optimize_now":
                logger.debug("Command: optimize_now")
                await self.broadcast({"type": "optimize_now"})

            elif msg_type == "config":
                logger.debug("Command: config (LLM/VAD/audio settings)")
                if self.on_config:
                    await self.on_config(cmd)
                await self._send(writer, {"type": "config_ack"})

            elif msg_type == "ping":
                await self._send(writer, {"type": "pong"})

            else:
                logger.debug(f"Unknown message type: {msg_type}")

        except Exception as e:
            logger.error(f"Process message error: {e}")

    async def broadcast(self, message: dict, exclude=None):
        if not self._clients:
            logger.warning(f"broadcast {message.get('type')}: no clients connected")
            return

        if message.get('type') == 'preedit':
            # preedit uses green/yellow/red keys, not 'text'
            total = (len(message.get('green', '')) +
                     len(message.get('yellow', '')) +
                     len(message.get('red', '')))
            logger.info(f"broadcast preedit: {total} chars "
                        f"({len(message.get('green', ''))}/{len(message.get('yellow', ''))}/{len(message.get('red', ''))}) "
                        f"to {len(self._clients)} client(s)")
        else:
            logger.info(f"broadcast {message.get('type')}: {str(message.get('text', ''))[:40]} to {len(self._clients)} client(s)")

        disconnected = set()
        for client in self._clients:
            if client == exclude:
                continue
            try:
                await self._send(client, message)
            except Exception:
                disconnected.add(client)

        self._clients -= disconnected

    async def _send(self, writer: asyncio.StreamWriter, message: dict):
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        length = struct.pack("!I", len(data))
        writer.write(length + data)
        await writer.drain()

    def stop(self):
        self._running = False
        for client in list(self._clients):
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()
        if self._server:
            self._server.close()

    @property
    def client_count(self) -> int:
        return len(self._clients)
