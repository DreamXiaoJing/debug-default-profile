# 让官方 Chrome / Edge 忽略 JS 的 `debugger` 语句

日期：2026-10-01
工具：`patch_browser.py no-debugger`
适用：官方（非自编译）Chrome / Edge，x64

---

## 一、要解决什么

反爬站点里最常见的一招：在热循环、`getter`、事件回调里塞 `debugger;`。
只要开发者工具连着（`Debugger.enable` 之后），执行流就在那里断住 —— 页面卡死，
连 DevTools 都点不动。你越是想看它的代码，它越是不让你看。

自编译 Chromium 可以改 V8 源码解决（见 `bypass_debugger_statement.md`），
但日常用的官方浏览器改不了源码，只能**改二进制**。

---

## 二、`debugger;` 在 V8 里的两条路径，一个出口

| 代码状态 | 路径 |
| --- | --- |
| 未优化（Ignition 解释器） | `Bytecode::kDebugger` → `IGNITION_HANDLER(Debugger, …)` |
| 已优化（TurboFan） | 算子 `JSDebugger` → `JSGenericLowering::LowerJSDebugger()` |

两条路**最终都收敛到同一个运行时函数**：

```cpp
// interpreter-generator.cc
IGNITION_HANDLER(Debugger, InterpreterAssembler) {
  TNode<Context> context = GetContext();
  TNode<Object> result = CallRuntime(Runtime::kHandleDebuggerStatement, context);
  ClobberAccumulator(result);
  Dispatch();
}

// js-generic-lowering.cc
void JSGenericLowering::LowerJSDebugger(Node* node) {
  ReplaceWithRuntimeCall(node, Runtime::kHandleDebuggerStatement);
}
```

所以只要动这一个函数，`debugger;`、`Function('debugger')()`、`eval('debugger')`、
循环刷屏的无限断点 —— 全部一起失效。

函数本体（`runtime-debug.cc:177`，V8 15.4）：

```cpp
RUNTIME_FUNCTION(Runtime_HandleDebuggerStatement) {
  SealHandleScope shs(isolate);
  DCHECK_EQ(0, args.length());
  if (isolate->debug()->break_points_active()) {          // ← 唯一闸门
    isolate->debug()->HandleDebugBreak(
        kIgnoreIfTopFrameBlackboxed,
        v8::debug::BreakReasons({v8::debug::BreakReason::kDebuggerStatement}));
    if (isolate->debug()->IsRestartFrameScheduled()) {
      return isolate->TerminateExecution();
    }
  }
  return isolate->stack_guard()->HandleInterrupts();
}
```

**手动断点为什么不受影响**：DevTools 的行号断点走 `Runtime::kDebugBreakOnBytecode`
（`DEBUG_BREAK_BYTECODE_LIST` 那一组 handler），是另一条路，不经过这个函数。

---

## 三、怎么在一个没有符号的 289 MB dll 里找到这个函数

关键在 V8 自己留的「名册」：`v8/src/runtime/runtime.cc`

```cpp
static const Runtime::Function kIntrinsicFunctions[] = {
    FOR_EACH_INTRINSIC(F) FOR_EACH_INLINE_INTRINSIC(I)};
```

而 `v8/src/runtime/runtime.h` 里的结构体在 x64 下是 **32 字节**：

```cpp
struct Function {
  FunctionId    function_id;     // +0   int32
  IntrinsicType intrinsic_type;  // +4   int32  (0=RUNTIME, 1=INLINE)
  const char*   name;            // +8   -> "HandleDebuggerStatement"
  Address       entry;           // +16  -> 函数地址
  int8_t        nargs;           // +24
  int8_t        result_size;     // +25
};
```

于是定位链条是纯数据推导，**不需要符号、不需要 PDB、不硬编码偏移**：

1. 搜 `b"HandleDebuggerStatement\0"` 的 VA（两个官方 dll 里都恰好命中 1 处）；
2. 在只读段里找引用该 VA 的 8 字节指针 —— 那就是表项 `+8`；
3. 表项 `+16` 即函数入口地址。

### 结构校验（防止撞车）

只找到指针还不够，必须证明那确实是 `kIntrinsicFunctions[]`。取表项前后各 2 项（步长 32），
要求同时满足：

| 检查 | 期望 |
| --- | --- |
| `function_id` 连续性 | 相邻项严格 ±1（枚举与表同序） |
| `intrinsic_type` | ∈ {0, 1} |
| `name` 指针 | 指向只读段里一段 NUL 结尾的可读标识符 |
| `entry` 指针 | 落在可执行段（`.text`） |

实测两个 dll 都通过，且 `function_id=102`、`nargs=0`、`result_size=1` 与源码
`F(HandleDebuggerStatement, 0, 1)`（`runtime.h:181`）完全一致 —— 三份独立证据互证。

---

## 四、闸门长什么样

反汇编 `Runtime_HandleDebuggerStatement`（官方 Chrome 154.0.8037.93）：

```
               56                      push   rsi                    ; CLOBBER_DOUBLE_REGISTERS 的痕迹
               48 83 ec 30             sub    rsp, 0x30
               4c 89 c6                mov    rsi, r8                ; r8 = Isolate*
               48 8b 05 81 eb 48 0a    mov    rax, [rip+…]           ; __security_cookie
               48 31 e0                xor    rax, rsp
               48 89 44 24 28          mov    [rsp+0x28], rax
+0x17          49 8b 88 90 0e 01 00    mov    rcx, [r8+0x10e90]      ; isolate->debug_
+0x1E          80 79 0c 01             cmp    byte [rcx+0x0c], 1     ; break_points_active_
+0x22          75 2F                   jne    +0x53                  ; ★ 闸门
+0x24          ba 01 00 00 00          mov    edx, 1                 ; kIgnoreIfTopFrameBlackboxed
+0x29          41 b8 20 00 00 00       mov    r8d, 0x20             ; BreakReasons{kDebuggerStatement}
+0x2F          e8 …                    call   Debug::HandleDebugBreak
+0x34          48 8b 86 90 0e 01 00    mov    rax, [rsi+0x10e90]
+0x3B          83 b8 b8 00 00 00 00    cmp    dword [rax+0xb8], 0    ; IsRestartFrameScheduled()
+0x42          74 0F                   je     +0x53
+0x4C          e8 …                    call   TerminateExecution
+0x51          eb 17                   jmp    +0x6A
+0x53          48 83 c6 08             add    rsi, 8                 ; stack_guard()
+0x65          e8 …                    call   HandleInterrupts
+0x83          48 83 c4 30 / 5e / c3   add rsp,0x30 / pop rsi / ret     ; 含 __security_check_cookie
```

**补丁**：把 `+0x22` 的 `75 2F`（`jne`）改成 `EB 2F`（`jmp`）。

- 等长 2 字节，指令边界不变，文件长度不变（302,709,400 字节 → 302,709,400 字节）；
- `if` 体永远被跳过 → `debugger;` 不再暂停；
- 尾部的 `HandleInterrupts()` 照常执行，栈溢出 / 终止等中断不受影响（比「干脆不 emit 字节码」更保守）。

Edge 154.0.4258.48 的机器码形状**完全一致**，只是 `isolate->debug_` 的偏移不同
（`0x11b28` vs Chrome 的 `0x10e90`），闸门同样在 `+0x22` 是 `75 2F`。

工具认的是**骨架**而不是固定偏移：

```
49 8b 88 ?? ?? ?? ??   80 79 ?? 01   75 ??      # disp32 / disp8 / 位移 全通配
```

要求函数入口后 `0x200` 字节内**恰好命中 1 处**，命中数 0 或多都拒绝写入。

---

## 五、实测验证（2026-10-01）

验证纪律：**`debugger` 在没有 DevTools 连接时本来就是空操作**，
所以必须先 `Debugger.enable` 再看有没有 `Debugger.paused` 事件；只看端口通不通永远是假 PASS。

方法：把官方 `Application` 目录整份复制到 `D:\Chrome\_ctest`（767 MB），
在**副本**上做 A/B（不改动真实安装），用 `--headless=new` + 临时 profile + CDP 驱动。
（当时那个 CDP 驱动脚本后来从工作区丢失，已不再随项目提供；手工复现步骤见
README.md 的「验证」小节。）

| 场景 | 结果 | 判定 |
| --- | --- | --- |
| 副本 **未打补丁** `(function(){debugger;return 42})()` | `{paused:true, reason:"other"}` | ✅ 对照有效 |
| 副本 **已打补丁** 同一表达式 | `{paused:false, dtMs:1, value:42}` | ✅ 补丁生效 |
| 副本 **已打补丁** + 手动行号断点（`bp_test.js:1`） | `{paused:true, fn:"bpTarget", line:1}` | ✅ 断点未受影响 |
| `--restore` 回滚后复测 | `{paused:true, reason:"other"}` | ✅ 回滚有效 |

字节级核对：

```
大小: 补丁后=302709400  备份=302709400  相同=True
差异字节数: 1
  偏移 0x71d1ad2: 备份 0x75 -> 补丁后 0xeb
```

第一行是整个结论的前提：如果未打补丁的副本也不暂停，那「补丁生效」的 PASS 毫无意义。

---

## 六、日常维护

### 自动更新后（最常见）

**通常什么都不用做**。工具每次运行都重新推导定位，版本一换偏移就自己跟着变：

```
python patch_browser.py no-debugger --list          # 看各版本当前状态
python patch_browser.py no-debugger --browser chrome --dry-run
```

上次记录的参考值（供比对）：

| 目标 | 版本 | 函数 RVA | 闸门文件偏移 | 原始字节 |
| --- | --- | --- | --- | --- |
| Chrome | 154.0.8037.93 | 0x71D24B0 | 0x71D1AD2 | `75 2F` |
| Edge | 154.0.4258.48 | 0x69F0520 | 0x69EFB42 | `75 2F` |
| Edge | 153.0.4234.48 | 0x6A5D740 | 0x6A5CD62 | `75 2F` |

### 定位失败时（报 `定位失败：…`）

说明 V8 这块的代码结构真的变了，按顺序排查：

1. 函数名串还在不在？`grep -a -c HandleDebuggerStatement chrome.dll`
   （若为 0，可能是 `Runtime::Function.name` 被裁剪了）
2. 表项结构有没有变？用 `probe_runtime_fn.py` 那套思路打印前后各 2 项，
   看 id 是否仍严格 ±1、步长是否仍是 32 字节。
3. 闸门骨架有没有变？反汇编函数体，确认 `break_points_active_` 的判断是否
   换成了 `test`+`jz`、或字段偏移算不出来（那就要改 `GATE_PREFIX`）。
4. 新形态确认后，改 `patch_browser.py`（no-debugger 段）里的 `GATE_PREFIX` / `GATE_JNE_OFF`，
   再 `--dry-run` 校验，最后在**副本**上做 A/B。

---

## 七、局限

1. **只解决 `debugger` 一类**。`toString` 检测、`console` 检测、`devtools-detector`
   库、时间差检测等反调试手段不受影响。
2. **时间差检测顺带受益**：`t1=Date.now(); debugger; t2=Date.now()` 这类判断，
   免疫后 `t2-t1≈0`，对站点而言是「没人调试」。
3. 与 `debug-port` 子命令是两件事：**那个**解决「能不能连上 CDP」，
   **这个**解决「连上之后页面会不会被 `debugger` 卡死」。
4. 浏览器自动更新会整份替换 dll，两个补丁都要重打。
5. 备份后缀是 `.nodebug.bak`（与 CDP 工具的 `.orig.bak` 分开，互不覆盖），
   但 `--restore` 恢复的是**本工具上次打补丁前**的状态，不含别的工具的改动。
