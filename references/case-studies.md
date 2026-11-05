# 广告位证据档案（样本：B 站 Android 9.13.0）

> **先读这段，再往下看。**
>
> 这份文件是**一次历史改造的快照**，全部类名、dex 编号（`classes12`/`classes15`/`classes25`）、
> 方法签名都只对 **9.13.0** 成立。9.18.0 的实测结果已经证明它们会整体漂移：
>
> | 项 | 9.13.0 | 9.18.0（实测） |
> |---|---|---|
> | 首页装配方法 | `pegasus.api.g.c` | `k.c` 形似但被探针否决 —— 说明字母会被重排，**不能按字母或结构找，只能按探针确认** |
> | 首页装配 dex | `classes12` | `classes14` |
> | 开屏 dex | `classes25` | `classes28` |
> | `BasicIndexItem` / `AdItem` / `adInfo:FeedAdInfo` | —— | 三项都**没变** |
>
> 所以它的正确用法是：**当作"证据链长什么样"的示例**，看完去自己的包里按 SKILL §4.1
> 重新走一遍；**不是**当作类名对照表。下面所有类名都按"某一次改造里是这样"来读。
>
> 另外 §5 末尾那段"广告不出现时怎么验证"是**未实测的设想**，不要当步骤执行。

这份文件记录**一次真实改造**中每一处改动的位置、证据和失败尝试。SKILL.md 讲怎么做，这里讲"为什么是这里"。

**用法**：定位卡住时、或怀疑某个类名是否还适用时读它。不要照抄类名——版本一变就得重新按 SKILL §4 的锚点定位。

## 样本信息

| 项 | 值 |
|---|---|
| 包名 | `tv.danmaku.bili` |
| 版本 | 9.13.0 |
| dex 数量 | 32（`classes.dex` … `classes32.dex`） |
| 设备 | 真机 arm64-v8a |
| 时间 | 2026-11 |

---

## 1 · 开屏广告

| 项 | 内容 |
|---|---|
| dex | `classes25`（体量小的那个，约 8.7 MB 原始） |
| 方法 | `loadSplashForShowInner`、`requestRealShowSplashInner`、`splash_event_splash_enable` |
| 改法 | 前两个方法体开头插入 `return-void`；第三个直接返回 `false` |
| 验证 | 冷启动直接进首页，无开屏倒计时 |

三个点要一起改。只改前两个会留下"事件开关"仍要求展示的分支。

---

## 2 · 暂停页广告

| 项 | 内容 |
|---|---|
| dex | `classes4` |
| 类 | `com.bilibili.adcommon.config.DDConfig` |
| 方法 | `getPausePageEnable()Z` |
| 改法 | 方法体替换为 `const/4 v0, 0x0` + `return v0`，**保留原 `.registers`** |
| 验证 | 暂停视频不再弹出广告卡片 |

```smali
.method public final getPausePageEnable()Z
    .registers 2

    const/4 v0, 0x0

    .line 1
    return v0
.end method
```

`.registers` 必须沿用原方法的数字，改小会让汇编失败或让后续 `v` 引用越界。

---

## 3 · 播放器下方广告卡

这是投入产出比最低的一处，也是最容易找错的一处。**它不是列表项，也不是商品模块**，是广告 SDK 在播放器与标签栏之间插入的独立视图。

### 证据链

`uiautomator dump` 抓到的层级：

```
RelativeLayout  id=ad_tint_frame      [617,1859][1205,2576]
  └ ImageView   id=cover
     ↑ 父节点
GridView        id=recycler_view
FrameLayout     id=swipe_layout
ViewGroup       id=root
ViewPager       id=pager
```

详情页那次是另一套层级：

```
CollapsableChildFrameLayout  id=underplayer_container   ← 高度 wrap_content
  └ RelativeLayout           id=ad_tint_frame
       └ ImageView           id=cover
```

布局文件 `theseus_united_video_details.xml` 里，`underplayer_container` 的实际类型是 `com.bilibili.ship.theseus.united.page.behavior.CollapsableChildFrameLayout`（继承 `FrameLayout`，`wrap_content`）。

### 改法

给这个容器重写 `onMeasure`，测完把高度改写成 0：

```smali
.method protected onMeasure(II)V
    .registers 5

    invoke-super {p0, p1, p2}, Landroid/widget/FrameLayout;->onMeasure(II)V

    invoke-virtual {p0}, Lcom/bilibili/ship/theseus/united/page/behavior/CollapsableChildFrameLayout;->getMeasuredWidth()I

    move-result v0

    const/4 v1, 0x0

    invoke-virtual {p0, v0, v1}, Lcom/bilibili/ship/theseus/united/page/behavior/CollapsableChildFrameLayout;->setMeasuredDimension(II)V

    return-void
.end method
```

因为容器高度是 `wrap_content`，子视图撑不开 → 广告卡没有可显示空间，且**不占版面**（不是 `GONE` 留白，是塌陷）。

### 走过的弯路（不要重复）

| 尝试 | 结果 |
|---|---|
| 给广告 SDK 的 `AdUpperRootFrameLayout` 重写 `setVisibility` 强制 `GONE` | **无效**。那张卡不是它渲染的 |
| 改 `module/merchandise` 的 `MerchandiseService.c()` 调用点 | 卡片换形态后仍在（轮换素材），且方案本身会误伤 |
| 在 `Iy0/i` 入口直接 `return` | 广告仍在，且**把商品/橱窗模块整个关掉** —— 已回退 |

### 为什么"按容器 id 反查类"在这里失效

`ad_tint_frame`（`0x7f09017d`）在已反编译的 dex 里**搜不到 `const` 引用**。它是广告 SDK 在运行时通过 `getIdentifier` 之类的动态方式取 id 的，不是编译期常量。所以这条路走不通——必须从**布局文件**里的自定义 View 类名反查。

---

## 4 · 详情页「相关推荐」列表广告

| 项 | 内容 |
|---|---|
| dex | `classes15` |
| 类 | `com.bilibili.ship.theseus.united.page.intro.module.relate.DetailRelateService` |
| 关键方法 | `k(RelateTab, List)` —— 列表装配循环；`d(z0)` —— 单条转换 |
| 判据 A | `z0.f`（`SourceContent`，即 `IAdReportInfo`）非空 ⇒ 广告 |
| 判据 B | `z0.a == RelateCardType.CM` ⇒ 商业推广卡 |

判据 A 的来源：同一文件里 `IAdReporter.exposeAll$default(..., z0.f, ...)` **只对广告条目调用**。这比猜测枚举值可靠。

### 寄存器陷阱（v14/v15 的"列表全空"根因）

`k(RelateTab, List)` 的 `.registers 8`，3 个参数（`this` + 2）：

```
p0 = v5   ← this
p1 = v6
p2 = v7
局部可用: v0..v4
```

在 `k()` 里用 `v5` 装 `RelateCardType` 会把 `this` 覆盖掉，ART 抛：

```
VerifyError: Verifier rejected class DetailRelateService:
  'this' argument 'RelateCardType' not instance of 'DetailRelateService'
```

**整个类被拒 → `k()` 调不起来 → 列表空白。** 症状像"过滤太狠"，实际是代码坏了。

正确做法：把需要宽裕寄存器的判断放进 `d(z0)`（`.registers 49`）。

### 两个改动点的取舍

| 改点 | 优点 | 风险 |
|---|---|---|
| `k()` 循环内跳过（判据 A） | 覆盖所有带广告上报信息的条目，含伪装成普通视频的广告 | 寄存器极紧，只能做最简单的判断 |
| `d(z0)` 入口返回 null（判据 B） | 寄存器充足，能做类型判断 | 必须确认三个调用方都判空（实测是） |

最终形态是两处都做：`k()` 负责 `SourceContent`，`d()` 负责 `CM`。

### `RelateCardType` 枚举全表（14 种）

| 值 | 含义 | 是否广告 |
|---|---|---|
| `AV` | 普通视频卡 | 可能被广告包装 |
| `CM` | 商业推广卡 | **是** |
| `BANGUMI` / `BANGUMI_AV` | 番剧 / 番剧关联 PGC 视频 | 否 |
| `LIVE` | 直播卡 | 否 |
| `COURSE` | 课程卡 | 否 |
| `GAME` | 游戏卡 | 偏推广，属正常功能 |
| `MINI_PROGRAM` | 小程序卡 | 同上 |
| `SPECIAL` | 专题/活动聚合 | 同上 |
| `RESOURCE` | 资源素材卡 | 否 |
| `AI_RECOMMEND` | AI 推荐 | 否 |
| `HISTORY_AV` | 观看历史（带进度） | 否 |
| `LOADING_PLACEHOLDER` | 加载占位骨架 | 否 |
| `CARD_TYPE_UNKNOWN` | 兜底 | 否 |

---

## 5 · 首页 feed 广告

| 项 | 内容 |
|---|---|
| dex | `classes12` |
| 装配方法 | `com.bilibili.pegasus.api.g.c(JSONArray) → ArrayList` |
| 响应模型 | `PegasusFeedResponse.items: ArrayList<BasicIndexItem>` |
| 广告形态 | ① 子类 `AdItem`；② 普通卡 + `adInfo` 非空（「创作推广」这类） |
| 改法 | 最终 `add` 循环里：`instance-of AdItem` **或** `adInfo != null` → 跳回循环头 |

### 反例：只判 `AdItem` 会漏（真机抓到的）

只做 `instance-of AdItem` 的版本（v17）装上后，首页仍然抓到一张广告卡：

```
id=ad_tint_frame   content-desc="创作推广,这才是我想要的AI,,轻点两下查看详情"  [15,1138][603,1849]
  ├ ImageView       id=cover
  ├ ViewGroup       id=cover_bottom_info_container
  │   ├ TextView    id=cover_left_text1   "49"
  │   ├ TextView    id=cover_left_text2   "-"
  │   └ FrameLayout id=ad_tag_v2          ← 广告标签
  ├ TextView        id=title              "这才是我想要的AI"
  ├ LinearLayout    id=desc_content
  │   └ TextView    id=tag_rcmd           "很多人点赞"
  └ FrameLayout     id=more
```

判读：

- 它**直接挂在 `recycler_view` 下**，是双列瀑布流里的一格，不是浮层；
- 结构（`cover` + `title` + `desc_content` + `tag_rcmd`）**与普通视频卡一致**；
- 唯一的广告特征是 `ad_tag_v2` 与根节点的 `content-desc`。

结论：它不是 `AdItem`，而是 `BasicIndexItem` 的普通视频子类 + `adInfo` 非空。`CardTypeEnum` 的 7 种 `AD_*` 只覆盖"专门广告卡"，覆盖不到这种"普通卡挂广告"的形态。

**教训**：判断一个条目是不是广告，优先看**字段**（`adInfo` / `cmInfo`），而不是**类型**。类型描述的是卡片形态，字段才代表它有广告主。

### 反例二：肯德基卡连 `adInfo` 都是空的

v18（`AdItem` + `adInfo` 两条判据）装上后，首页又抓到一张：

```
id=ad_tint_frame   content-desc="广告,肯德基联名｜辣堡新品买一送一！,,轻点两下查看详情"
  ├ ImageView       id=cover
  ├ LinearLayout    id=quality_container
  ├ FrameLayout     id=corner_hint2          ← 广告角标
  ├ LinearLayout    id=tag_text / View id=tag_view   ← 广告标签
  ├ TextView        id=title                 "肯德基联名｜辣堡新品买一送一！"
  ├ LinearLayout    id=desc_content
  │   └ TextView    id=tag_text              "2万人感兴趣"
  └ FrameLayout     id=more
```

同样是普通卡结构、同样直接挂在 `recycler_view` 下，而 `adInfo` 为空——所以两条判据都抓不到。

结论：**字段判据一次查全**（`adInfo` / `cmInfo` / `isAd` / `cmMark` / `ad_cb` / `cardGoto`）。逐条试的代价是每轮"改 → 汇编 3 分钟 → 打包签名 1 分钟 → 装机 → 刷新"约 6 分钟，而广告投放不稳定，很可能试到第五轮才发现又漏了。

补判据时的两个实现细节：

- `ad_cb` 可能是**空串**而不是 null，直接 `if-eqz` 会把整页内容当广告过滤掉 → 必须用 `String.isEmpty()`。
- 判据变多后寄存器不够。扩 `.registers` 前先 `grep` 原方法有没有显式引用 `v4`/`v5`；扩完后 `p0`/`p1` 自动重映射，但显式的 `v4` 会错位到别的局部变量上。

### 为什么改这一处就够

`g.c` 的管道：

```
JSONArray
 → ArrayList<JSONObject>
 → asSequence()
 → filter(KG/o)
 → map(KG/p)
 → map(com.bilibili.pegasus.api.e)
 → flatMap(KG/r)
 → 逐条 add 到结果列表
```

`F`、`G`、`i` 三个响应解析器都调用它（`F.1:725`、`F.1:1051`、`G.1:197`、`i:197`），所以这是 pegasus 列表的公共瓶颈。

### `CardTypeEnum` 中的广告类型（7 种，全部映射到 `AdItem`）

| 枚举 | impl |
|---|---|
| `AD_SINGLE_V1` | `AdItem` |
| `AD_SINGLE_V7` | `AdItem`（复用前值） |
| `AD_SINGLE_V9` | `AdItem`（复用前值） |
| `AD_SMALL_COVER_V5` | `AdItem`（复用前值） |
| `ADV2` | `AdItem`（复用前值） |
| `AD_DOUBLE_V7` | `AdItem`（复用前值） |
| `AD_DOUBLE_V9` | `AdItem`（复用前值） |

一条 `instance-of` 覆盖全部。反编译里看到 `const-class v6, AdItem` 之后连续多个枚举项复用同一个 `v6`，就是这个意思。

### 探针证据

把过滤条件临时改成**无条件跳过所有条目**，装包 → 下拉刷新：

| 版本 | 刷新后首页标题数 |
|---|---|
| 探针版（全跳过） | **0** |
| 正常版（只跳过 AdItem） | **4** |

第一行证明 `g.c` 确实是首页 feed 的必经路径；第二行证明过滤是选择性的。

### 缓存陷阱

`am force-stop` 后重开，首页显示的是**本地缓存**，不走网络解析。此时改 `g.c` 看不出任何效果——必须先下拉刷新（`input swipe 600 500 600 1900 350`）才会重新解析。

我们在这上面浪费过一整轮，误判为"补丁无效"。

### 广告不出现时怎么验证（思路，尚未实测）

首页广告按批次投放，实测连续 10–20 轮刷新都可能一次不出现。此时"看不到广告"区分不了两件事：服务端没下发，还是被补丁滤掉了。

一个可能凑效的确定性做法（**未实测，用之前先确认前提成立**）：

1. 装**旧版**（不过滤），反复刷新直到 dump 里出现 `ad_tint_frame`；
2. **保持不刷新**，直接覆盖安装新版（安装会杀进程，但应用缓存保留）；
3. 启动新版，**不刷新**，直接 dump 对比。

前提是：B 站把 feed 的**原始 JSON** 缓存到磁盘、启动时重新解析（logcat 里可见 `CachedResourceResolver: readFromDisk json is = …`）。若前提成立，同一份缓存走两条不同解析逻辑，差异就是补丁效果。

若前提不成立，退回"路径探针 + 如实标注推断"，不要编造结论。

---

## 6 · 明确不采用的方案

| 方案 | 为什么放弃 |
|---|---|
| 把 13 处 `ad_extra` 字段置空 | 对信息流**完全无效**，实测确认 |
| 在 `Iy0/i` 入口 `return`（关掉 MerchandiseService） | 广告没去掉，商品/橱窗模块被整个关掉 |
| 改 `MerchandisePanelComponent`（"点开更多"面板） | 不在目标链路上 |
| 从 `AdUnderPlayer` 数据层拦截 | 该字段只出现在暂停页链路，与播放器下方广告卡无关 |

---

## 7 · 各版本改动清单（可回滚基线）

| 版本 | 改动 | 状态 |
|---|---|---|
| v1 | 防自毁 + 开屏 | 验证通过 |
| v2 | v1 + 暂停页 | 验证通过 |
| v3–v8 | 相关推荐/商品模块的多次尝试 | 多为打偏，已回退 |
| v11 | v1 + 播放器下方广告位归零 | 验证通过 |
| v12 | v1 + v2 + 播放器下方（剔除无效改动） | 基线 |
| v13 | v12 + 相关推荐 `SourceContent` 过滤 | 验证通过 |
| v14 | v13 + `CM` 判断写在 `k()` | **失败**（`VerifyError`，列表全空） |
| v15 | 改写在 `k()`，换寄存器 | **失败**（同上） |
| v16 | `CM` 判断移入 `d(z0)` | 验证通过（用户指定保存版） |
| v17 | v16 + 首页 feed `AdItem` 过滤 | **判据不足**：真机抓到「创作推广」卡仍显示 |
| v18 | v17 + 首页 `adInfo` 判据 | **仍漏**：肯德基卡 `adInfo` 为空 |
| v19 | v18 + `cmInfo` / `isAd` / `cmMark` | 页面正常（134 节点），广告未撞到 |
| v20 | v19 + `ad_cb` / `cardGoto`，`.registers` 6→8 | 页面正常（130 节点），广告未撞到 |
| v21 | v20 + banner 过滤，误用 `v11`（Object） | **崩溃**：`VerifyError [0x25B]`，已回滚 |
| v22 | v21 改用已 cast 的 `v12` | 运行正常；banner 位当前是普通视频卡，非广告 |

---

## 8 · 构建产物对应关系

定位改了哪个 dex，就要重新汇编哪个 dex，其它 dex 沿用基线产物：

```
outV12/classes4.dex      ← DDConfig 暂停页
outV16/classes15.dex     ← 相关推荐 + 播放器下方广告位
outV17/classes12.dex     ← 首页 feed
outF/classes25.dex       ← 开屏
patched_libbili.so       ← 防自毁
```

汇编后 dex 体积会明显大于原始（smali 汇编不做原始优化，9.5 MB → 14.7 MB 属正常），**不要**用体积判断是否出错，要用 `find_class.py --compare` 或 `baksmali list classes` 比对类数。

---

## 9 · 9.18.0 补充（2026-11 实测）

以下都是 9.18.0 真机实测，**结论分两类，注意区分**：定位是实测的，效果有真有假。

### 首页 feed：找到了一个"长得一模一样"的方法，但探针否决了它

`PegasusFeedResponse`、`AdItem`、`CardTypeEnum`、`adInfo:FeedAdInfo` 都还在 `classes14.dex`。
`com.bilibili.pegasus.api.k` 里有 `c(JSONArray)→ArrayList`，管道（`asSequence → filter →
map → map → flatMap → add`）与 9.13 的 `g.c` 同构，三个解析子类 `J`/`K`/`m` 都调它。

**改成过滤后装机验证：完全没效果。** 探针（把 `add` 改成无条件跳过）之后首页仍然是
8 张卡，说明首页 feed 根本不走 `k.c`。

所以这一条的正确定位是：**`k.c` 是一个诱人的错误候选**。

**这个判例真正可复用的地方**：筛掉它的依据不是"结构像不像"，而是"探针过不过"。同类的诱人候选每个版本都会有，每次都只能用探针筛，不能靠读代码说服自己"就是这里"。

（顺带：`pegasus.api.g` 这个字母在本版被一个处理 `Bbolts/Task` 的类占着，同样不是它。）

### 防自毁：这一处是真实且有效的

`lib/lib/arm64-v8a/libbili.so` 里对 `exit` 的 `BL` 调用**全库只有 1 处**（文件偏移
`0x8f1c`）。把它 NOP 掉之后：

| 版本 | 处理 | 结果 |
|---|---|---|
| 只换签名、不改代码 | —— | 启动约 **22 秒**后退出，无 `FATAL EXCEPTION` |
| 加了这处 NOP | —— | 稳定存活（60 秒观察窗口） |

所以 9.18.0 **有**签名自毁，而且就这一处。注意"22 秒"这个数字：只抽查一次启动状态
（比如等 14 秒看一眼）会得出"没问题"的错误结论。用 `verify.py --watch 40`。

### 一个看起来完美、其实不能改的谓词

`com.bilibili.pegasus.api.b.c(BasicIndexItem)Z` 的方法体就一行：

```smali
instance-of p1, p1, Lcom/bilibili/pegasus/api/modelv2/AdItem;
return p1
```

像极了"是否广告"的过滤器。但它的调用方是 `z.b(BasicIndexItem)`：

```smali
if-eqz p1, :cond_b
invoke-virtual {p0, p1}, L...z;->c(...)Z      # 是广告卡吗？
move-result v0
if-eqz v0, :cond_b
invoke-virtual {p0, p1}, L...z;->d(...)V      # 是 → 做广告解析/校验/上报
:cond_b
return-void
```

也就是说 `c()` 的语义是**"要不要走广告校验流程"**，不是"要不要这条卡片"。
把它改成恒 false，卡片照样在列表里，只是不再做广告解析——**看起来改了、实际没用**。

教训：在 smali 里看到一个像过滤器的布尔方法，先往上找调用方，看清楚返回值被怎么用。

