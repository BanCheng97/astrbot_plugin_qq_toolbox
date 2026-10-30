# 更新日志

## 2.1.1（2026-10-30）

### 修复

- AI 写说说的 `post_provider_id` 指向已删除或非聊天的 provider 时，先自查再回退当前会话模型，不再触发 AstrBot 的 `Provider xxx was not found` 警告

## 2.1.0（2026-10-30）

### 语音增强

- 语音情绪从 6 种扩展到 13 种（新增害羞、害怕、疲惫、兴奋、调皮、傲娇、惊讶），每种情绪补充语速、音调、气息、停顿指令，情绪自动识别同步升级
- 歌唱模式新增唱法 `sing_style`（pop / lyrical / child），歌词自动切句与换气停顿，控制台可选择唱法并透传给 TTS，去掉原来手工插入的省略号

## 2.0.0（2026-10-30）

四个独立插件合并为「大肥鱼工具箱」，插件标识名 `astrbot_plugin_dafeiyu_toolbox`（兼容旧前缀 `astrbot_plugin_qq_toolbox` / `astrbot_plugin_dafeiyu_suite`）。

### 架构

- 一个入口类 `DafeiyuSuitePlugin` + 四个 Mixin（告状 / 用量 / 语音 / QQ 工具箱）
- 公共能力收拢到 `modules/base.py`：配置查找、数据目录、JSON、数值转换、OneBot 客户端
- 所有跨模块重名方法加模块前缀，消除多继承静默覆盖
- 配置按 `complain / usage / voice / toolbox` 四命名空间隔离，同名键不再串用
- 启动时自动把子模块 Handler 与 LLM 工具归位到 main 模块路径，保证事件分发与工具绑定生效

### 新增

- 统一可视化控制台 `views/suite/index.html`：总览、告状、用量、语音、QQ 工具箱、记录六个页签
- Web API（主名 + 两个兼容别名三前缀）：状态、配置读取/保存、动作触发、语音转文字、记录
- schema 驱动的配置表单：分组折叠、开关、下拉、滑块、列表、密钥掩码
- 旧四个插件配置自动迁移到新命名空间，只覆盖仍为默认值的项
- 新增 `qq_change_avatar` 工具：说「换头像」并附图即可调用 NapCat 的 `set_qq_avatar`
- 「触发说说提醒的概率」改为百分比（0-100），旧小数配置自动换算
- 视图桥显式提前加载并加超时兜底，修复控制台一直显示演示模式的问题

### 兼容

- 保留四个模块原有指令、LLM 工具、频控、冷却、每日上限、文案与数据文件
- 告状、用量、语音、工具箱历史数据目录不变，可直接沿用
- Python 3.13+ 通过 `audioop-lts` 补齐 `audioop`，避免 Silk 转码失败
