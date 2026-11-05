# bilibili-adfree-mod

一个给 AI Agent 用的技能包：把**任意版本**的 Android 哔哩哔哩（`tv.danmaku.bili`）改造成无广告版。

不是成品 APK，是**方法论**——它教 Agent 如何在一个从没见过的版本上，自己定位广告链路、改 smali、重打包、并用探针证明改动真的生效。

> ⚠️ 仅限在**你自己的设备**上使用。请先读 [DISCLAIMER.md](DISCLAIMER.md)。

---

## 它能处理什么

| 广告位 | 识别特征 |
|---|---|
| 开屏 | 启动时全屏广告 + 跳过按钮 |
| 暂停页 | 暂停视频弹出的广告卡 |
| 播放器下方 | 播放器与标签栏之间的横条 |
| 详情页相关推荐 | 视频下方推荐列表里的推广卡 |
| 首页 feed 格子卡 | 瀑布流里一格，有"广告/创作推广"角标 |
| 首页通栏 banner | 横跨整行的大图 + "查看详情" |

## 它不做什么

- 不提供现成的修改版安装包
- 不处理 iOS / 网页版
- 不绕过会员、不破解任何付费内容
- 不动账号数据

---

## 前置要求

| 依赖 | 说明 |
|---|---|
| **REA**（Reverse Engineer Anything） | 必需。`npx -y rea-agents@latest setup` |
| Node.js | 装 REA 需要 |
| JDK 21 | REA 的 Android 分析需要 |
| `baksmali` / `smali` 2.5.2 | dex ↔ smali 互转 |
| `uber-apk-signer` | zipalign + 签名 |
| Python 3.9+ | 跑本包的脚本 |
| adb | 与设备通信 |

`scripts/preflight.py` 会把缺哪一项直接报出来，不用挨个试。

---

## 怎么用

把这个目录放进你的 skills 目录（例如 `~/.workbuddy/skills/bilibili-adfree-mod/`），然后对 Agent 说：

> 用 bilibili-adfree-mod 这个技能，帮我把手机上的 B 站去广告

面向非技术用户的详细说明见 [GUIDE.md](GUIDE.md)。

---

## 目录结构

```
SKILL.md                 给 Agent 的主提示词：任务、六步循环、判据、纪律
GUIDE.md                 给普通用户的白话指南
DISCLAIMER.md            使用边界
scripts/
  preflight.py           开工前预检（rea / adb / 分包 / 包身份）
  find_class.py          定位类在哪个 dex + 汇编前后类数比对
  scan_native_exit.py    定位/中和 libbili.so 的 exit 调用（防签名自毁）
  rebuild.py             重打包 + 逐条哈希校验 + 签名
  verify.py              装机后体检（崩溃 / 自毁 / 包一致性 / 页面渲染）
references/
  case-studies.md        两个真实版本的完整类名、字段名、smali 与踩坑记录
evals/
  cases.md               七个最容易走错的判断，交付前自查
  run_checks.py          技能包自身的完整性检查
```

---

## 设计上的三个要点

### 1. 混淆名只是路标，语义才是地图

B 站的类名、dex 位置、方法归属每个版本都会漂移。实测两个版本之间：

| 会变 | 不变 |
|---|---|
| `pegasus.api.g` → `k` | `BasicIndexItem`、`AdItem` 这些模型类名 |
| `classes12` → `classes14` | 字段名 `adInfo` / `cmInfo` / `isAd` / `cardGoto` |
| banner 对象 `qs0/b` → `Tt0/b` | 资源 id `ad_tint_frame`、`underplayer_container` |
| 开屏方法从 `SplashManager` 搬到 `SplashOrderSelectComponentKt` | 方法名 `loadSplashForShowInner` |

所以技能不提供"类名对照表"，只提供"怎么重新找到它"。

### 2. 探针是唯一的路径确认手段

结构完全相同的两个方法，可能一个是一真一假。实测遇到过一个候选：入参、返回类型、内部流水线（`asSequence→filter→map→flatMap→add`）、连调用它的三个解析子类都和上一版完全一致——但把它改成无条件过滤后，首页照旧有内容，说明根本不走它。

**结构相似只能用来提出候选，不能用来确认路径。**

### 3. 证据要分级

```
L0 改了，没装        —— 什么都不算
L1 装了，没崩        —— 只证明没改坏
L2 探针通过          —— 证明路径正确
L3 看到效果          —— 证明广告真的消失
```

交付时必须用等级说话。把 L1 说成"已修复"，是这类工作最常见的谎。

---

## 已知限制

- **每个新版本都要重做一遍**：版本一变，代码位置就变，没有一劳永逸的补丁。
- **改过的包不能从应用商店更新**：签名变了，商店不认。
- **广告投放不稳定**：有些广告位一天可能一次都不推，导致"无法证明广告消失"。这种情况下技能会如实报告为"路径已验证，效果未观测"，而不是含糊地说成功。
- **少数功能可能异常**：分享、投屏、第三方登录等依赖官方签名的功能。

## 许可

没有附许可文件。默认保留所有权利——你可以自己用、自己改，但分发修改后的应用安装包不在授权范围内。详见 [DISCLAIMER.md](DISCLAIMER.md)。
