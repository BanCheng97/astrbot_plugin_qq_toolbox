# 大肥鱼 QQ 工具箱（合集）

把原来四个独立插件合并成一个 AstrBot 插件，并附带一个统一的可视化控制台：

| 模块 | 原插件 | 主要能力 |
| --- | --- | --- |
| 告状 | `astrbot_plugin_dafeiyu_complain` | 被欺负时私聊家人告状、家人互相告状、让机器人传话 |
| 用量 | `astrbot_plugin_dafeiyu_usage` | 查 DeepSeek 余额、今日/近 7 天消耗、token 估算、低余额撒娇提醒 |
| 语音 | `astrbot_plugin_dafeiyu_voice` | 语音识别（STT）、语音合成（TTS）、概率自动触发、歌唱模式 |
| QQ 工具箱 | `astrbot_plugin_qq_toolbox` | 改头像/昵称/签名、名片赞、发说说、AI 写说说、点赞/评论/回复/删除说说 |

插件标识名：`astrbot_plugin_qq_toolbox`（目录名 `astrbot_plugin_dafeiyu_suite` 也可以，Web API 双前缀已兼容）。

## 代码结构

```
astrbot_plugin_dafeiyu_suite/
├── main.py                  # 唯一入口类 DafeiyuSuitePlugin + Web API + Handler 归位
├── modules/
│   ├── base.py              # 四个模块共用：配置查找、数据目录、JSON、OneBot 客户端
│   ├── complain.py          # 告状 Mixin
│   ├── usage.py             # 用量 Mixin
│   ├── voice.py             # 语音 Mixin
│   └── toolbox.py           # QQ 工具箱 Mixin
├── views/suite/index.html   # 可视化控制台（Plugin Views）
├── _conf_schema.json        # 四命名空间配置（complain / usage / voice / toolbox）
└── .astrbot-plugin/i18n/    # 视图标题与说明
```

- **一个入口类 + 四个 Mixin**：AstrBot 只认一个入口类，业务按模块拆分，方法名全部加模块前缀，多继承不会静默覆盖。
- **公共依赖收拢**：配置读取、数据目录、JSON 读写、数值转换、OneBot 客户端统一在 `modules/base.py`。
- **配置隔离**：四个模块各自一个命名空间，`owner_session`、`max_length`、`enabled` 这类同名键不会串味。
- **Handler 归位**：AstrBot 只给 main.py 模块路径下的 Handler 绑定实例并分发事件，插件启动时会把子模块的 Handler 与 LLM 工具重新挂回 main 路径，保证指令、工具、监听都照常生效。

## 安装

1. 把整个 `astrbot_plugin_dafeiyu_suite` 文件夹放进 AstrBot 的 `data/plugins/`。
2. 安装依赖：`pip install -r requirements.txt`（AstrBot 通常会自动装）。
3. 重载插件。
4. 在插件配置页或可视化控制台里填写需要的 Key / 主人 QQ / 家人名单。

依赖说明：

- `aiohttp`：QQ 空间接口
- `httpx`：DeepSeek 余额与语音接口
- `edge-tts`：保留的原语音依赖
- `audioop-lts`：Python 3.13+ 的 `audioop` 替代（Python 3.12 及以下不需要）
- `pysilk`（可选）：WAV → Silk 转码，装不上时会自动回退到适配器发送

## 可视化控制台

插件加载后打开 AstrBot 的插件视图页「大肥鱼合集控制台」，或直接访问：

```
/{PLUGIN_NAME}/suite
```

页面包含六个页签：

- **总览**：插件版本、已注册 Handler / LLM 工具数量、平台、四个模块的启用状态与今日计数
- **告状**：预览告状文案（不发消息）、手动告状、传话、查看最近记录
- **用量**：一键查余额与消耗报告、手动触发低余额检查
- **语音**：输入文本合成语音并试听（说 / 唱）、上传音频转文字
- **QQ 工具箱**：测试空间连接、改昵称/签名/头像、发说说、AI 预览、说说列表、点赞/评论/回复/删除、名片赞
- **记录**：告状记录与 QQ 操作记录

配置页签按 `_conf_schema.json` 自动生成表单，支持分组折叠、开关、下拉、滑块、列表与密钥字段；密钥保存后只显示「已配置」，留空不会覆盖原值。

页面上的动作调用与聊天里的工具调用走同一套方法，因此频控、冷却、每日上限、历史记录行为完全一致。

## 模块说明

### 告状

- 模型判断被骂/被欺负时调用 `qq_complain`，插件私聊发告诉家人；管理员也可 `/告状 内容` 手动触发。
- 欺负者在 `guardians` 名单里 → 发给名单里的其他人；名单外 → 发给 `owner_umo` / `owner_qq`。
- 同一人冷却、每天总上限、群聊附带群号、重复欺负升级提示均可配置。
- 家人可让机器人传话（`qq_relay_message`），有独立冷却与每日上限。

### 用量

- LLM 工具 `deepseek_money`，指令 `/用量`、`/余额`、`/用量帮助`（默认仅管理员）。
- DeepSeek 只提供余额接口，消耗按余额快照推算，token 数按单价换算。
- 低余额时私聊主人，同一天最多一次；可按人格用模型写提醒。
- 定时检查任务在 `initialize` 启动、`terminate` 停止。

### 语音

- 收到的语音自动转录（MiMo ASR），转录结果注入给模型。
- 回复可按概率自动合成语音；`/说 文本` 手动合成，`/唱 歌词` 歌唱模式。
- TTS 后端可选 BreezeBlue 或 MiMo，支持语气指令、音色选择；WAV 会转 Silk 后由 OneBot 发送。
- `/听到` 可让机器人听当前会话最近一条语音。

### QQ 工具箱

- 11 个 LLM 工具 + 15 个指令：`/qq帮助`、`/改头像`、`/改昵称`、`/改签名`、`/名片赞`、`/发说说`、`/ai说说`、`/写说说`、`/说说列表`、`/赞说说`、`/评说说`、`/评论说说`、`/回复说说`、`/回复评论`、`/删说说`。
- 空间接口走 NapCat / LLOneBot 的 Cookie 通道，自动缓存登录态、失效重登、非标准 JSON 解析。
- 所有写操作有全局冷却与每日上限，历史上限记录在 `history.json`。
- 情绪触发（被夸、被凶、生日）会按概率提醒她改签名或发说说。

## 配置迁移

首次加载时，插件会读取 AstrBot 配置目录下旧四个插件的配置文件：

```
astrbot_plugin_dafeiyu_complain_config.json
astrbot_plugin_dafeiyu_usage_config.json
astrbot_plugin_dafeiyu_voice_config.json
astrbot_plugin_qq_toolbox_config.json
```

把其中的键按四命名空间写入新配置 `astrbot_plugin_dafeiyu_suite_config.json`。只在新配置仍为默认值时才覆盖，已经手动改过的新配置不会被旧值打扰。旧插件目录与旧配置文件不会被删除，可以随时回滚。

## 数据文件

各模块仍使用原插件的数据目录，历史数据可直接沿用：

- 告状：`.../astrbot_plugin_dafeiyu_complain/complaints.json`
- 用量：`.../astrbot_plugin_dafeiyu_usage/usage.json`
- 语音：`.../astrbot_plugin_dafeiyu_voice/`
- 工具箱：`.../astrbot_plugin_qq_toolbox/history.json`

## 常见问题

**指令没反应 / 工具不触发？**
确认 AstrBot 版本支持插件子模块（本插件已在 `__init__` 里把 Handler 与 LLM 工具归位到 main 路径）；查看日志里是否有「已把 N 个 Handler 归位」。

**可视化页面打不开？**
需要 AstrBot 4.x 的 Plugin Views 与 `context.register_web_api`。老版本会在日志提示「可视化视图不可用」，聊天指令与 LLM 工具不受影响。

**空间操作报登录失效？**
确认 NapCat / LLOneBot 在线且已登录；插件会自动重取 Cookie 并重试两次。

**语音发不出去？**
检查 `pysilk` 是否安装、OneBot 适配器是否支持 `send_group_msg` / `send_private_msg`；日志会标明失败环节。

## 许可证

MIT
