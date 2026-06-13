"""Backend Discord do auto-ticket. Roda como task no mesmo event loop do FastAPI."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Awaitable, Callable

import discord


LogCB = Callable[[str, str], Awaitable[None]]
StatusCB = Callable[[str, str], Awaitable[None]]


class TicketBot:
    def __init__(self, config: dict, on_log: LogCB, on_status: StatusCB):
        self.config = config
        self.on_log = on_log
        self.on_status = on_status

        self.client = discord.Client()
        self.current_ticket_id: int | None = None
        self.claim_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopped = False

        self._register_events()

    async def _log(self, msg: str, level: str = "info"):
        await self.on_log(level, f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    async def _set_status(self, label: str, color: str):
        await self.on_status(label, color)

    def _find_button(self, message: discord.Message):
        target = self.config["button_label"].lower()
        for row in getattr(message, "components", []) or []:
            children = getattr(row, "children", None) or [row]
            for comp in children:
                lab = getattr(comp, "label", None)
                if lab and target in lab.lower():
                    return comp
        return None

    async def _try_claim(self, thread: discord.Thread) -> bool:
        deadline = asyncio.get_event_loop().time() + self.config["embed_wait_seconds"]
        while asyncio.get_event_loop().time() < deadline:
            try:
                async for msg in thread.history(limit=15, oldest_first=True):
                    bot_id = self.config["bot_id"]
                    if bot_id and msg.author.id != bot_id:
                        continue
                    button = self._find_button(msg)
                    if button is not None:
                        try:
                            await button.click()
                            await self._log(f"Ticket assumido: {thread.name}", "ok")
                            return True
                        except Exception as e:
                            await self._log(f"click falhou: {e!r}", "err")
                            return False
            except discord.HTTPException as e:
                await self._log(f"erro lendo historico: {e!r}", "warn")
            await asyncio.sleep(1.5)
        await self._log(f"botao nao apareceu em {thread.id}", "warn")
        return False

    def _register_events(self):
        client = self.client

        @client.event
        async def on_ready():
            await self._log(f"Conectado como {client.user}", "ok")
            await self._set_status("conectado (aguardando ticket)", "ok")

        @client.event
        async def on_thread_create(thread: discord.Thread):
            if thread.parent_id != self.config["parent_channel_id"]:
                return
            if self.current_ticket_id is not None:
                await self._log(f"ja atendendo {self.current_ticket_id}, ignorando", "info")
                return
            async with self.claim_lock:
                if self.current_ticket_id is not None:
                    return
                try:
                    await thread.join()
                except Exception:
                    pass
                if await self._try_claim(thread):
                    self.current_ticket_id = thread.id
                    await self._set_status(f"atendendo ticket {thread.id}", "accent")

        @client.event
        async def on_thread_delete(thread: discord.Thread):
            if thread.id == self.current_ticket_id:
                await self._log(f"ticket {thread.id} fechado, livre", "ok")
                self.current_ticket_id = None
                await self._set_status("conectado (aguardando ticket)", "ok")

        @client.event
        async def on_raw_thread_delete(payload: discord.RawThreadDeleteEvent):
            if payload.thread_id == self.current_ticket_id:
                await self._log(f"ticket {payload.thread_id} fechado (raw)", "ok")
                self.current_ticket_id = None
                await self._set_status("conectado (aguardando ticket)", "ok")

    async def _runner(self):
        try:
            await self.client.start(self.config["token"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._log(f"FATAL: {e!r}", "err")
            await self._set_status("erro", "err")
        finally:
            try:
                await self.client.close()
            except Exception:
                pass
            await self._set_status("parado", "muted")

    def start(self):
        self._stopped = False
        self._task = asyncio.create_task(self._runner())

    async def stop(self):
        self._stopped = True
        try:
            await self.client.close()
        except Exception:
            pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()
