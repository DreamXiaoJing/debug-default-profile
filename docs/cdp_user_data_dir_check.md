# Chromium 154 — CDP 端口「必须使用非默认用户数据目录」校验代码定位

源码根：`D:\Chrome\chromium154\src`
目标版本：Chromium 154.0.8037.44

> **状态：该判断已按需求移除（2026-09-22）。**
> 改动文件 `chrome/browser/devtools/remote_debugging_server.cc`，留档补丁
> `D:\Chrome\disable_cdp_user_data_dir_check.patch`。详见文末第 6 节。
> 下方 1–5 节记录的是**改动前**的上游原始实现，供回滚/对照使用。

---

## 1. 结论速查表

| 作用 | 文件 | 行号 |
| --- | --- | --- |
| 错误枚举定义 | `chrome/browser/devtools/remote_debugging_server.h` | 28–36 |
| 校验函数（真正的闸门） | `chrome/browser/devtools/remote_debugging_server.cc` | 160–183 |
| 校验调用点 ①（pipe） | 同上 | 353–361 |
| 校验调用点 ②（port） | 同上 | 363–395 |
| 启动总入口 | `chrome/browser/browser_process_impl.cc` | 1085–1115 |
| stderr 错误文案 | `chrome/browser/browser_process_impl.cc` | 1106–1112 |
| 「是否默认目录」判定 | `chrome/common/chrome_paths.cc` | 523–539 |
| 默认目录的物理位置（Windows） | `chrome/common/chrome_paths_win.cc` | 42–48 |
| 开关：政策允许远程调试 | `chrome/common/pref_names.h` | 1766–1767 |
| 政策注册（默认 true） | `chrome/browser/browser_process_impl.cc` | 1372 |
| 企业政策名 `RemoteDebuggingAllowed` | `chrome/browser/policy/configuration_policy_handler_list_factory.cc` | 869–871 |

---

## 2. 闸门本体 — `IsRemoteDebuggingAllowed()`

`chrome/browser/devtools/remote_debugging_server.cc:160`

```cpp
// Returns true, or a reason why remote debugging is not allowed.
base::expected<bool, RemoteDebuggingServer::NotStartedReason>
IsRemoteDebuggingAllowed(const std::optional<bool>& is_default_user_data_dir,
                         PrefService* local_state) {
  if (!local_state->GetBoolean(prefs::kDevToolsRemoteDebuggingAllowed)) {
    return base::unexpected(
        RemoteDebuggingServer::NotStartedReason::kDisabledByPolicy);
  }
#if BUILDFLAG(IS_WIN) || BUILDFLAG(IS_MAC) || BUILDFLAG(IS_LINUX)
#if BUILDFLAG(GOOGLE_CHROME_BRANDING)
  constexpr bool default_user_data_dir_check_enabled = true;
#else
  const bool default_user_data_dir_check_enabled =
      g_enable_default_user_data_dir_check_for_chromium_branding_for_testing;
#endif

  if (default_user_data_dir_check_enabled &&
      is_default_user_data_dir.value_or(true)) {
    return base::unexpected(
        RemoteDebuggingServer::NotStartedReason::kDisabledByDefaultUserDataDir);
  }
#endif  // BUILDFLAG(IS_WIN) || BUILDFLAG(IS_MAC) || BUILDFLAG(IS_LINUX)
  return true;
}
```

注意两点：
- `is_default_user_data_dir.value_or(true)` —— 判定失败（返回 `std::nullopt`）时**按"是默认目录"处理，即拒绝**，fail-closed。
- Android 平台编译时整段检查被 `#if` 排除，不生效。

---

## 3. 「是否默认目录」判定 — `IsUsingDefaultDataDirectory()`

`chrome/common/chrome_paths.cc:523`

```cpp
std::optional<bool> IsUsingDefaultDataDirectory() {
  if (g_override_using_default_data_directory_for_testing.has_value()) {
    return g_override_using_default_data_directory_for_testing.value();
  }

  base::FilePath user_data_dir;
  if (!base::PathService::Get(chrome::DIR_USER_DATA, &user_data_dir)) {
    return std::nullopt;
  }

  base::FilePath default_user_data_dir;
  if (!chrome::GetDefaultUserDataDirectory(&default_user_data_dir)) {
    return std::nullopt;
  }

  return user_data_dir == default_user_data_dir;
}
```

`chrome/common/chrome_paths_win.cc:42`（Windows 上的"默认目录"定义）：

```cpp
bool GetDefaultUserDataDirectory(base::FilePath* result) {
  if (!base::PathService::Get(base::DIR_LOCAL_APP_DATA, result))
    return false;
  *result = result->Append(install_static::GetChromeInstallSubDirectory());
  *result = result->Append(chrome::kUserDataDirname);
  return true;
}
```

即 `%LOCALAPPDATA%\Google\Chrome\User Data`（品牌名由 `GetChromeInstallSubDirectory()` 决定），
与当前实际生效的 `DIR_USER_DATA`（会被 `--user-data-dir` 覆盖）做**字符串全等比较**。

测试用覆盖开关：`chrome/common/chrome_paths.cc:48 / 541-544`，声明在 `chrome_paths_internal.h:32-36`。

---

## 4. 错误如何抛出（stderr 文案）

`chrome/browser/browser_process_impl.cc:1085` 起，`GetInstance()` 返回 `unexpected` 后：

```cpp
  switch (maybe_remote_debugging_server.error()) {
    case RemoteDebuggingServer::NotStartedReason::kNotRequested:
      break;
    case RemoteDebuggingServer::NotStartedReason::kDisabledByPolicy:
      fprintf(stderr, "%s",
          "\nDevTools remote debugging is disallowed by the system admin.\n");
      fflush(stderr);
      break;
    case RemoteDebuggingServer::NotStartedReason::kDisabledByDefaultUserDataDir:
      fprintf(stderr, "%s",
          "\nDevTools remote debugging requires a non-default data directory. "
          "Specify this using --user-data-dir.\n");
      fflush(stderr);
      break;
  }
```

枚举定义在 `remote_debugging_server.h:28-36`：

```cpp
  enum class NotStartedReason {
    kNotRequested,
    kDisabledByPolicy,
    kDisabledByDefaultUserDataDir,
  };
```

---

## 5. 关键工程结论

1. **该检查默认只对 Google Chrome 官方品牌开启**（`BUILDFLAG(GOOGLE_CHROME_BRANDING) == true`）。
   自行编译的 Chromium 品牌下 `g_enable_default_user_data_dir_check_for_chromium_branding_for_testing`
   全局默认 `false`（`remote_debugging_server.cc:51-52`），**校验被短路**，
   直接用默认用户数据目录开 `--remote-debugging-port` 不会被拦。

2. 若要给自编译 Chromium 也打开该检查做回归测试，调用：
   `RemoteDebuggingServer::EnableDefaultUserDataDirCheckForTesting()`（`remote_debugging_server.cc:292`）。

3. 规避/改造的三条路径：
   - 命令行加 `--user-data-dir=<非默认路径>`（不改代码）；
   - 自编译 Chromium 品牌（默认不启用该检查）；
   - 修改 `remote_debugging_server.cc:176` 的 `if` 条件（硬改）。

4. 校验发生在 **Chrome 层**（`chrome/browser/...`），content 层
   `content/browser/devtools/devtools_http_handler.cc` 不做此判断，
   因此不能靠 content 层绕开。已核实 `content/browser/devtools/` 目录内
   不存在任何 `user_data_dir` 相关闸门。

---

## 6. 已应用的改动（2026-09-22）

**目标**：允许 `--remote-debugging-port` / `--remote-debugging-pipe` 在
**默认用户数据目录**下也生效。

**改动文件**：`chrome/browser/devtools/remote_debugging_server.cc`

### 6.1 闸门函数（原 160–183 行 → 现 165–178 行）

删除前：

```cpp
base::expected<bool, RemoteDebuggingServer::NotStartedReason>
IsRemoteDebuggingAllowed(const std::optional<bool>& is_default_user_data_dir,
                         PrefService* local_state) {
  if (!local_state->GetBoolean(prefs::kDevToolsRemoteDebuggingAllowed)) {
    return base::unexpected(
        RemoteDebuggingServer::NotStartedReason::kDisabledByPolicy);
  }
#if BUILDFLAG(IS_WIN) || BUILDFLAG(IS_MAC) || BUILDFLAG(IS_LINUX)
#if BUILDFLAG(GOOGLE_CHROME_BRANDING)
  constexpr bool default_user_data_dir_check_enabled = true;
#else
  const bool default_user_data_dir_check_enabled =
      g_enable_default_user_data_dir_check_for_chromium_branding_for_testing;
#endif

  if (default_user_data_dir_check_enabled &&
      is_default_user_data_dir.value_or(true)) {
    return base::unexpected(
        RemoteDebuggingServer::NotStartedReason::kDisabledByDefaultUserDataDir);
  }
#endif  // BUILDFLAG(IS_WIN) || BUILDFLAG(IS_MAC) || BUILDFLAG(IS_LINUX)
  return true;
}
```

删除后：

```cpp
// Returns true, or a reason why remote debugging is not allowed.
//
// [PATCH] The "remote debugging requires a non-default user data directory"
// check has been removed: --remote-debugging-port and --remote-debugging-pipe
// are now allowed to bind the DevTools endpoint while the default user data
// directory is in use. The enterprise policy gate below is kept on purpose.
base::expected<bool, RemoteDebuggingServer::NotStartedReason>
IsRemoteDebuggingAllowed(PrefService* local_state) {
  if (!local_state->GetBoolean(prefs::kDevToolsRemoteDebuggingAllowed)) {
    return base::unexpected(
        RemoteDebuggingServer::NotStartedReason::kDisabledByPolicy);
  }
  return true;
}
```

### 6.2 其余同步改动

| 位置 | 改动 |
| --- | --- |
| 全局变量（原 51–52 行） | 加 `[[maybe_unused]]` + 注释，说明已失效；保留以维持 `EnableDefaultUserDataDirCheckForTesting()` 的 API/链接兼容 |
| `GetInstance()` 原 347–348 行 | 删除 `std::optional<bool> is_default_user_data_dir = chrome::IsUsingDefaultDataDirectory();` |
| `GetInstance()` 原 355 行（pipe 分支） | `IsRemoteDebuggingAllowed(is_default_user_data_dir, local_state)` → `IsRemoteDebuggingAllowed(local_state)` |
| `GetInstance()` 原 385 行（port 分支） | 同上 |

### 6.3 故意保留的死代码（便于回滚，不扩大 diff）

- `remote_debugging_server.h:35` 枚举值 `kDisabledByDefaultUserDataDir`
- `browser_process_impl.cc:1106-1112` 对应的 stderr 分支
- `remote_debugging_server.cc:287-289` `EnableDefaultUserDataDirCheckForTesting()`
- `chrome/common/chrome_paths.*` 的 `IsUsingDefaultDataDirectory()` ——
  仍被 `app_bound_encryption_win.cc`、`profile_shortcut_manager_win.cc`、
  `web_app_uninstallation_via_os_settings_registration_win.cc` 使用，**不可删**

### 6.4 回滚方式

```bat
cd /d D:\Chrome\chromium154\src
git checkout -- chrome/browser/devtools/remote_debugging_server.cc
```

### 6.5 重新打补丁

`gclient sync` 或切版本后工作树会被重置，需重新应用：

```bat
cd /d D:\Chrome\chromium154\src
git apply D:\Chrome\disable_cdp_user_data_dir_check.patch
```

### 6.6 ⚠️ 安全提醒

默认用户数据目录开放 CDP 意味着**本机任意进程**都能连上 `127.0.0.1:<port>`
读取完整 profile（Cookie、登录态、扩展数据）。补丁只是解除软件限制，
不改变这一事实。仍建议只用专用 `--user-data-dir` 目录跑调试实例。

---

## 7. 二进制补丁点（各版本）

补丁内容是把下面这些条件跳转整条 NOP 掉（6 字节 `0F 85 rel32` → 6 个 `90`，
2 字节 `75 rel8` → 2 个 `90`）。

### 7.1 Chrome `chrome.dll`

| 版本 | 补丁点（文件偏移, 原始字节） |
| --- | --- |
| 154.0.8037.93 | `0x24A6383 0F 85 FB 00 00 00`、`0x24A6391 0F 85 ED 00 00 00`、`0x24A6574 75 19`、`0x24A657E 75 0F` |
| 154.0.8037.58 | `0x256A7D3 0F 85 FB 00 00 00`、`0x256A7E1 0F 85 ED 00 00 00`、`0x256A9C4 75 19`、`0x256A9CE 75 0F` |

### 7.2 Edge `msedge.dll`

| 版本 | 补丁点（文件偏移, 原始字节） |
| --- | --- |
| 154.0.4258.48 | `0x315D91B 0F 85 3D 59 79 01`、`0x315D929 0F 85 2F 59 79 01`、`0x48F346D 75 E6`、`0x48F3477 75 DC`、`0x48F38B9 75 75`、`0x48F38C3 75 6B` |
| 154.0.4258.37 | `0x31607BB 0F 85 E7 07 79 01`、`0x31607C9 0F 85 D9 07 79 01`、`0x48F11B7 75 E6`、`0x48F11C1 75 DC`、`0x48F1603 75 75`、`0x48F160D 75 6B` |

### 7.3 这些点为什么是对的

`GetInstance()` 里 pipe / port 两个分支各自内联了一份闸门，所以每个 dll 里
会出现 2 组（Chrome）或 3 组（Edge）同样的指令：

```
mov  eax, 2                     ; NotStartedReason::kDisabledByDefaultUserDataDir
cmp  byte ptr [rsp + d1], 1     ; std::optional::has_value()
jne  <写错误码>                  ; 算不出来 -> 按「是默认目录」处理，fail-closed
cmp  byte ptr [rsp + d2], 0     ; 目录是否等于默认目录（1 = 是）
jne  <写错误码>
<构造 RemoteDebuggingServer，即放行>
```

两条 `jne` 的目标都是同一段「把 `eax`(=2) 写进外层 expected 的错误槽」的代码，
所以 NOP 掉之后控制流直接落到放行分支。

### 7.4 怎么在自动更新后重新定位（2026-09-30 用的方法）

`.58 → .93`、`.37 → .48` 都是小版本更新：**机器码逐条相同，只是整体挪了位置**
（Chrome 整体前移 `0xC4450`，Edge 的 `0x315D…` 段前移 `0x2EA0`、`0x48F…` 段后移 `0x22B6`；
`call` / `jne` 的相对位移随之变化，其余字节不变）。

做法是把上面那段骨架做成带通配符的字节模式（栈偏移 `d1/d2` 和跳转位移都用 `..`
跳过），在该 dll 里扫一遍 —— 工具里就是 `--locate`：

```
python patch_debug_port.py --browser chrome --locate
python patch_debug_port.py --browser edge --locate
```

定位的交叉验证方式：

1. 用 `--locate` 扫**旧版本**（`.58` / `.37`）的原始 dll，输出必须与表中旧版本的
   补丁点完全一致 —— 说明模式没有误匹配。
2. 扫新版 dll 得到的偏移，用反汇编逐条比对新旧两版的上下文：除 `call` / 跳转位移外
   指令序列完全相同。
3. 加进 `PATCH_DB` 后 `--dry-run` 必须通过（写入前逐字节校验原始字节）。
4. 拿 dll 副本跑一遍「打补丁 → 校验 → 回滚」，回滚后 SHA256 必须与原始文件一致。

手工定位的参考 dll（保留在 `D:\Chrome\`）：
`chrome.dll.154.0.8037.58.orig.bak`、`msedge.dll.154.0.4258.37.orig.bak`。

### 7.5 两个工程细节

- Edge 的 `Application\<版本>\msedge.dll` 与 `EdgeCore\<版本>\msedge.dll` 是**同一个
  文件的硬链接**，改一个两个都变；`--restore` 用备份覆盖时也不会把它们拆开成两份。
- 打补丁要写 `Program Files`，必须**管理员**运行且**先关掉浏览器**（`Users` 组只有
  读取权限；浏览器运行时 dll 被映射也写不进去）。
