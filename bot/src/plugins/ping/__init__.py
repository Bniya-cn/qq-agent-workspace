"""Task1 smoke plugin: /ping -> pong"""
from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="ping",
    description="链路联调冒烟：/ping -> pong",
    usage="/ping",
)

ping = on_command("ping", priority=1, block=True)


@ping.handle()
async def handle_ping(event: MessageEvent):
    await ping.finish("pong")
