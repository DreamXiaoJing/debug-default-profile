# debug-default-profile

**中文** · [English](README.en.md)

一个脚本，两个给**官方**（非自编译）Chrome / Edge 打的二进制补丁：

1. `patch_browser.py debug-port` —— 让 Chrome / Edge 在「默认用户数据目录」下也能开启远程调试端口（CDP）。
2. `patch_browser.py no-debugger` —— 让浏览器忽略 JS 源码里的 `debugger` 语句（反调试「无限 debugger」失效）。

两个子命令互相独立，可以只用其中一个，也可以都打。补丁点不同、备份后缀不同
（`.orig.bak` / `.nodebug.bak`），互不覆盖。

## 这是什么

Chromium 系浏览器默认禁止在默认用户数据目录下绑定 `--remote-debugging-port` / `--remote-debugging-pipe`。想开端口，必须显式指定一个非默认的 `--user-data-dir`。

这个项目把浏览器二进制里那道校验的几条跳转指令 NOP 掉，让你用默认目录也能直接开调试端口。不用每次起隔离的 profile，自动化 / 调试 / 测试更顺手。

## 原理（30 秒版）

- 源码里是 `IsRemoteDebuggingAllowed()`，一个 `std::optional<bool>` 决定是否放行，默认「算不出来就当默认目录，直接拒绝」。
- 编译进 `chrome.dll` / `msedge.dll` 后，这段判断塌缩成几对指令：

```asm
mov   eax, 2
cmp   [has_value], 1
jne   失败
cmp   [value], 0
jne   失败
...放行
```

- 工具做的事就是把那几条 `jne` 抹成 `NOP`，让流程一路走到放行。

完整逆向过程见 [docs/cdp_user_data_dir_check.md](docs/cdp_user_data_dir_check.md)。
如果你是自编译 Chromium 的人，也可以直接用源码级补丁 [docs/disable_cdp_user_data_dir_check.patch](docs/disable_cdp_user_data_dir_check.patch)。

## 支持版本

**实际上是全版本通杀**，没有「只支持下面这几个版本」这回事：

- `no-debugger`：每次运行都从 V8 运行时函数表现场推导补丁点，浏览器更新后不用改任何代码。
- `debug-port`：已收录的版本走「写入前逐字节校验」；**没收录的版本由 `auto` 现场按闸门指令
  形状定位**，打完自动把补丁点写回 `patch_db.json`。所以新版本不用等作者更新——
  双击 `一键修复.bat` 就行。

下表只是**已经在真机上验证过的构建**（是记录，不是上限）：

<!-- version-table:start -->
| 浏览器 | 版本 | 目标文件 |
| --- | --- | --- |
| Chrome | 154.0.8037.98（当前） | chrome.dll |
| Chrome | 154.0.8037.93 | chrome.dll |
| Chrome | 154.0.8037.58 | chrome.dll |
| Edge | 154.0.4258.53（当前） | msedge.dll |
| Edge | 154.0.4258.48 | msedge.dll |
| Edge | 154.0.4258.37 | msedge.dll |
<!-- version-table:end -->

浏览器自动更新后（Edge / Chrome 会直接换掉版本目录，旧补丁随之失效），跑
`python patch_browser.py auto --all`，或者直接双击 `一键修复.bat`。

这张表由 `patch_db.json` 生成：**收录新版本时工具会自动把上表刷新掉**（README 不在、或没有
`version-table` 标记就跳过，不影响补丁）；想手动刷新：

```shell
python patch_browser.py debug-port --list --markdown --update-md
```

只打印不写文件就去掉 `--update-md`。

认不出来的情况也有：闸门那几行代码被大版本重写过、骨架变了，工具会**明确报「没找到闸门指令」
并拒绝写入**（不会乱猜、不会打坏文件）；这种构建照 docs/ 的手工流程定位，把补丁点加进
`patch_db.json` 即可。`--locate` 认的是编译后的指令骨架，所以只要骨架没变，新旧构建都能认，
小版本更新（补丁点整体挪位）更是不在话下。

## 参数文件 `patch_db.json`

版本更新后会变的参数都在脚本旁边的 `patch_db.json` 里，改它就行，不用动 `patch_browser.py`：

```json
"brands": {
  "chrome": { "dll": "chrome.dll", "roots": ["C:\\Program Files\\Google\\Chrome\\Application", "…"] }
},
"patch_db": {
  "chrome": {
    "154.0.8037.93": {
      "0x24A6383": "0F 85 FB 00 00 00",
      "0x24A6391": "0F 85 ED 00 00 00"
    }
  }
}
```

- `brands`：品牌 → dll 文件名 + 安装搜索根目录（新增品牌只动这里）。
- `patch_db`：`debug-port` 的补丁点，每个版本 = `{ "文件偏移(hex)": "原始字节(hex)" }`。
  原始字节用于写入前校验，对不上就拒绝写入；**只追加、不删旧版本条目**（旧版本是回滚与取证依据）。
- 不用手写：`debug-port --locate --save` 会把新定位到的补丁点写回这个文件；
  `auto` 在没收录的版本上打完补丁也会自动收录。
- `no-debugger` 的补丁点每次运行自己推导，不存在这个文件里。

## 跑起来

要求：**Windows 10 / 11 + Python 3.11 或更高**。纯标准库，**没有依赖，不需要 `pip install`**，
也没有编译步骤——克隆下来就能跑。

```shell
git clone https://github.com/DreamXiaoJing/debug-default-profile.git
cd debug-default-profile
python patch_browser.py
```

最后那条是**自检**：只读、不需要管理员，会把两个补丁 × 两个浏览器的状态打一遍，再告诉你要做什么。
真要去改浏览器（比如更新后补丁失效）：

```shell
一键修复.bat
```

双击也行——它会弹 UAC 提权、必要时问你要不要关掉浏览器，然后自动重打两个补丁；
命令行等价写法是 `python patch_browser.py auto --all`。

不想保持一个仓库也无所谓：整个工具就是 `patch_browser.py` + `patch_db.json` 两个文件，
拷到任何目录都能跑（`一键修复.bat` 是可选的便利入口）。

## 用法

命令统一是 `python patch_browser.py <子命令> [参数]`，子命令只有三个：

| 子命令 | 作用 | 备份后缀 |
| --- | --- | --- |
| `debug-port`（别名 `cdp`） | 默认用户数据目录下也能开 CDP 端口 | `.orig.bak` |
| `no-debugger`（别名 `nodebug`） | 忽略 JS 的 `debugger` 语句 | `.nodebug.bak` |
| `auto` | 只给一个路径（或 `--all` 扫所有品牌的最新版），其余全部自己推导 | 同上 |

**最省事：双击 [一键修复.bat](一键修复.bat)。** 浏览器自动更新后两个补丁都会失效，双击它即可：
弹 UAC 提权 → 必要时问你要不要关掉浏览器 → 自动把 Chrome / Edge **最新版**的两个补丁重新打好，
新版本会现场定位补丁点并收录进 `patch_db.json`。等价于 `python patch_browser.py auto --all`。
只想看它打算做什么（不写任何文件、不需要管理员）：

```shell
一键修复.bat --dry-run
```

两个补丁子命令的参数完全一致，下面以 `debug-port` 为例，换成 `no-debugger` 即可。

路径都懒得找？`auto` 只吃一个路径 —— `chrome.dll` / `msedge.dll`、浏览器 exe、`Application`
目录或版本目录都行 —— 品牌、版本、补丁点全部自己推导：没收录的版本现场按闸门形状定位，
打完还会把补丁点写回 `patch_db.json`；已经打过的直接跳过。先加 `--dry-run` 看它打算干什么：

```shell
python patch_browser.py auto --path "C:\Program Files\Google\Chrome\Application\chrome.exe" --dry-run
python patch_browser.py auto --path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

只想处理其中一个补丁，加 `--only debug-port` 或 `--only no-debugger`。
不想给路径就用 `--all`：扫 `patch_db.json` 里的所有品牌，各处理本机最新已安装版本
（`一键修复.bat` 走的就是这条），本机同时留着的旧版本目录会被跳过并提示。

不知道现在是什么状态？**直接不带子命令运行**，它会把两个补丁 × 两个浏览器体检一遍
（安装版本、补丁状态、备份、写权限、浏览器是否还开着），再打印下一步该敲哪条命令和常用教程。
全程只读，不写任何文件，也不需要管理员：

```shell
python patch_browser.py
```

先关掉对应的浏览器，然后以管理员运行：

```shell
python patch_browser.py debug-port --browser chrome
python patch_browser.py debug-port --browser edge
```

`Program Files` 下 `Users` 只有读权限，所以**必须提权**：不是管理员、或浏览器没关，
工具会在写入前停下并说明原因，不会留下半个备份或半个补丁。
不确定权限够不够，先跑 `--dry-run`，它最后会打印一行「写入权限：可写 / 不可写」。
（若 dll 已经打过补丁，它会提前停下并说明，不会打印这一行。）

只校验、不写入：

```shell
python patch_browser.py debug-port --browser chrome --dry-run
```

回滚：

```shell
python patch_browser.py debug-port --browser chrome --restore
```

已打过补丁的 dll 再跑一次不会重复写入，会直接提示「该 dll 已打过补丁：N 个补丁点均为 NOP」。
所以 `--dry-run` 可以直接当健康检查用，它有两种「正常」结果：

| 输出 | 含义 | 要做什么 |
| --- | --- | --- |
| `该 dll 已打过补丁：N 个补丁点均为 NOP` | 补丁在位 | 什么都不用做 |
| `dry-run：N 个补丁点全部匹配，未写入` | 版本已支持，但补丁没打（浏览器更新后重新编译了） | 提权重跑一次正式打补丁 |

只有报 `校验失败，偏移 0x...` 才需要处理：说明这个构建还没收录，先 `--locate` 重新定位补丁点。

其他参数（两个子命令通用）：

- `--path` 直接指定 dll 路径（也可以只给 `--path`，品牌按文件名判断）
- `--version` 指定版本号
- `--backup-dir` 备份放哪（默认放 dll 旁边）
- `--no-backup` 不生成备份
- `--list` 列出支持版本和本机检测结果
- `--locate` 只读扫描 dll，打印补丁点；`debug-port` 的输出可直接贴进 `PATCH_DB`

验证：启动浏览器加 `--remote-debugging-port=9222`，访问 http://127.0.0.1:9222/json/version。

## 新增一个版本

**小版本自动更新（最常见）**：闸门那几行代码几乎不变，只是整体挪了位置。

```shell
python patch_browser.py debug-port --browser chrome --locate
python patch_browser.py debug-port --browser edge --locate
```

它会打印形如 `"0x24A6383": "0F 85 FB 00 00 00",` 的补丁点（JSON 片段），整段贴进
`patch_db.json` 里对应品牌的版本段，然后 `--dry-run` 校验。不想手贴就加 `--save`：

```shell
python patch_browser.py debug-port --browser chrome --locate --save
python patch_browser.py debug-port --browser chrome --dry-run
```

更省事的是 `auto`，定位、校验、备份、写入、收录一条龙：

```shell
python patch_browser.py auto --path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

`--locate` 认的是编译后的指令骨架
（`mov eax,2` → `cmp byte [rsp+d],1` → `jne` → `cmp byte [rsp+d],0` → `jne`），
栈偏移和跳转位移都用通配符跳过，所以位置变了也认得出来。
扫到的闸门**已经是 NOP** 时，它会直接告诉你「该 dll 已打过补丁」，不会再误报成「没找到闸门指令」。
dll 名和搜索根目录在 `patch_db.json` 的 `brands` 里维护一份，新增品牌改那里。
如果它报「没找到闸门指令」，说明大版本改动了这块代码，走下面的手工流程。

**大版本 / 代码结构变了**：

1. 打开新版本，确认报错仍在。
2. 在 dll 里搜报错字符串，反查引用（RIP 相对寻址 xref），反汇编对应上下文。
3. 找到那几对 `cmp` + `jne`，记下文件偏移和原始字节。
4. 加进 `patch_db.json` 的 `patch_db` 里，跑 `--dry-run` 校验。

更细的定位方法见 [docs/cdp_user_data_dir_check.md](docs/cdp_user_data_dir_check.md)。

## 第二个子命令：忽略 JS 的 `debugger` 语句

反爬站点常把 `debugger;` 塞进热循环、`getter`、事件回调。只要 DevTools 连着，执行流就在那里
断住，页面卡死、工具都点不动。这个子命令改浏览器二进制，让它从此变成空操作。

### 原理（30 秒版）

`debugger;` 不管有没有被 JIT，最终都调用同一个运行时函数
`Runtime_HandleDebuggerStatement`（解释器走 `Bytecode::kDebugger`，
优化后走 TurboFan 的 `JSDebugger` → `LowerJSDebugger()`）。函数体里只有一道闸门：

```cpp
if (isolate->debug()->break_points_active()) { …HandleDebugBreak… }
return isolate->stack_guard()->HandleInterrupts();
```

编译后是一条 `cmp byte ptr [rcx+disp], 1` + `jne <跳过>`。把这条 `jne` 换成**等长**的 `jmp`，
`if` 体就永远被跳过 —— 文件长度不变，尾部 `HandleInterrupts()` 照常执行。

**手动行号断点不受影响**：那走 `Runtime::kDebugBreakOnBytecode`，是另一条路。

### 怎么在一个没符号的 289 MB dll 里定位

不靠符号、不靠硬编码偏移：V8 的 `kIntrinsicFunctions[]` 表项里有函数名字符串指针
（`Runtime::Function` 的结构是 `{id, type, name*, entry*, nargs, result_size}`，x64 下 32 字节）。
所以流程是：搜 `"HandleDebuggerStatement\0"` 的 VA → 找引用它的 8 字节指针 → 表项 `+16` 就是函数地址。
表项前后还用「id 严格 ±1 连续 + type ∈ {0,1} + name 可读 + entry 在 `.text`」做结构校验。

**优点：每次运行都重新推导，浏览器自动更新后一般不用改代码。**

### 用法

先关掉对应的浏览器，然后以管理员运行：

```shell
python patch_browser.py no-debugger --browser chrome
python patch_browser.py no-debugger --browser edge
```

只定位与校验、不写入：

```shell
python patch_browser.py no-debugger --browser chrome --dry-run
python patch_browser.py no-debugger --list          # 列出各版本当前状态
```

回滚（后缀是 `.nodebug.bak`，不会覆盖 CDP 工具的 `.orig.bak`）：

```shell
python patch_browser.py no-debugger --browser chrome --restore
```

参数与 `debug-port` 一致：`--path` / `--version` / `--backup-dir` / `--no-backup` / `--locate` / `--list`。

### 验证

`debugger` 在没有 DevTools 连接时本来就是空操作，**只看端口通不通是假 PASS**，
必须让调试器真正附加上（DevTools 一打开就会 `Debugger.enable`）再看有没有断住。手工验证：

1. 关掉浏览器，带调试端口启动，然后按 F12 打开 DevTools：

   ```shell
   chrome.exe --remote-debugging-port=9222
   ```

2. 在 Console 里执行 `(function(){debugger;return 42})()`：
   - **已打补丁**：直接返回 `42`，Sources 面板不断住；
   - **未打补丁**（对照）：Sources 面板断在 `debugger` 那一行。
3. 回归检查：在 Sources 里给任意函数手动打个行号断点，触发它 —— 仍然应该断住，
   说明手动断点这条路没被误伤。

实测记录（2026-10-01，在整份复制的副本上做 A/B，不动真实安装）：未打补丁 `paused:true`（对照），
已打补丁 `paused:false / 返回 42`，手动断点仍 `paused:true @ bpTarget:1`，回滚后恢复 `paused:true`。
字节差异只有 1 个：`0x71D1AD2: 0x75 -> 0xEB`。

完整逆向过程与日常维护见 [docs/no_debugger_statement.md](docs/no_debugger_statement.md)。

## 社区

**9222 社区** —— 官方 Chrome / Edge 调试补丁。QQ 群：**799741829**（群名「9222 社区 · 浏览器调试」）。

- 提问前先跑一次自检，把输出一起贴上来，能省掉大半来回：

  ```shell
  python patch_browser.py
  ```

- 浏览器更新后补丁失效，双击 `一键修复.bat` 即可，不用在群里问。
- 群里解决过的问题会整理进 GitHub Discussions；仓库里的 README / docs 才是最终依据。
- 群规：只用于自己的机器和你有授权的环境；不聊破解他人系统、不聊未授权抓取。

## 免责声明

- 只用于你自己的浏览器、你自己的机器。
- 只为了本地调试 / 自动化 / 测试，别用于他人设备，也别在你不拥有授权的环境里用。
- **CDP 端口没有任何认证**：能访问 `127.0.0.1:<port>` 的本机进程就等于拿到浏览器的完全控制权
  （包括默认 profile 里的 Cookie 和登录态）。别把端口绑到局域网 / 公网，别用
  `--remote-allow-origins=*`，调试完就关掉浏览器。详见
  [docs/cdp_user_data_dir_check.md](docs/cdp_user_data_dir_check.md) 6.6 的实测记录。
- 打补丁改的是原文件，浏览器自动更新后补丁会失效，需要重新打。
- 用前看清风险，自行承担后果。

## License

[MIT](LICENSE) © 2026 DreamXiaoJing
