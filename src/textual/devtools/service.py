"""Manages a running devtools instance"""
from __future__ import annotations

import asyncio
import base64
import json
import pickle
from json import JSONDecodeError
from typing import cast

from aiohttp import WSMessage, WSMsgType
from aiohttp.abc import Request
from aiohttp.web_ws import WebSocketResponse
from rich.console import Console
from rich.markup import escape

from textual.devtools.renderables import DevtoolsLogMessage, DevtoolsInternalMessage

QUEUEABLE_TYPES = {"client_log", "client_spillover"}


class DevtoolsService:
    """A running instance of devtools has a single DevtoolsService which is
    responsible for tracking connected client applications.
    """

    def __init__(self, update_frequency: float) -> None:
        """
        Args:
            update_frequency (float): The number of seconds to wait between
                sending updates of the console size to connected clients.
        """
        self.update_frequency = update_frequency
        self.console = Console()
        self.shutdown_event = asyncio.Event()
        self.clients: list[ClientHandler] = []

    async def start(self):
        """Starts devtools tasks"""
        self.size_poll_task = asyncio.create_task(self._console_size_poller())

    @property
    def clients_connected(self) -> bool:
        """Returns True if there are connected clients, False otherwise."""
        return len(self.clients) > 0

    async def _console_size_poller(self) -> None:
        """Poll console dimensions, and add a `server_info` message to the Queue
        any time a change occurs. We only poll if there are clients connected,
        and if we're not shutting down the server.
        """
        current_width = self.console.width
        current_height = self.console.height
        while not self.shutdown_event.is_set():
            width = self.console.width
            height = self.console.height
            dimensions_changed = width != current_width or height != current_height
            if dimensions_changed:
                await self._send_server_info_to_all()
                current_width = width
                current_height = height
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=self.update_frequency
                )
            except asyncio.TimeoutError:
                pass

    async def _send_server_info_to_all(self) -> None:
        """Add `server_info` message to the queues of every client"""
        for client_handler in self.clients:
            await self.send_server_info(client_handler)

    async def send_server_info(self, client_handler: ClientHandler) -> None:
        """Send information about the server e.g. width and height of Console to
        a connected client.

        Args:
            client_handler (ClientHandler): The client to send information to
        """
        await client_handler.send_message(
            {
                "type": "server_info",
                "payload": {
                    "width": self.console.width,
                    "height": self.console.height,
                },
            }
        )

    async def handle(self, request: Request) -> WebSocketResponse:
        """Handles a single client connection"""
        client = ClientHandler(request, service=self)
        self.clients.append(client)
        websocket = await client.run()
        self.clients.remove(client)
        return websocket

    async def shutdown(self) -> None:
        """Stop server async tasks and clean up all client handlers"""

        # Stop polling/writing Console dimensions to clients
        self.shutdown_event.set()
        await self.size_poll_task

        # We're shutting down the server, so inform all connected clients
        for client in self.clients:
            await client.close()
        self.clients.clear()


class ClientHandler:
    """Handles a single client connection to the devtools.
    A single DevtoolsService managers many ClientHandlers. A single ClientHandler
    corresponds to a single running Textual application instance, and is responsible
    for communication with that Textual app.
    """

    def __init__(self, request: Request, service: DevtoolsService) -> None:
        """
        Args:
            request (Request): The aiohttp.Request associated with this client
            service (DevtoolsService): The parent DevtoolsService which is responsible
                for the handling of this client.
        """
        self.request = request
        self.service = service
        self.websocket = WebSocketResponse()

    async def send_message(self, message: dict[str, object]) -> None:
        """Send a message to a client

        Args:
            message (dict[str, object]): The dict which will be sent
                to the client.
        """
        await self.outgoing_queue.put(message)

    async def _consume_outgoing(self) -> None:
        """Consume messages from the outgoing (server -> client) Queue."""
        while True:
            message_json = await self.outgoing_queue.get()
            if message_json is None:
                self.outgoing_queue.task_done()
                break
            type = message_json["type"]
            if type == "server_info":
                await self.websocket.send_json(message_json)
            self.outgoing_queue.task_done()

    async def _consume_incoming(self) -> None:
        """Consume messages from the incoming (client -> server) Queue, and print
        the corresponding renderables to the console for each message.
        """
        while True:
            message_json = await self.incoming_queue.get()
            if message_json is None:
                self.incoming_queue.task_done()
                break

            type = message_json["type"]
            if type == "client_log":
                path = message_json["payload"]["path"]
                line_number = message_json["payload"]["line_number"]
                timestamp = message_json["payload"]["timestamp"]
                encoded_segments = message_json["payload"]["encoded_segments"]
                decoded_segments = base64.b64decode(encoded_segments)
                segments = pickle.loads(decoded_segments)
                self.service.console.print(
                    DevtoolsLogMessage(
                        segments=segments,
                        path=path,
                        line_number=line_number,
                        unix_timestamp=timestamp,
                    )
                )
            elif type == "client_spillover":
                spillover = int(message_json["payload"]["spillover"])
                info_renderable = DevtoolsInternalMessage(
                    f"Discarded {spillover} messages", level="warning"
                )
                self.service.console.print(info_renderable)
            self.incoming_queue.task_done()

    async def run(self) -> WebSocketResponse:
        """Prepare the websocket and communication queues, and continuously
        read messages from the queues.

        Returns:
            WebSocketResponse: The WebSocketResponse associated with this client.
        """

        await self.websocket.prepare(self.request)
        self.incoming_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.outgoing_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.outgoing_messages_task = asyncio.create_task(self._consume_outgoing())
        self.incoming_messages_task = asyncio.create_task(self._consume_incoming())

        if self.request.remote:
            self.service.console.print(
                DevtoolsInternalMessage(
                    f"Client '{escape(self.request.remote)}' connected"
                )
            )
        try:
            await self.service.send_server_info(client_handler=self)
            async for message in self.websocket:
                message = cast(WSMessage, message)
                if message.type == WSMsgType.TEXT:
                    try:
                        message_json = json.loads(message.data)
                    except JSONDecodeError:
                        self.service.console.print(escape(str(message.data)))
                        continue

                    type = message_json.get("type")
                    if not type:
                        continue
                    if (
                        type in QUEUEABLE_TYPES
                        and not self.service.shutdown_event.is_set()
                    ):
                        await self.incoming_queue.put(message_json)
                elif message.type == WSMsgType.ERROR:
                    self.service.console.print(
                        DevtoolsInternalMessage(
                            "Websocket error occurred", level="error"
                        )
                    )
                    break
        except Exception as error:
            self.service.console.print(
                DevtoolsInternalMessage(str(error), level="error")
            )
        finally:
            if self.request.remote:
                self.service.console.print(
                    "\n",
                    DevtoolsInternalMessage(
                        f"Client '{escape(self.request.remote)}' disconnected"
                    ),
                )
            await self.close()

        return self.websocket

    async def close(self) -> None:
        """Stop all incoming/outgoing message processing,
        and shutdown the websocket connection associated with this
        client.
        """

        # Stop any writes to the websocket first
        await self.outgoing_queue.put(None)
        await self.outgoing_messages_task

        # Now we can shut the socket down
        await self.websocket.close()

        # This task is independent of the websocket
        await self.incoming_queue.put(None)
        await self.incoming_messages_task