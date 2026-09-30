# debug-default-profile

让 Chrome / Edge 在「默认用户数据目录」下也能开启远程调试端口（CDP）。

## 这是什么

Chromium 系浏览器默认禁止在默认用户数据目录下绑定 `--remote-debugging-port` / `--remote-debugging-pipe`。想开端口，必须显式指定一个非默认的 `--user-data-dir`。

这个项目把浏览器二进制里那道校验的几条跳转指令 NOP 掉，让你用默认目录也能直接开调试端口。不用每次起隔离的 profile，自动化 / 调试 / 测试更顺手。

## 原理（30 秒版）

- 源码里是 `IsRemoteDebuggingAllowed()`，一个 `std::optional<bool>` 决定是否放行，默认「算不出来就当默认目录，直接拒绝」。
- 编译进 `chrome.dll` / `msedge.dll` 后，这段判断塌缩成几对指令：

```
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

| 浏览器 | 版本 | 目标文件 |
| --- | --- | --- |
| Chrome | 154.0.8037.93（当前） | chrome.dll |
| Chrome | 154.0.8037.58 | chrome.dll |
| Edge | 154.0.4258.48（当前） | msedge.dll |
| Edge | 154.0.4258.37 | msedge.dll |

版本对不上（比如自动更新后）工具会拒绝写入，不会打坏文件。新增版本的方法见下文，
小版本自动更新后可以直接用 `--locate` 自动重新定位补丁点。

## 用法

先关掉对应的浏览器，然后以管理员运行：

```
python patch_debug_port.py --browser chrome
python patch_debug_port.py --browser edge
```

`Program Files` 下 `Users` 只有读权限，所以**必须提权**：不是管理员、或浏览器没关，
工具会在写入前停下并说明原因，不会留下半个备份或半个补丁。
不确定权限够不够，先跑 `--dry-run`，它最后会打印一行「写入权限：可写 / 不可写」。

只校验、不写入：

```
python patch_debug_port.py --browser chrome --dry-run
```

回滚：

```
python patch_debug_port.py --browser chrome --restore
```

其他参数：

- `--path` 直接指定 dll 路径
- `--version` 指定版本号
- `--backup-dir` 备份放哪（默认放 dll 旁边，后缀 `.orig.bak`）
- `--no-backup` 不生成备份
- `--list` 列出支持版本和本机检测结果
- `--locate` 只读扫描 dll，按「闸门指令形状」打印补丁点，输出可直接贴进 `PATCH_DB`

验证：启动浏览器加 `--remote-debugging-port=9222`，访问 http://127.0.0.1:9222/json/version。

## 新增一个版本

**小版本自动更新（最常见）**：闸门那几行代码几乎不变，只是整体挪了位置。

```
python patch_debug_port.py --browser chrome --locate
python patch_debug_port.py --browser edge --locate
```

它会打印形如 `(0x24A6383, "0F 85 FB 00 00 00")` 的补丁点，贴进 `patch_debug_port.py`
的 `PATCH_DB`（记住把 `roots` 填上），然后 `--dry-run` 校验：

```
python patch_debug_port.py --browser chrome --dry-run
```

`--locate` 认的是编译后的指令骨架
（`mov eax,2` → `cmp byte [rsp+d],1` → `jne` → `cmp byte [rsp+d],0` → `jne`），
栈偏移和跳转位移都用通配符跳过，所以位置变了也认得出来。
如果它报「没找到闸门指令」，说明大版本改动了这块代码，走下面的手工流程。

**大版本 / 代码结构变了**：

1. 打开新版本，确认报错仍在。
2. 在 dll 里搜报错字符串，反查引用（RIP 相对寻址 xref），反汇编对应上下文。
3. 找到那几对 `cmp` + `jne`，记下文件偏移和原始字节。
4. 加进 `patch_debug_port.py` 里的 `PATCH_DB`，跑 `--dry-run` 校验。

更细的定位方法见 [docs/cdp_user_data_dir_check.md](docs/cdp_user_data_dir_check.md)。

## 免责声明

- 只用于你自己的浏览器、你自己的机器。
- 只为了本地调试 / 自动化 / 测试，别用于他人设备，也别在你不拥有授权的环境里用。
- 打补丁改的是原文件，浏览器自动更新后补丁会失效，需要重新打。
- 用前看清风险，自行承担后果。

## License

MIT