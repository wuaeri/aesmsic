# aesmsic

**把一首歌翻过来看。**

一个跑在你自己机器上的本地音乐播放器。正面是播放，翻过来是编曲解剖 —— 波形、频谱、响度、调性，以及六条分轨各自在第几秒进场。不上传、不联账户、不采集。

![播放器](docs/shot-player.png)

![分轨](docs/shot-stems.png)

---

## 一条贯穿全项目的规矩：measured / estimated

每个数据块都带一个 `method` 字段，只有两个值：

| | 含义 | 前端怎么画 |
|---|---|---|
| `measured` | 信号里直接算出来的 | 实线 |
| `estimated` | 模型猜的 | 虚线 |

波形、频谱、LUFS、样本峰值、LRA、频段占比、立体声相关度 —— **实测**。这些是从采样点直接算的，算错了是 bug，不是判断。

BPM、调性、以及"这条轨是钢琴" —— **估计**。Krumhansl-Schmuckler 给调性，beat tracker 给 BPM，Demucs 给分轨，它们都会错。

这条规矩不是为了免责。是因为一个把猜测和测量混在一起画的产品，**会让你没法判断该信哪一格**。分错了，你就不知道该去听，还是该去信。

所以分轨区永远挂着这句：

> 6 轨 · htdemucs_6s · 每帧 50ms · 进场 −35dB / 离场 −40dB（两个阈值 → 迟滞防抖）。所有轨共用同一套电平线，所以纵轴上「高 = 真的响，不是归一化过」。

以及这句：

> 数据能算出「有个东西在这里」，算不出「它是钢琴」。

---

## 跑起来

### 1. 装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

分轨要另装 `demucs` 和 `torch`（约 2 GB，Apple Silicon 走 MPS）。**不装也能用** —— 波形、频谱、响度、调性照常，只是没有分轨那一层。

### 2. 放歌

往 `media/` 里扔文件就行。支持 `.flac` `.wav` `.m4a` `.ogg`。

命名建议 `Artist-Title.ext` —— 索引按第一个 `-` 切，切不出来就用整个文件名当标题。

### 3. 起服务

```bash
./start.sh          # http://127.0.0.1:8900
./start.sh stop
```

打开之后：

- 页面上什么都没写「分析中」的歌，说明它还没被扫到，等十几秒
- 一首 4 分钟的歌：波形 + 频谱 + 响度约 **6 秒**，六轨分离再 **20 秒**（M5 实测）
- 分轨会晚到，到了页面自己把它长出来，不用刷新

### 4. 桌面 App（可选，仅 macOS）

```bash
cd app && ./build.sh
```

需要 Xcode Command Line Tools，不需要 Xcode 本体，也不需要签名证书（自签）。

它是个 WKWebView 外壳，干三件事：开窗口、服务没跑就拉起来、关掉时只停自己拉起的进程。编译时会把仓库路径写进 `Info.plist`，所以装在 `/Applications` 也知道该回哪找 `server.py`。

---

## 架构

```
media/            你的音乐（不进版本库）
  ↓
pipeline.py       常驻，每 3 秒扫一次 media/，只补缺的
  ├─ analyze.py           波形 / 频谱 / LUFS / 峰值 / LRA / 频段 / 立体声 → analysis/<id>.json
  └─ demucs + analyze_stems.py → 六轨 wav → 逐帧能量 + 进出事件 → analysis/<id>.stems.json
  ↓
build_index.py    汇总成 analysis/_index.json（6 KB，列表页只读这个）
  ↓
server.py         本地 HTTP，手写，唯一有技术含量的部分是实现 Range 请求
  ↓
static/index.html 播放器（单文件，零依赖，零构建）
```

两个人分工，是因为实测过：一首 116 秒的曲子，分离占 19.4 秒（75%），基础分析 6.1 秒，逐帧能量 0.4 秒。**八成时间花在会拖慢"看一眼"的那一步上，但那一步本来就不该挡着你看。**

**为什么手写 HTTP 而不是用框架**：整个服务里唯一难的是 Range 请求。浏览器必须看到 `Accept-Ranges: bytes` 和 `206 Partial Content` 才肯让你拖进度条 —— 一个 264 MB 的 24bit/192kHz 文件，不能拖就等于不能用。标准库里没有现成的，所以那几十行是自己写的。

---

## 已知边界

**分轨是猜的。** Demucs 的官方说明自己写着钢琴轨（`htdemucs_6s` 的第六轨）"not working great"。鼓和贝斯通常可靠，吉他和钢琴经常互换。看的时候记住：**高 = 真的有声，名字 = 模型的标签。**

**BPM 和调性对复杂编曲会错。** 界面上给了置信度和前五候选，是为了让你发现它没把握，不是让你信第一个。

**没有分轨播放。** 能看见六条轨，不能单独听其中一条（solo / mute 还没做）。

**只支持本地文件。** 不接流媒体，不解析任何加密格式。音源请自己解决 —— 这跟播放器本身无关。

**界面是中文的。** 没有做 i18n。

---

## 名字

`aes` + `music`。前三个字母是一个叫 aespaverse 的个人项目群，后面三个是它在这儿干的事。

## 署名

| | |
|---|---|
| [wuaeri](https://github.com/wuaeri) | 内容 · 审美 · 判断 |
| 老 C | 结构 · 代码 · 实现 |

设计与实现全程由 Claude Code 协作完成。

## 许可

MIT
