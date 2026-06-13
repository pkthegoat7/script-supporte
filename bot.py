"""Backend Discord do auto-ticket. Roda como task no mesmo event loop do FastAPI."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from typing import Awaitable, Callable

import discord


LogCB = Callable[[str, str], Awaitable[None]]
StatusCB = Callable[[str, str], Awaitable[None]]


def _stdout(msg: str):
    print(f"[bot] {msg}", flush=True, file=sys.stdout)


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
        self._claimed_messages: set[int] = set()

        self._register_events()

    async def _log(self, msg: str, level: str = "info"):
        _stdout(f"{level.upper()}: {msg}")
        await self.on_log(level, f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    async def _set_status(self, label: str, color: str):
        _stdout(f"STATUS: {label}")
        await self.on_status(label, color)

    def _all_buttons(self, message: discord.Message):
        out = []
        for row in getattr(message, "components", []) or []:
            children = getattr(row, "children", None) or [row]
            for comp in children:
                out.append(comp)
        return out

    def _find_button(self, message: discord.Message):
        target = self.config["button_label"].lower().strip()
        for comp in self._all_buttons(message):
            lab = getattr(comp, "label", None)
            if lab and target in lab.lower():
                return comp
        return None

    async def _try_claim_message(self, msg: discord.Message, thread: discord.Thread) -> bool:
        button = self._find_button(msg)
        if button is None:
            return False
        await self._log(f"clicando '{button.label}' em {thread.name}", "info")
        try:
            await button.click()
            await self._log(f"Ticket assumido: {thread.name} ({thread.id})", "ok")
            return True
        except Exception as e:
            await self._log(f"click falhou: {e!r}", "err")
            return False

    async def _claim_thread_by_history(self, thread: discord.Thread) -> bool:
        """Fallback: varre o historico procurando a embed com o botao."""
        deadline = asyncio.get_event_loop().time() + self.config["embed_wait_seconds"]
        attempts = 0
        while asyncio.get_event_loop().time() < deadline:
            attempts += 1
            try:
                bot_id = self.config["bot_id"]
                async for msg in thread.history(limit=20, oldest_first=True):
                    if bot_id and msg.author.id != bot_id:
                        continue
                    btns = self._all_buttons(msg)
                    if btns:
                        labels = [getattr(b, "label", "?") for b in btns]
                        await self._log(f"msg {msg.id} botoes: {labels}", "muted")
                    if await self._try_claim_message(msg, thread):
                        return True
            except discord.HTTPException as e:
                await self._log(f"erro lendo historico (tentativa {attempts}): {e!r}", "warn")
            await asyncio.sleep(1.5)
        await self._log(f"timeout esperando botao em {thread.id}", "warn")
        return False

    def _register_events(self):
        client = self.client

        @client.event
        async def on_ready():
            await self._log(f"Conectado como {client.user} (id={client.user.id})", "ok")
            cfg = self.config
            await self._log(
                f"config: parent={cfg['parent_channel_id']} bot={cfg['bot_id']} label='{cfg['button_label']}'",
                "muted",
            )
            await self._set_status("conectado (aguardando ticket)", "ok")

        @client.event
        async def on_thread_create(thread: discord.Thread):
            await self._log(
                f"thread_create id={thread.id} name='{thread.name}' parent={thread.parent_id}",
                "muted",
            )
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
                    await self._log(f"entrei na thread {thread.id}", "muted")
                except Exception as e:
                    await self._log(f"join falhou: {e!r}", "warn")
                if await self._claim_thread_by_history(thread):
                    self.current_ticket_id = thread.id
                    await self._set_status(f"atendendo ticket {thread.id}", "accent")

        @client.event
        async def on_message(message: discord.Message):
            # Trigger principal: quando o bot HIT manda a embed com botao no thread,
            # tentamos assumir. Isso e mais confiavel que on_thread_create.
            channel = message.channel
            if not isinstance(channel, discord.Thread):
                return
            if channel.parent_id != self.config["parent_channel_id"]:
                return
            bot_id = self.config["bot_id"]
            if bot_id and message.author.id != bot_id:
                return
            if not self._all_buttons(message):
                return
            if message.id in self._claimed_messages:
                return
            if self.current_ticket_id is not None and self.current_ticket_id != channel.id:
                return

            async with self.claim_lock:
                if self.current_ticket_id is not None and self.current_ticket_id != channel.id:
                    return
                self._claimed_messages.add(message.id)
                try:
                    await channel.join()
                except Exception:
                    pass
                if await self._try_claim_message(message, channel):
                    self.current_ticket_id = channel.id
                    await self._set_status(f"atendendo ticket {channel.id}", "accent")

        @client.event
        async def on_message_edit(before: discord.Message, after: discord.Message):
            # Caso a embed venha por edit
            await on_message(after)

        @client.event
        async def on_thread_delete(thread: discord.Thread):
            await self._log(f"thread_delete {thread.id}", "muted")
            if thread.id == self.current_ticket_id:
                await self._log(f"ticket {thread.id} fechado, livre", "ok")
                self.current_ticket_id = None
                await self._set_status("conectado (aguardando ticket)", "ok")

        @client.event
        async def on_raw_thread_delete(payload: discord.RawThreadDeleteEvent):
            await self._log(f"raw_thread_delete {payload.thread_id}", "muted")
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
