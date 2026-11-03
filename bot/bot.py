"""QQ Agent NoneBot2 entrypoint — reverse WS on :8080/onebot/v11/ws"""
import os
import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 确保在任何启动入口（双击脚本、原生终端、守护进程）下默认加载 dev 配置
if not os.environ.get("ENVIRONMENT"):
    os.environ["ENVIRONMENT"] = "dev"

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    nonebot.run()
