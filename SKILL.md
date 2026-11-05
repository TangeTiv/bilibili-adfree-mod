---
name: bilibili-adfree-mod
description: >
  把一个任意版本的 Android 哔哩哔哩（tv.danmaku.bili）改造成无广告版：用 REA 侦察，按语义锚点重新定位广告链路，
  改 smali 后重打包签名装机，并用探针证明改动真的生效。
  覆盖开屏 / 暂停页 / 播放器下方 / 详情页相关推荐 / 首页 feed 格子卡 / 首页通栏 banner 六类广告位。
  适用于用户说"给 B 站去广告""做个无广版 B 站""改一下 bilibili 安装包""b 站广告太多"，
  或给了一个 B 站 APK 要求去广告（版本可能从未见过）。
agent_created: true
---

# 你的任务

把用户**自己设备上**的哔哩哔哩改造成无广告版，交付一个可覆盖安装的签名 APK。

版本由用户提供，可能你从没见过。**你的工作是按语义重新定位，不是套用任何一篇教程里的类名。**

## 必须先接受的现实

| 会变的 | 不会变的 |
|---|---|
| 混淆类名（`g` → `k`，`qs0/b` → `Tt0/b`，`z0` → `D0`） | 模型类名（`BasicIndexItem`、`AdItem`） |
| 类所在的 dex（35 个 dex 里换位置很常见） | 方法语义（`c(JSONArray)→ArrayList` 仍是列表装配） |
| Kotlin 扩展函数所在的类（`*Kt` 类之间搬） | 资源 id（`ad_tint_frame`、`underplayer_container`） |
| 字段布局（`.registers`、`.line` 编号） | 字段名（`adInfo`、`cmInfo`、`isAd`、`cardGoto`） |
| 方法在类里的相对位置 | UI 结构（封面+标题+角标） |

**结论**：类名只是路标，语义才是地图。每次换版本都要重新走一遍"看症状 → 找入口 → 证明入口"。

## 交付定义

同时满足才算完成：

1. 每个声称处理掉的广告位，**有验证等级说明**（见 §7）。
2. 正常运行：`FATAL EXCEPTION = 0`、`VerifyError = 0`、进程存活。
3. 没有误伤：页面元素数不低于基线、正常卡片与功能仍在。
4. 产出物带版本号、sha256、改动清单，上一版可回滚。

**"改了但没验证"不等于完成。** 必须显式区分"已验证"和"推断"。

## 开工前必须问清

**如果用户不像技术用户**（问"这是什么意思"、只说"帮我去广告"），先照 `GUIDE.md` 的口径解释会发生什么、会失去什么——不要一上来就讲 dex、签名、探针。解释清楚再开始。

1. 目标设备 serial（`rea list-adb-devices`），是否接受覆盖安装。
2. 是否接受"应用商店更新失效、部分依赖签名的功能可能异常"。
3. 有没有 keystore。没有就生成一个并要求用户备份——**同一个 keystore 才能覆盖安装、保住登录态**。

## 硬性约束

违反任意一条，结果都不可信：

- **只在用户自己的设备上操作。** 不分发、不上传、不提供公开下载链接。
- **禁止照抄任何文档里的混淆类名。** `g`/`k`、`qs0/b`/`Tt0/b`、`z0`/`D0` 每版都可能变。一律按语义锚点重新定位。
- **禁止在没做探针的情况下宣称"路径正确"。** 结构相似、汇编通过、装机不崩，都不能证明改的是对的地方。
- **禁止把 L1/L2 说成"已修复"。** 证据等级必须如实标注。
- **禁止对同一文件并行发编辑请求**（会互相覆盖，症状是"报告成功但改动丢失"）；一次一处，改完立刻 grep 校验。
- **禁止用 `zip -X` 原地更新大 APK**（会被锁住，且可能留下半更新的包被签名发出去）。
- **一个版本只加一处改动**（否则出问题无法归因）。

---

# 工作循环

对**每一个**广告位，重复这六步。跳过第 3 步 = 交付一个可能无效的版本。

```
1 观察   让它显示出来，记录特征（截图 / dump / 日志）
2 定位   找到它的数据入口或视图入口
3 探针   证明你找到的入口就是它的入口
4 修改   改 smali
5 验证   装机，证明效果
6 归档   版本号 + sha256 + 改动清单
```

**一次只处理一个广告位。** 混在一起改，出问题无法归因，验证也说不清是哪处生效。

---

# 阶段 0 · 前置检查

> 本文件里所有 `scripts/…`、`references/…` 都相对于 **skill 根目录**。你的 cwd 不在那里时用绝对路径（skill 加载时会给出 base directory）。

```bash
python scripts/preflight.py --apk <目标.apk>
```

它把"不成立也不会报错、只会让你两小时后才发现"的前提变成 FAIL：**rea 未安装**、adb 不在 PATH、**split 分包**（只改 base.apk 对 split 里的 dex 完全无效且不报错）、端上装的不是你以为的那份包。

**有 FAIL 就先修掉再动包。** 这个脚本存在的意义就是让问题在十分钟内暴露，而不是两小时后。

---

# 阶段 1 · 侦察（十分钟内摸清一个版本）

## 1.1 包的基本面

```bash
rea inspect-android-package <apk>
# 或：aapt dump badging <apk>（中文路径会失败，先复制到英文路径）
```

记录三件事：**版本号、dex 数量、入口 Activity**。B 站是大包（32–35 个 dex、约 35 万类），版本号决定后面所有判断。

把源包复制一份当基线，之后每次重建都从它出发：

```bash
cp base.apk baseline.apk && sha256sum baseline.apk
```

## 1.2 关键类在哪个 dex

**不要全量反编译 35 个 dex**（几十分钟、几十 GB）。两步走：

```bash
# ① 列出每个 dex 的类（几秒一个，不解码指令）
baksmali list classes classes14.dex > list14.txt

# ② 按 JVM 描述符精确匹配 —— 格式是 Lcom/bilibili/x; 不是 com.bilibili.x
grep "^Lcom/bilibili/pegasus/api/model/BasicIndexItem;$" list*.txt
```

`scripts/find_class.py <apk> --class <类名>` 封装了这件事。

**只解命中的那几个 dex。**

## 1.3 防自毁：先比对，再动手

B 站部分版本的 `libbili.so` 会做签名校验，装完十几秒**静默退出**（无 `FATAL`，logcat 只有 `exited cleanly`）。

```bash
python scripts/scan_native_exit.py libbili.so --list
```

**先比对，不要直接打补丁。** 不同版本的 `libbili.so` 可能**完全相同**——实测 9.13.0 与 9.18.0 只差 4 字节，正是补丁位置。用 sha256 比对，相同就直接复用已有补丁，零成本：

```bash
sha256sum 新版/libbili.so 旧版/patched_libbili.so
# 若不同，逐字节对比差异段，看差异是否只落在补丁处
```

没有这道校验的版本，跳过本节。

---

# 阶段 2 · 广告位地图

**先认形状，再找代码。** 每行是"看到的症状 → 稳定锚点 → 已知变体"。锚点是语义特征，不是类名。

| 广告位 | 怎么认（症状） | 稳定锚点 | 已知变体 |
|---|---|---|---|
| **开屏** | 启动时全屏广告图 + 跳过按钮 | 方法名 `loadSplashForShowInner`、`requestRealShowSplashInner`、`splash_event_splash_enable` | 方法可能在 `SplashManager`，也可能搬进 `SplashOrderSelectComponentKt`（Kotlin 扩展类） |
| **暂停页** | 暂停视频弹出广告卡 | `DDConfig.getPausePageEnable()Z` | 在 `adcommon.config` 包 |
| **播放器下方** | 播放器与标签栏之间一条广告 | 容器 id `underplayer_container`，内含 `ad_tint_frame`；类型是 `wrap_content` 的 FrameLayout 子类 | 类名稳定：`CollapsableChildFrameLayout` |
| **详情页相关推荐** | 视频下方推荐列表里的推广卡 | 条目字段 `SourceContent` 非空；枚举 `RelateCardType.CM` | 条目类型混淆名会变（`z0`→`D0`），**字段名 `a`/`f` 不变** |
| **首页 feed 格子卡** | 瀑布流里一格，有"广告/创作推广"角标，结构像普通视频卡 | 模型 `BasicIndexItem` 的广告字段；`AdItem` 子类 | **判据要一次查全**，见 2.1 |
| **首页通栏 banner** | 横跨整行的大图 + "查看详情" | `pegasus.holders.bannerv8` 包；类型判定 `h.a(String)Z` | 对象类型混淆名会变（`qs0/b`→`Tt0/b`）；**`uiautomator` 抓不到它** |

## 2.1 首页 feed 格子卡：判据必须查全

最容易反复失败的一处。实测两次漏网：

| 判据 | 漏掉的卡 |
|---|---|
| `instance-of AdItem` | 「创作推广·这才是我想要的AI」 |
| + `adInfo != null` | 「广告·肯德基联名」 |

两张都是**普通视频卡结构 + 广告角标**，广告信息落在不同字段上。所以不要逐条试（每轮"改→汇编→打包→装机→刷新"约 6 分钟），**一次把 `BasicIndexItem` 上所有广告字段查一遍**：

`AdItem` 子类 / `adInfo` / `cmInfo` / `isAd` / `cmMark` / `ad_cb`（空串要放过）/ `cardGoto == "ad"`

完整 smali 见 `references/case-studies.md`。

---

# 阶段 3 · 定位

## 3.1 四种手法，按可靠性降序

1. **UI 反查**（最可靠）：让广告显示 → `uiautomator dump` → 取 `resource-id` → 在 `res/values/public.xml` 查布局 → 从布局里的自定义 View 类名反查代码。
2. **字段反查**：广告条目一定带专有字段（曝光上报对象、`adInfo`）—— 按字段找消费方。
3. **结构特征反查**：按方法签名 + 内部流水线形状找（如 `c(JSONArray)→ArrayList` + `asSequence→filter→map→flatMap→add`）。
4. **猜类名**：仅当前三条都失败时使用，**必须配合阶段 4 探针**。

```bash
rea search-android-classes   <apk> <关键词>
rea inspect-android-method   <apk> <类全名> <方法名>     # 直接读反编译后的实现
rea trace-android-references <apk> <类全名>              # 谁在调它
```

**rea 是必须的，不是可选项。** 没有它就停下来装，不要用 `grep` 硬扛——读反编译后的 Java 和读 smali 的差速，在"判断这段逻辑到底在干什么"这件事上非常明显；硬扛的典型结果是定位不准、反复试错、把结构相似当路径正确。

```bash
rea --version                                  # ① 确认可用
npx -y rea-agents@latest setup                 # ② 未安装时（需要 Node.js）
rea inspect-android-package <任意.apk>          # ③ 装完自检：必须能返回包信息
```

REA 的 Android 分析需要 JDK 21 与匹配的 JADX headless JAR；跑不通时它报的错会直接指向缺哪一项，按提示补。

## 3.2 手法 3 有个大坑

**结构完全相同 ≠ 同一条路径。** 实测 9.18.0 的 `pegasus.api.k.c()` 与 9.13.0 的 `pegasus.api.g.c()` 入参、返回类型、内部流水线、调用它的解析子类全部一致——但探针证明首页根本不走它。

结构相似只能用来**提出候选**，不能用来**确认路径**。确认只能靠探针。

---

# 阶段 4 · 探针：唯一的路径确认手段

## 4.1 怎么做

把正要改的条件**临时改成"无条件命中"**，跑真实场景：

| 广告位 | 探针 | 命中表现 |
|---|---|---|
| 列表过滤 | 无条件跳过**所有**条目 | 列表变空 |
| 高度归零 | 把高度改成显眼的异常值 | 版面明显变化 |
| 开屏 | 方法开头 `return-void` | 无开屏 |
| banner | 无条件跳过所有 banner | banner 区消失 |

## 4.2 判读

- **症状出现** ⇒ 路径正确，继续做功能补丁。
- **症状不出现** ⇒ **立刻换路径**，不要在这条线上继续优化。

## 4.3 两个会让探针撒谎的陷阱

1. **缓存**：首页有本地缓存，`force-stop` 后重开读的是缓存，不走解析。**必须先下拉刷新**再 dump。这是把"路径错"误判成"补丁无效"的头号原因。
2. **`uiautomator` 看不见 SurfaceView/WebView 广告**。通栏 banner 在 dump 里完全不存在（既无 `ad_tint_frame` 也无"广告"二字），只能用截图判断。**只靠 dump 关键词会漏掉整整一类广告。**

---

# 阶段 5 · 改 smali 的纪律

每条都是真实事故。

1. **调对象方法前，先确认寄存器在 verifier 眼里是什么类型。** 同一个对象常有两个别名：一个来自 `move-result-object`（verifier 只知道是 `Object`），另一个是 `check-cast` 过的副本。用错会抛
   `VerifyError: 'this' argument 'Reference java.lang.Object' not instance of 'Reference: X'`
   —— **整个类被拒，应用启动即闪退**。
2. **先算寄存器**：局部寄存器 = `.registers` − 参数个数（含 `this`）。`.registers 8` + 3 参数 ⇒ 局部只有 `v0..v4`，**`v5` 就是 `this`**。拿它装别的类型会覆盖 `this`，症状是"某个页面整块空白"。
3. **对象寄存器与整型寄存器分开用**。`instance-of` 写 int、紧接着 `iget-object` 写同一寄存器，verifier 可能报类型冲突。
4. **`.line` 编号跨版本会变，不能当 anchor**。用"指令序列"或"字段访问"当锚点。
5. **注释在汇编后全部丢失**。不能用 `grep PATCHED` 校验 dex，要校验**指令指纹**或比对类数。
6. **写文件顺序**：写临时文件 → 校验内容 → 再 `os.replace`。断言失败时若先 `rm` 后 `mv`，文件就没了（实测丢过一次，重新反编译才恢复）。
7. **Windows 文件名大小写不敏感**：同时存在 `g` 和 `G` 时 baksmali 会写成 `g.smali`/`G.1.smali`，回读时命名可能反过来。看到"类丢了"先用**文件数 vs 类数**比对。

---

# 阶段 6 · 打包、签名、装机

## 6.1 用脚本重打包

```bash
python scripts/rebuild.py --base baseline.apk --out mod.apk \
    --replace classes14.dex=/work/out/classes14.dex \
    --replace lib/arm64-v8a/libbili.so=/work/out/patched_libbili.so \
    --sign --signer /tools/uber-apk-signer.jar \
    --ks mod.jks --ks-alias mod --ks-pass <pass>
```

它保证四件事，手写容易漏：拒绝替换**不存在**的条目（拼错会悄悄新增文件）、流式复制未改条目、写完**读回产物**逐条比对 sha256、任一条不匹配即退出非零。

**不要用 `zip -X` 原地更新**：200MB+ 的包会被系统锁住，还可能留下半更新的包，然后被签名发出去。

## 6.2 装机

```bash
adb push mod-signed.apk /data/local/tmp/m.apk
adb shell pm install -r -d /data/local/tmp/m.apk
```

`pm install` 比 `adb install` 更可靠：部分国产 ROM 会对 streamed install 弹确认框，超时即 `INSTALL_FAILED_USER_RESTRICTED`。

**同签名 + 版本号递增 ⇒ 覆盖安装，登录态保留**。只有签名变了或版本降低才需要卸载（会丢登录态）。

---

# 阶段 7 · 验证与证据等级

```
L0 改了，没装        —— 什么都不算
L1 装了，没崩        —— 只证明没改坏
L2 探针通过          —— 证明路径正确
L3 看到效果          —— 证明广告真的消失
```

交付时用等级说话：**"L3 已验证" / "L2 路径正确，实际广告未撞到" / "L1 仅未崩溃"**。不要把 L1/L2 说成"已修复"。

## 7.1 每次装机必查

```bash
python scripts/verify.py --apk ./mod-signed.apk \
    --launch --refresh --strict-identity --dump-keywords ad_tint_frame,广告 --expect-elements 120
```

检查：设备在线 → 进程存活 → `FATAL`/`VerifyError` → 原生自毁迹象（有 died 但 `FATAL=0`）→ 包 sha256 一致 → 页面元素数达到基线 → 关键词命中。

**`--expect-elements` 不能省。** 没它的话，停在登录页/加载页时"广告 0 次"也会 PASS——实测踩过：dump 只有 2.6KB、页面是登录页，却七项全绿。

## 7.2 广告测不出来时

1. 先跑**对照组**（上一版，同样刷新次数）。对照组同样为 0 ⇒ 环境问题不是补丁问题。
2. 再用探针证明路径。
3. 最后如实说："路径已验证，实际广告未撞到，结论属推断。"

广告有**频控**：同一账号短时间反复刷新会拿不到新广告，越刷越测不出。

---

# 阶段 8 · 交付

```
releases/
  bilibili-<version>-mod-v<N>.apk
  bilibili-<version>-mod-v<N>.sha256
```

- 每版只比上一版多一处改动。
- 记录：版本号、改了哪几个 dex、每个 dex 的 sha256、改动清单、**每个广告位的证据等级**。
- 回滚 = 装上一个基线包（同签名，数据保留）。

**向用户说明四件事**：改了什么、验证到什么程度、还剩什么没解决、怎么回滚。

---

# 附录 A · 拿到新版本先查这六项

按顺序判断"能不能复用上一版"：

1. **`libbili.so` 是否相同**（sha256 / 逐字节差异）。相同 ⇒ 防自毁补丁直接复用。
2. **模型类名是否还在**（`BasicIndexItem`、`AdItem`、`RelateCardType`）。非混淆类通常跨版本稳定。
3. **目标类在哪个 dex**（`list classes` + 精确匹配）。位置几乎每版都变。
4. **混淆名是什么**（banner 对象 `qs0/b`→`Tt0/b`，相关推荐条目 `z0`→`D0`）。从方法签名反推。
5. **方法是否搬了类**（`loadSplashForShowInner` 从 `SplashManager` 搬到 `SplashOrderSelectComponentKt`）。
6. **字段名是否变了**（`adInfo`/`cmInfo`/`isAd`/`cardGoto` 实测跨版本不变）。

前三条决定工作量，后三条决定改法。

# 附录 B · 失败模式速查

| 现象 | 原因 |
|---|---|
| 装完十几秒自动退出，无 FATAL | `libbili.so` 签名校验 |
| 启动即闪退，logcat 有 `VerifyError` | smali 类型/寄存器错，整个类被拒 |
| 某个列表整页空白 | 同上，或过滤条件误伤全表 |
| 补丁"没效果" | 看的是缓存；或打错路径（先做探针） |
| dump 显示"广告 0 次"但用户看到广告 | 那是 SurfaceView/WebView 广告，dump 看不见，要截图 |
| 广告还在但换了形态 | 同一坑位轮换素材，说明只拦住一类 |
| 打包后装不上 | 签名损坏；确认走脚本且逐条哈希校验过 |
| 写 APK 报 `Permission denied` | 用了 `zip -X` 原地更新 |
| 中文路径下 aapt 报 `Illegal byte sequence` | 复制到英文路径再跑 |

# 随包资源

| 路径 | 什么时候用 |
|---|---|
| `scripts/preflight.py` | 阶段 0，环境/分包/包身份预检 |
| `scripts/find_class.py` | 阶段 1，定位类在哪个 dex + 类数比对 |
| `scripts/scan_native_exit.py` | 阶段 1，定位/中和 `libbili.so` 的 `exit` 调用 |
| `scripts/rebuild.py` | 阶段 6，重打包 + 逐条校验 + 签名 |
| `scripts/verify.py` | 阶段 7，装机后体检 |
| `references/case-studies.md` | 两个真实版本的完整类名、字段名、smali 与踩坑记录 |
| `evals/cases.md` | 交付前自查：七个最容易走错的判断 |

`references/case-studies.md` 里的类名是**某两个版本**的快照，当线索用，不要当答案。
