# QQ 资料与空间工具箱

给 AstrBot 用的 QQ 操作插件：改头像、改昵称、改个性签名、名片赞，以及 QQ 空间说说的发布（可附图）、AI 自动写说说、列表、点赞、评论、回复评论、删除。

两条通道：

- **LLM 工具通道（主路径）**：把操作做成 function calling 工具，主人用大白话说「把签名改成……」「发条说说」「给第一条说说点个赞」，模型自己判断并调用。工具返回的是「人格化提示语」，由模型用自己的口吻回话，所以对话里没有人机感。
- **斜杠指令通道（兜底）**：不便开口或模型没调工具时，直接敲指令。

> 依赖 **NapCat / LLOneBot** 等支持扩展 API 的 OneBot v11 实现（AstrBot 的 `aiocqhttp` 适配器）。
> 说说相关功能还需要协议端支持 `get_cookies` / `get_credentials`；Lagrange 等实现不满足。

空间层参考 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) 的实现做了对齐：

- Cookie 上下文缓存（`uin` / `skey` / `p_skey`），按 `cookie_ttl` 自动刷新，多请求串行加锁
- 登录态失效（HTTP 401 / `-3000` / 图片 `-100`）自动重新取 Cookie 并重试，最多两次
- JSONP / 非标准 JSON 响应解析（内置 `json5` 可选兜底）
- 发说说支持附图：`cgi_upload_image` 上传后拼 `pic_bo` + `richval`
- 删除、回复评论走 `h5.qzone.qq.com` 代理，评论 / 列表走对应 `taotao` 代理

## 支持平台

`metadata.yaml` 里已声明 `support_platforms: [aiocqhttp]`。

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| aiocqhttp（NapCat / LLOneBot） | 完整支持 | 头像、昵称、签名、名片赞、说说全部可用 |
| 其他 OneBot v11 实现 | 部分支持 | 头像/签名/名片赞走标准 action，说说因拿不到空间 Cookie 不可用 |
| Telegram / 飞书 / QQ 官方机器人等 | 不支持 | 没有 OneBot action，也没有 QQ 空间 Cookie |

在非 aiocqhttp 平台触发时，插件会直接回一句「仅支持 aiocqhttp 适配器」，不会抛异常影响 AstrBot 运行。

## LLM 工具

| 工具名 | 作用 | 谁可触发 |
| --- | --- | --- |
| `qq_set_nickname` | 改 QQ 昵称 | 主人 |
| `qq_set_signature` | 改个性签名，主人叫改或她心情变了自己改 | 主人 |
| `qq_post_feed` | 发一条说说，可带图 | 主人 |
| `qq_ai_post_feed` | 让她用模型自己想内容写一条说说，可给主题 | 主人 |
| `qq_list_feeds` | 看最近几条说说（给后续操作找序号） | 主人 |
| `qq_like_feed` | 给某条说说点赞 | 主人 |
| `qq_comment_feed` | 评论某条说说 | 主人 |
| `qq_reply_comment` | 回复某条评论 | 主人 |
| `qq_delete_feed` | 删除某条说说 | 主人 |
| `qq_like_card` | 给指定 QQ 名片点赞 | 主人 |

「主人」判定：`owner_session` 指定的会话、`extra_admins` 里的 QQ 号，或 AstrBot 管理员。

### 让她自己改签名

仅靠人设，模型很少主动调用签名工具。开启 `self_change_hint` 后，主人话里出现夸奖、凶她、说生日这类词时，插件会在**本轮系统提示**里补一句提醒，让她顺手改签名（这段提示不落进对话，用户看不到）。当天自己改的次数用完就不再提醒。

签名、说说都会写进插件数据目录的 `history.json`，用于每日配额和重复内容去重。

### 她会不会自己发说说

会，但受控。开启 `self_feed_hint` 后，同样是在主人夸奖 / 凶她 / 说生日时，按 `feed_hint_chance`（默认 0.25）的概率补一句「这次想发就顺手发条说说」，让她自己决定发不发。所以是偶发的、带情绪的，不会到点就冒一条。

不发的情况：`self_feed_hint` 关掉、当天说说配额（`daily_feed_limit`）用完、主人话里已经在说「说说」、或者这次概率没命中。

### 让她自己写说说（AI 生成）

和上一条不同：这里正文不是模型在聊天里顺手写的，而是**单独调一次 LLM 生成**再发布，所以提示词、模型都可以单独指定。

- 聊天里：主人说「你自己写一条说说」「随便发点什么」「按这个主题发一条」，模型会调用 `qq_ai_post_feed`
- 指令：「`/ai说说`」或「`/写说说`」，后面可以跟主题，例如 `/ai说说 今天下雨`

生成用的模型和提示词在配置页的「AI 写说说」分组里：`post_provider_id`（留空则用当前会话的提供商）、`post_prompt`（建议按人格改写）。生成结果会去掉代码块、引号和「说说：」之类前缀，再交给正常的发说说流程，所以冷却、每日上限、去重一样生效。

## 斜杠指令

| 指令 | 说明 |
| --- | --- |
| `/改头像` | 发送图片时附带该指令，修改机器人头像 |
| `/改昵称 昵称` | 修改 QQ 昵称 |
| `/改签名 内容` | 修改个性签名 |
| `/名片赞 @某人 10` | 名片点赞，次数可省略 |
| `/发说说 内容` | 发布说说，可同时附图片 |
| `/ai说说 主题` | 用模型自己写一条说说并发布，主题可省略；也可用 `/写说说` |
| `/说说列表 10` | 拉取最近说说并缓存序号（附前几条评论的序号） |
| `/赞说说 1` | 给说说点赞 |
| `/评说说 1 内容` | 评论说说，也可用 `/评论说说`、`/回复说说` |
| `/回复评论 1 2 内容` | 回复第 1 条说说下的第 2 条评论 |
| `/删说说 1` | 删除说说 |
| `/qq帮助` | 查看指令 |

序号来自 `/说说列表`，也可以直接填说说 tid。

## 安装

1. 把 `astrbot_plugin_qq_toolbox` 整个文件夹放进 AstrBot 的 `data/plugins/`。
2. WebUI 里重载插件或重启 AstrBot。
3. 在 AstrBot 里启用「函数调用工具 / Function Calling」，LLM 工具通道才会生效。
4. 依赖 `aiohttp`（AstrBot 环境通常自带），缺失时 `pip install aiohttp`。

## 配置项

配置页按用途分成 7 组，从上到下依次是：

**权限与主人**

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `owner_session` | string | 空 | 主人的私聊会话，形如 `机器人名:FriendMessage:QQ号` |
| `extra_admins` | list | [] | 额外放行的 QQ 号；私聊建议填自己 |
| `admin_only` | bool | true | 只影响斜杠指令的权限 |

**个性签名**

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `daily_self_limit` | int | 2 | 她一天最多自己改几次签名 |
| `daily_owner_limit` | int | 10 | 主人叫她改，一天最多几次 |
| `max_length` | int | 40 | 签名最大长度 |
| `self_change_hint` | bool | true | 情绪词触发「顺手改签名」提示 |

**说说与空间**

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `self_feed_hint` | bool | true | 情绪词触发「可以自己发条说说」提示 |
| `feed_hint_chance` | float | 0.25 | 说说提醒的触发概率 |
| `daily_feed_limit` | int | 2 | 一天最多发几条说说 |
| `max_list` | int | 10 | 说说列表默认条数（1-20） |
| `max_images` | int | 9 | 一条说说最多附图张数（上限 9，0 为禁用） |

**AI 写说说**

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `post_provider_id` | string | 空 | 写说说用的 LLM 提供商，留空用当前会话的 |
| `post_prompt` | text | 见页面 | 写说说的提示词，建议按人格改写 |

**名片赞**

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `like_times` | int | 10 | 名片赞默认次数（1-20） |
| `daily_card_like_limit` | int | 30 | 每天名片赞上限 |

**风控与频率**

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `cooldown_seconds` | int | 15 | 写操作全局冷却秒数，0 为不限制 |
| `daily_like_limit` | int | 20 | 每天说说点赞上限 |
| `daily_comment_limit` | int | 10 | 每天评论 + 回复上限 |
| `daily_delete_limit` | int | 5 | 每天删除说说上限 |

**空间连接**

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `qzone_domain` | string | user.qzone.qq.com | 获取 Cookie 的域名，失败自动回退 qzone.qq.com |
| `cookie_ttl` | int | 600 | Cookie 缓存秒数，0 表示每次都重新取 |
| `timeout` | int | 20 | 空间接口超时秒数 |

## 接口说明

- 签名：`set_self_longnick`（`longNick`），失败时回退 `set_qq_profile`（`personal_note`）
- 头像：`set_qq_avatar`
- 名片赞：`send_like`
- 空间：`get_cookies` 取 Cookie 后请求，`uin` / `skey` / `p_skey` 缓存复用
  - 图片上传 `up.qzone.qq.com/cgi-bin/upload/cgi_upload_image`
  - 发布 `emotion_cgi_publish_v6`
  - 列表 `emotion_cgi_msglist_v6`
  - 点赞 `likes/internal_dolike_app`
  - 评论 `emotion_cgi_re_feeds`
  - 回复评论 `emotion_cgi_re_feeds`（`h5.qzone.qq.com` 代理，带 `commentId`）
  - 删除 `emotion_cgi_delete_v6`（`h5.qzone.qq.com` 代理）

## 常见问题

- **模型不调用工具**：确认已开启函数调用；确认发消息的人是主人（`owner_session` / `extra_admins` / 管理员）。模型对工具名不熟时，可在人设里提一句她有哪些 QQ 能力。
- **提示没有权限**：私聊里 `is_admin()` 判不出来，把自己的 QQ 填进 `extra_admins`。
- **获取空间 Cookie 失败**：确认机器人已登录、协议端是 NapCat/LLOneBot 且支持 `get_cookies`。
- **说说接口返回非 0 错误码**：空间有风控，新号或高频操作容易被限，降低频率、先用小号测。
- **登录态失效**：插件会自动重取 Cookie 并重试两次；仍失败就检查 NapCat 是否掉线。
- **图片没发出去**：附图走 `cgi_upload_image`，接口比较脆弱，失败时检查 `max_images` 和图片地址是否可访问；读取失败的图会被跳过。
- **改头像无反应**：NapCat 需能访问图片地址，临时链接可能失效。

## 文件结构

```
astrbot_plugin_qq_toolbox/
├── main.py            # 插件逻辑（LLM 工具 + 指令）
├── metadata.yaml      # 插件元数据
├── _conf_schema.json  # 配置项定义
├── requirements.txt   # 依赖
└── README.md          # 本文件
```

## 版本

- 1.6.1：新增改 QQ 昵称（`qq_set_nickname` / `/改昵称`）
- 1.6.0：移除 WebUI 只读说说视图，只保留聊天里的工具与指令通道
- 1.5.0：新增 AI 写说说（`qq_ai_post_feed` / `/ai说说`），可单独指定模型与提示词；配置页按用途分成 7 组
- 1.4.0：新增 WebUI 只读说说视图（列表 / 评论详情 / 今日配额），不提供写入接口
- 1.3.0：指令去掉 `qq` 前缀（原 qzone 插件已移除，不再冲突）
- 1.2.1：增加全局冷却与每日上限（点赞/评论/删除/名片赞），降低空间风控
- 1.2.0：对齐参考插件重写空间层——说完接口参数、Cookie 缓存与自动重登、附图发布、回复评论
- 1.1.3：LLM 工具的数字参数改为按字符串传入，修复 4.29.3 不支持 integer 类型
- 1.1.2：移除 `@register` 装饰器，改用 metadata 注册（修复 AstrBot 4.x 加载报 register 未定义）
- 1.1.1：加入情绪触发的「自己发说说」，带概率和每日配额
- 1.1.0：改成 LLM 工具 + 指令双通道，签名改用 `set_self_longnick`，加每日配额与 history.json
- 1.0.0：首个版本，指令形式

## 许可证

MIT
