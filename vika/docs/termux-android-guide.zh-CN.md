# 在 Android 手机上从零运行 Vika

这份指南使用 **原生 Termux/Bionic + Pi 或 Durable + 飞书**，不需要 root、PRoot 或 Docker。先让手机能收消息、调用模型并回复，再配置后台运行。

适用代码是 [nexw/OpenViking PR #3](https://github.com/nexw/OpenViking/pull/3) 的 `termux/vika-rebased` 分支，基于 Vika `0.5.0-preview`，不是上游正式发布包。PR 描述已报告 Android arm64 / Node 26.3.1 上安装及各服务就绪；其他设备、系统版本和完整消息往返仍应按本文逐项验证。

## 1. 开始前准备什么

| 项目 | 要准备的内容 |
| --- | --- |
| Android 设备 | 本指南以 Android 7+、64 位 ARM 设备为目标；已有实测记录是 arm64。为编译工具、Go/npm 缓存预留数 GB 存储空间 |
| 网络 | 能访问 Termux 软件源、GitHub、npm、Go 模块源，以及模型、OpenViking 和飞书服务 |
| 模型账号 | Pi 支持的模型订阅或 API Key；Pi/Durable 负责调用模型，安装 Vika 不会附送模型额度 |
| OpenViking 服务 | 手机可访问的服务 URL 及所需凭据。当前 Vika setup 必须通过 OV 检查，不能跳过 |
| 飞书账号 | 手机号注册或企业账号，并具备创建/使用机器人应用的权限；当前集成不支持豆包账号方式登录飞书 |

只有手机时，可以使用团队提供的 OpenViking 服务，或按 [OpenViking 快速开始](https://github.com/volcengine/OpenViking/blob/feat/vika/docs/zh/getting-started/02-quickstart.md) 准备托管服务。也可在另一台电脑/云主机部署服务，再让手机连接。本文不包含在 Termux 内编译 OpenViking 服务端。

先记下 OV 的 **URL、API Key，以及管理员要求的 Account/User/Agent 信息**。模型的 Key 与 OV 的 Key 用途不同。远端服务的 `127.0.0.1` 地址不能直接给手机使用：手机上的 `127.0.0.1` 指手机本身。

## 2. 安装 Termux 应用

建议按下面这一套来源安装：

1. 用手机浏览器打开 [F-Droid 的 Termux 页面](https://f-droid.org/packages/com.termux/)，选择版本下的 APK 下载并安装；不必先安装 F-Droid 客户端。
2. Android 询问时，允许当前浏览器安装应用。
3. 打开 Termux，等待首次初始化完成，看到命令提示符。

后面若需要开机启动，再从 **同一来源**安装 [Termux:Boot](https://f-droid.org/packages/com.termux.boot/)。不要混装 F-Droid 和 GitHub 来源的 Termux 插件，它们的签名不同。已有 Termux 数据时，不要为切换来源直接卸载应用。来源与兼容性说明见 [Termux 官方安装文档](https://github.com/termux/termux-app#installation)。

接下来所有终端命令都在 **Termux** 中执行。使用默认 Bash 即可。源码、安装目录和工作区放在 `$HOME` 下，不放到 `/sdcard` 或共享下载目录；后者的执行权限和符号链接行为不适合这套安装。

## 3. 安装系统依赖并检查环境

逐段执行，遇到错误先停下来处理，不要继续下一段：

```sh
pkg update
pkg upgrade
pkg install git nodejs-lts npm golang clang make pkg-config bash curl tar termux-services
```

这里用 Termux 提供的 Node，不能用 nodejs.org 的 Linux ARM64 压缩包。`nodejs-lts` 与 `nodejs` 是互斥包；如果已有能通过下列检查的 Termux Node，不必为了本文切换版本，只安装缺少的工具。包的实际版本以软件源为准，Vika 要求 Node **>=22.19.0**。[Termux Node 包定义](https://github.com/termux/termux-packages/blob/master/packages/nodejs-lts/build.sh)

```sh
node -p 'JSON.stringify({platform:process.platform,arch:process.arch,node:process.versions.node,exe:process.execPath})'
npm --version
go version
go env GOOS GOARCH
node --input-type=module -e 'import {DatabaseSync} from "node:sqlite"; const db=new DatabaseSync(":memory:"); console.log(db.prepare("SELECT 1 AS ok").get()); db.close();'
```

预期看到：Node 的 `platform` 是 `android`，本文目标设备的 `arch` 是 `arm64`；npm 和 Go 能显示版本；SQLite 返回 `ok: 1`。SQLite 的实验性提示本身不等于失败。Go 使用 Termux 原生工具链，版本至少满足 lark-cli 的 Go 1.23 要求；不要手动设置 `GOOS=linux` 来生成普通 Linux 包。

准备目录和 PATH：

```sh
mkdir -p "$HOME/src" "$HOME/.local/bin" "$HOME/vika-workspace"
export PATH="$HOME/.local/bin:$HOME/.vika/bin:$PATH"
```

让新开的默认 Bash 终端也能找到命令：

```sh
touch "$HOME/.bashrc"
grep -Fqx 'export PATH="$HOME/.local/bin:$HOME/.vika/bin:$PATH"' "$HOME/.bashrc" || printf '\nexport PATH="$HOME/.local/bin:$HOME/.vika/bin:$PATH"\n' >> "$HOME/.bashrc"
```

如果自行换成了 Zsh，将同一行加到 `~/.zshrc`。不要改写 `HOME` 或 `PREFIX`。

## 4. 获取 Vika 源码，编译原生 lark-cli

先取得带 Termux 适配的分支。稀疏检出只下载需要的 Vika 工作文件：

```sh
git clone --depth 1 --single-branch --filter=blob:none --sparse \
  --branch termux/vika-rebased https://github.com/nexw/OpenViking.git \
  "$HOME/src/OpenViking-termux"
git -C "$HOME/src/OpenViking-termux" sparse-checkout set vika
cd "$HOME/src/OpenViking-termux/vika"
```

如果目标目录已存在，先确认它的分支和本地改动，不要删目录或强制覆盖。若分支已改名/合并，以 PR #3 页面所示来源为准。

PR #3 对应的 lark-cli 版本是 **1.0.99**。已有可运行的同版本原生程序时，可以跳过编译，直接执行本节末尾检查。否则在手机上编译：

```sh
git clone --depth 1 --branch v1.0.99 https://github.com/larksuite/cli.git \
  "$HOME/src/lark-cli-v1.0.99"
cd "$HOME/src/lark-cli-v1.0.99"
make build VERSION=1.0.99
install -m 755 lark-cli "$HOME/.local/bin/lark-cli"
```

这里使用上游 [v1.0.99 Makefile](https://github.com/larksuite/cli/blob/v1.0.99/Makefile) 的构建入口，由 Termux Go/clang 生成本机程序。第一次会下载 Go 模块，编译时间取决于设备。出现模块下载或编译错误时，先保留首条错误解决，不能用 Linux 预编译包替代。

```sh
command -v node
command -v lark-cli
lark-cli --version
```

确认 `lark-cli` 指向刚安装的 `$HOME/.local/bin/lark-cli`，版本为 1.0.99。若 PATH 上还有其他同名程序，可明确指定：

```sh
export LARK_CLI_BIN="$HOME/.local/bin/lark-cli"
```

这版 Vika 安装器要求 `LARK_CLI_BIN` 是绝对路径；不设置时从 PATH 查找。Node 则使用启动安装器的那个 Node，之后写入启动脚本。

## 5. 安装 Vika

```sh
cd "$HOME/src/OpenViking-termux/vika"
node scripts/install-termux.mjs
vika --version
```

成功时会看到 Pi 和 Durable 检查通过，以及 `Installed ...`。安装器会把应用放到新的 `~/.vika/releases/termux-*` 目录，安装项目内固定的 Pi/Durable **1.5.0**，再创建 `~/.vika/current` 和启动命令。无需另外全局安装 Pi、Durable 或 PM2。

安装器本身不会启动服务，也不会下载 glibc Node 或 Slack CLI。不要改用普通 Linux 的 `curl ... install.sh`，也不要用 `npm run setup` 代替此入口。源码中的修改不会自动同步到已经安装的 `~/.vika/current`。

若提示 `An installation already exists`，说明已有安装。请按 [Termux 手动更新说明](termux.md#manual-updates) 处理，不要删除 `~/.vika` 来重新安装，那里还保存配置与会话数据。

## 6. 登录模型，并先验证 Pi

```sh
vika pi
```

在 Pi 界面内输入 `/login`，按所选 provider 的提示完成订阅登录或 API Key 配置；然后用 `/model` 选择可用模型。发送一句“只回复 OK”，确认模型确实能返回答案。编辑框为空时按 `Ctrl+D` 可退出 Pi。

这一步会实际调用模型。若需要浏览器登录，在手机浏览器打开界面给出的地址。自定义 API endpoint/provider 的配置见 [Pi 配置说明](https://github.com/earendil-works/pi/tree/main/packages/coding-agent#readme) 和 [Vika Durable 模型说明](pi-durable-runtime.md)。

Pi 和 Durable 共享 `~/.pi/agent` 的模型与登录配置，Durable 不需要再注册一套账号。后面启用 runit 时，不要只依赖当前终端临时 `export` 的模型 Key；服务启动不会保存这组临时变量。通过 Pi 登录保存配置，或使用 [Vika 支持的凭据配置](installation-reference.md)；不要把模型 Key 写进 `config.json`。

## 7. 运行 setup，连接 OV 和飞书

```sh
vika --lang zh
vika setup
```

按向导依次完成：

1. **OpenViking 连接**：选择已有配置或手工输入服务 URL 和凭据。不是手机上运行的服务，不要填写 `127.0.0.1:1933`。验证通过后才能继续。
2. **Agent**：设置名称，工作区填写刚才的 `$HOME/vika-workspace` 的实际绝对路径。需要查看路径时，在另一个 Termux 会话执行 `printf '%s\n' "$HOME/vika-workspace"`。运行时选择 Pi 或 Durable；建议先用刚验证过的 Pi。
3. **飞书**：按提示扫码创建/选择应用，或填写已有应用的 App ID 与 App Secret。同一台手机不方便扫自己屏幕上的码时，使用向导提供的授权链接（若有），或选已有应用方式；已有应用的准备见 [手动飞书配置](installation-reference.md#manual-feishu-setup)。
4. **授权与保存**：按向导确认需要的权限并保存。已有飞书应用需要启用机器人、长连接和消息事件，并完成发布/审批；不要让另一套程序同时监听同一个应用。

是否允许读取本人可见的消息，按实际用途在向导中选择；这与机器人收取自己的消息是不同的权限。手机号注册/企业账号要求以及具体授权步骤见 [安装指南](../install.md)。

当前 setup 保存后会尝试启动实例。首个实例通常为 `primary`；后面的命令若使用了自定义实例名，请相应替换。

## 8. 检查 Vika 确实跑起来了

```sh
vika start
vika status
```

`start` 会保留已经运行且就绪的实例。查看 `vika-primary` 和 `vika-web` 是否就绪；Termux 模式没有 `vika-updater` 是正常的。

打开手机上的 Web 控制台：

```sh
termux-open-url http://127.0.0.1:3737
```

也可以在手机浏览器直接输入这个地址。页面只监听本机；另一台设备访问自己的 `127.0.0.1` 不会打开手机的 Vika。

在飞书里找到机器人，发一条私聊，例如“请只运行 pwd，并回复当前工作目录”。确认机器人收到消息、执行工具并回复。再查看日志：

```sh
vika logs primary
```

`Ctrl+C` 只退出日志查看。成功标准是：**进程就绪、模型可用、飞书能收发、工具在指定工作区执行**。只看 PM2 的 online 状态还不够。

验证 Durable 时，可以回到 `vika setup` 编辑同一实例，切换运行时，再重复上述消息测试；不要为同一个飞书应用启动两个独立监听实例。

## 9. 可选：用 runit 管理后台运行

先完成上一节，确保手动运行正常。`termux-services` 安装后需要启动其 supervisor；新开一个 Termux shell 会自动启动，也可在当前默认 Bash 中执行：

```sh
. "$PREFIX/etc/profile.d/start-services.sh"
vika stop
vika autostart enable
sv status "$PREFIX/var/service/vika"
vika status
```

supervisor 启动和扫描服务目录需要一点时间；刚启用时还没有 `supervise/ok`，稍等再查。使用了自定义 `SVDIR` 时，服务路径改为 `$SVDIR/vika`。runit 负责前台 PM2，PM2 负责 Vika 的实例和 Web。Vika 私有 PM2 home 是 `~/.vika/pm2`，不用系统级 systemd 或 `sudo`。

停止并保持关闭：

```sh
vika autostart disable
```

恢复自动运行：

```sh
vika autostart enable
```

仅执行 `vika stop` 是停止应用，不等于关闭 runit 服务。不要把 `vika start` 重复加到 `.bashrc`，也不要再运行一套全局 `pm2 startup`。

## 10. 可选：开机启动和 Android 后台设置

“服务能在 Termux 中运行”与“手机重启后自动运行”是两件事。需要后者时：

1. 从与 Termux 相同的来源安装 **Termux:Boot**，并点开它一次。
2. 在 Android 应用设置中，按设备提供的选项允许 Termux/Termux:Boot 后台活动、自启动和通知，取消对它们的电池优化。厂商的菜单名称不同。
3. 在 Termux 创建启动脚本：

```sh
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-vika-services" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
. "$PREFIX/etc/profile.d/start-services.sh"
SH
chmod 700 "$HOME/.termux/boot/start-vika-services"
```

此脚本适用于标准包名、主用户安装的 Termux。已经有启动 termux-services 的 Boot 脚本时，不必再创建第二份。它启动服务管理器，已启用的 Vika runit 服务随之启动；不是重新安装 Vika。[Termux:Boot 官方用法](https://github.com/termux/termux-boot#how-to-use)

`termux-wake-lock` 有助于避免休眠，但会增加耗电，不能保证绕过厂商清理、强行停止或所有 Android 进程限制。需要释放时执行 `termux-wake-unlock`。测试重启后，重新打开 Termux 检查 `sv status`、`vika status`，并再发一条飞书消息。

## 11. 遇到问题怎么定位

| 现象 | 先检查什么 |
| --- | --- |
| `pkg` 下载失败 | 网络和软件源；可用 `termux-change-repo` 选择可用镜像，再运行 `pkg update` |
| Node 显示 `linux` 或缺少 `node:sqlite` | 是否用了 PRoot/普通 Linux Node；回到原生 Termux，检查 Node 来源和版本 |
| `lark-cli: not found`、文件存在却不能执行 | `command -v lark-cli`、`lark-cli --version`；确认它是在 Termux 原生编译的程序 |
| `vika: command not found` | 执行 `~/.vika/bin/vika --version`，再检查 PATH 和当前 shell 的 rc 文件 |
| 没有可用模型，或切换 runit 后认证失败 | 先运行 `vika pi` 验证登录与模型，确认凭据已保存，未只放在旧 shell 的临时变量中 |
| OV 检查失败 | 手机是否能访问服务的真实地址，是否填对端口、Key 和身份字段；不要把远端机器的 localhost 当成手机地址 |
| 飞书不收消息 | 机器人权限、消息事件、长连接、应用发布，以及是否还有另一套程序监听同一应用 |
| PM2 online，但机器人不回复 | `vika logs primary` 和 Web 活动记录；区分模型调用失败、OV 不可用与连接器错误 |
| `sv` 报无法打开 supervise/ok | termux-services 是否已启动；新开 shell 或 source start-services.sh，稍后重试 |
| 息屏后失联 | Android 电池/后台限制、Termux 是否被强行停止、网络是否被系统暂停 |
| `vika upgrade` 被拒绝 | 这是预期行为；Android 没有对应发布包，按 [手动更新流程](termux.md#manual-updates) 更新 |

反馈问题时提供：Android/Termux 来源、`node --version`、`lark-cli --version`、Vika 分支/commit、失败步骤及相关错误。不要附 API Key、App Secret、登录令牌或完整配置文件。

本文命令已按 PR #3 和各工具的官方构建/安装入口核对；文档编写环境不是 Android 真机。以每一步的检查结果为准，不把其他设备上的 ready 记录当作你的设备已经验证成功。
