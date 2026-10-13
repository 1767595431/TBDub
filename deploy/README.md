# TBDub 部署文档（国内镜像 + 双版本权重）

给**新 GPU 机器从零部署**、**重新下载权重**、**在 V1.0 / V1.1 权重之间切换**用。

> 前提：本套流程**不改任何代码**。`inference.py`、`diffsynth/`、`server/`、`admin/` 一行都不动，
> 只操作 `checkpoints/` 和 `config.yaml`（这两个本来就不进 git）。

---

## 0. 三条速查结论

| 问题 | 结论 |
|---|---|
| 用哪个权重版本？ | **默认 V1.1**（ModelScope，全套约 25GB）。模型效果与 V1.0 完全一致，体积减半。要跟老生产机逐字节对齐时才用 V1.0（HF `v1.0` tag，约 48GB） |
| 能不能两版共存？ | 能。`checkpoints/v1.1/` 和 `checkpoints/v1.0/` 各放一份，代码读的三个文件名用硬链接指向"当前生效"的那版，`switch_weights.sh` 一键切换 |
| 为什么不用改代码？ | 代码按 `tbdub_base` + `tbdub_finetune`（Teacher）/ `tbdub_student`（Student）三个固定文件名加载，并按顺序合并 state dict。V1.1 是**完整模型**、键集合与 V1.0 一致，所以能直接顶进这几个槽位。细节见 §3.3 |

---

## 1. 本平台的两个坑（**必须先处理，否则卡死**）

这套部署脚本里 `01_system.sh` 会做两件事，换机器时**不要跳过**：

1. **IPv6 优先导致连接挂死。** 容器拿到的 DNS 会返回 IPv6 地址（`pypi.tuna.tsinghua.edu.cn` → `2402:f000:...`、
   `pypi.org` → `2a04:4e42:...`），这些地址连不上，`curl`/`pip`/`apt` 会一直等到超时。
   修法：`/etc/gai.conf` 加一行 `precedence ::ffff:0:0/96  100`，让 `getaddrinfo` 优先 IPv4。
2. **全局 `http_proxy` 拖慢国内源。** 容器环境里预设了 `http_proxy=http://mihomo.kube-system.svc.cluster.local:7890`
   （这是平台给海外域名做加速用的）。访问国内镜像时应绕过它，否则速度只有几百 KB/s。
   修法：把国内域名写进 `no_proxy`（脚本已内置），并在 apt 里对 `mirrors.aliyun.com` 设 `DIRECT`。

海外域名（`download.pytorch.org`、`huggingface.co`）**保留走代理**，那是加速通道。

---

## 2. 机器要求

| 项 | 要求 | 本次实测机器 |
|---|---|---|
| OS | Linux，Ubuntu 22.04 | Ubuntu 22.04.5 |
| GPU | NVIDIA，显存 ≥ 24GB | RTX 4090 **48GB**（驱动 610.57.04） |
| 磁盘 | ≥ 200GB（权重 25GB + 可选 V1.0 48GB + data/） | 200GB，可用 183GB |
| 内存 | ≥ 32GB | 64GB |
| Python | **3.10**（`mmcv==2.1.0` 的轮子对 3.11/3.12 不齐） | 3.10（conda） |
| conda | `/usr/local/miniconda3` | 一致 |
| ffmpeg | 在 PATH 里 | 系统包，apt 装 |

`start_server.sh` / `start_worker.sh` / `restart_api.sh` 里 conda 路径写死为 `/usr/local/miniconda3` + `conda activate tbdub`；
若你的 conda 不在这里，**必须同步改这三个脚本**，并把 `config.yaml` 的 `python_bin` 写成绝对路径。

---

## 3. 从零部署

```bash
# 假设仓库在 /AI/TBDub-master，部署脚本在 <repo>/deploy/
cd /AI/TBDub-master

bash deploy/01_system.sh          # 系统包：git ffmpeg build-essential libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 + 两个坑
bash deploy/02_env.sh             # conda env tbdub：python3.10 + torch2.4.1+cu121 + requirements + mmcv2.1.0
bash deploy/03_weights_v11.sh     # V1.1 权重 + Wan 辅助权重 + DWPose 权重（约 28GB）
bash deploy/04_weights_v10.sh     # 可选：V1.0 原版权重（约 48GB，来自 HF v1.0 tag）
bash deploy/05_finalize.sh v11    # 整理目录 + 生成 config.yaml + 指向 V1.1

# 启动与自检
bash start.sh
bash logs.sh
curl -s http://127.0.0.1:8811/api/system/ready
```

每个脚本都可以**重复执行**：已存在的 conda 环境会跳过，已下完的文件按字节数校验后跳过，
中断的分片保留在 `*.partNN` 里，重跑从断点续传。

### 3.1 依赖来源（全部国内）

| 组件 | 源 | 备注 |
|---|---|---|
| apt | `mirrors.aliyun.com/ubuntu`（DIRECT） | 走国外源时 200 个包要 30 分钟，换源后约 4 分钟 |
| conda | 清华 `mirrors.tuna.tsinghua.edu.cn/anaconda` | 用机器现有 `.condarc` |
| pip | 清华 `pypi.tuna.tsinghua.edu.cn/simple`（DIRECT） | |
| PyTorch | 同上（PyPI 的 Linux wheel 本身就是 cu121 构建） | 备选：`download.pytorch.org/whl/cu121`（走代理） |
| V1.1 权重 | ModelScope `TaoLiveAIGC/TBDub` | 实测 13–19MB/s，8 线程分片 |
| V1.0 权重 | hf-mirror `TaoLiveAIGC/TBDub` tag `v1.0` | 实测 8 线程约 3.4MB/s |
| hubert-large-ll60k | ModelScope `facebook/hubert-large-ll60k` | 1.26GB |
| Wan VAE / T5 / umt5-xxl | 本机共享盘 `/model/HuggingFace/Wan-AI/...` | 已存在则直接拷贝，不用下载 |
| DWPose 两个 `.pth` | `download.openmmlab.com` | 约 590MB |
| mmcv | `download.openmmlab.com/mmcv/dist/cu121/torch2.1/` | **预编译 wheel**，见下 |

### 3.2 mmcv 的坑（重要，`deploy/fix_mmcv_mmdet.sh`）

`mmcv==2.1.0`（requirements / 部署安装文档里钉的版本）在 cu121 索引里**只有 torch2.1 的预编译轮子**，
torch2.4 就只剩源码编译，而本机 CUDA 工具链是 **nvcc 13.2**，和 torch 的 cu121 对不上 →
`torch.utils.cpp_extension` 的版本校验直接拒绝编译。

更坑的是：torch2.1 那个轮子能装上、`import mmcv` 也能过，但 `mmcv.ops` 会在加载 `_ext*.so` 时报

```text
ImportError: mmcv/_ext.cpython-310-x86_64-linux-gnu.so: undefined symbol:
_ZN2at4_ops10zeros_like4callERKNS_6TensorEN3c108optionalINS5_10ScalarTypeEE...
```

（torch 2.4 改了 `at::_ops::zeros_like::call` 的签名，符号对不上。）
后果不只是"少几个算子"：`mmcv.ops` 一挂，`mmdet.apis` 和 `mmpose.apis` 全部 import 失败，
**DWPose 人脸检测/预处理整条链路直接不可用**，服务"就绪"但一提交任务就炸。

本套脚本的处理（`fix_mmcv_mmdet.sh`）：

1. 装 `mmcv==2.2.0` 的 **cu121/torch2.4** 预编译 wheel（这个版本有轮子）；
2. mmdet 3.2.0 / 3.3.0 里 `mmcv_maximum_version = '2.2.0'` 且判断是 `<`，会把 2.2.0 自己判成不兼容，
   而 OpenMMLab 官方兼容表实际覆盖到 2.2.x —— 因此把该常量放宽为 `'2.3.0'`（一行，脚本内已标注），
   同时把 mmdet 升到 PyPI 上最新的 3.3.0；
3. 装完立即验证 `import mmcv.ops / mmdet.apis / mmpose.apis`。

> 想要和部署文档**逐版本一致**（mmcv 2.1.0 + mmdet 3.2.0）的话：先装 CUDA **12.1** 工具链
> （本机镜像只有 13.2），再 `CUDA_HOME=/usr/local/cuda-12.1 pip install --no-build-isolation mmcv==2.1.0` 源码编译。

顺序很重要：**先装完 requirements，最后跑 mmcv 修复**。因为 `requirements.txt` 钉了 `mmdet==3.2.0`，
会把你升级好的版本覆盖回去；`02_env.sh` 已按这个顺序编排。

### 3.3 权重版本与"零改动兼容"的原理

官方仓库（HF `TaoLiveAIGC/TBDub`）有 `v1.0`、`v1.1` 两个 tag，**训练权重完全相同**，
区别只在存储方式（官方 CHANGELOG）：V1.1 把每个变体存成**一个完整的 BF16 DiT**，
V1.0 存的是"基础权重 + 增量"，由代码按顺序叠加。

| | V1.0 | V1.1 |
|---|---|---|
| Teacher | `tbdub_base` 12.59GB + `tbdub_finetune` 10.28GB = **22.87GB** | `tbdub_teacher` **12.59GB** |
| Student | `tbdub_base` 12.59GB + `tbdub_student` 25.18GB = **37.77GB** | `tbdub_student` **12.59GB** |
| 全套 | 约 48GB | 约 25.2GB |
| `--cpu-offload` 低显存模式 | 无 | 有（代码功能，本仓 V1.0 代码没有，不影响出片质量） |

代码侧的加载逻辑（`diffsynth/core/loader`）：

```python
state_dict = {}
for path in paths:               # 列表按顺序合并，后面的覆盖前面的
    state_dict.update(load_state_dict(path))
model.load_state_dict(state_dict, assign=True)   # strict=True，键必须齐
```

而 `server/worker.py` 固定传：

```text
高质量  (steps=50/30) -> [tbdub_base, tbdub_student]   + --inference-mode student --num-student-steps 2 --sigma-shift 1.0 --motion-from-latents
超高质量(steps=80)    -> [tbdub_base, tbdub_finetune]  + --inference-mode teacher --num-inference-steps 30
```

因为 V1.1 的文件是**完整模型**、键集合与 V1.0 相同，把它填进这两个槽位后：
Student 档最终生效 student 模型，Teacher 档最终生效 teacher 模型；`base` 槽位重复读同一个文件，
只多花一点读取时间，不额外占显存（state dict 按 key 覆盖，不会翻倍）。所以**代码零改动**。

### 3.4 权重目录结构（最终形态）

```text
checkpoints/
├── v1.1/                                  # 默认生效
│   ├── tbdub_teacher.safetensors          12,591,523,040
│   ├── tbdub_student.safetensors          12,591,523,048
│   └── config.json
├── v1.0/                                  # 可选保留
│   ├── tbdub_base.safetensors             12,585,231,496
│   ├── tbdub_finetune.safetensors         10,279,804,520
│   ├── tbdub_student.safetensors          25,181,688,152
│   └── config.json
├── tbdub_base.safetensors                 # ↓ 三个硬链接（0 额外磁盘），指向"当前生效"版本
├── tbdub_finetune.safetensors             #    代码只认这三个名字
├── tbdub_student.safetensors
├── null_prompt_emb.pt                     4,195,524        （V1.0/V1.1 通用）
├── Wan2.2_VAE.pth                         来自 /model/HuggingFace/Wan-AI/Wan2.2-TI2V-5B/
├── models_t5_umt5-xxl-enc-bf16.pth        来自 /model/HuggingFace/Wan-AI/Wan2.1-T2V-1.3B/
├── umt5-xxl/                              tokenizer，来自 .../Wan2.1-T2V-1.3B/google/umt5-xxl
└── hubert-large-ll60k/                    含 model.safetensors + config.json + preprocessor_config.json

dwpose_tools/models/
├── yolox_l_8x8_300e_coco_20211126_140236-d3bd2b23.pth                          217,289,556
└── rtmw-x_simcc-cocktail14_pt-ucoco_270e-384x288-f840f204_20231122.pth         369,720,404
```

### 3.5 切换权重版本

```bash
cd /AI/TBDub-master
bash deploy/switch_weights.sh v11     # 用 ModelScope 的 V1.1（默认）
bash deploy/switch_weights.sh v10     # 切回 HF v1.0 原版
```

切完不需要改任何配置，重启服务即可（已经在跑的 `inference.py` 不受影响，队列空时切换最安全）：

```bash
bash stop.sh && bash start.sh
curl -s http://127.0.0.1:8811/api/system/ready
```

### 3.6 手工重新下载单个权重（脚本之外的备用方式）

```bash
# V1.1（ModelScope，推荐）
curl -L -o checkpoints/v1.1/tbdub_teacher.safetensors \
  "https://modelscope.cn/api/v1/models/TaoLiveAIGC/TBDub/repo?Revision=master&FilePath=tbdub_teacher.safetensors"

# V1.0（hf-mirror 上的 HF tag v1.0）
curl -L -o checkpoints/v1.0/tbdub_base.safetensors \
  "https://hf-mirror.com/TaoLiveAIGC/TBDub/resolve/v1.0/tbdub_base.safetensors"

# 也可以直接用官方 CLI（装好环境后可用）
hf download TaoLiveAIGC/TBDub --revision v1.0 --local-dir checkpoints/v1.0 \
  tbdub_base.safetensors tbdub_finetune.safetensors tbdub_student.safetensors
export HF_ENDPOINT=https://hf-mirror.com     # 国内加速；hf-mirror 单线程约 300KB/s
modelscope download --model TaoLiveAIGC/TBDub --local_dir checkpoints/v1.1 \
  tbdub_teacher.safetensors tbdub_student.safetensors
```

大文件建议用 8 线程分片（见 `03_weights_v11.sh` 里的 `chunked()`），单线程会慢一个数量级。

### 3.7 hubert 必须存成 safetensors（否则一加载就报错）

`facebook/hubert-large-ll60k` 上游只提供 `pytorch_model.bin`。但 `transformers 5.1.0` 因为
torch 的 CVE-2025-32434，**禁止用 `torch.load` 加载 `.bin`**（除非 torch ≥ 2.6）：

```text
ValueError: Due to a serious vulnerability issue in `torch.load`, even with `weights_only=True`,
we now require users to upgrade torch to at least v2.6 in order to use the function.
This version restriction does not apply when loading files with safetensors.
```

`diffsynth` 的 `HubertProcessor` 走的就是 `HubertModel.from_pretrained(...)`，所以必须给
`hubert-large-ll60k/` 放一份 `model.safetensors`（生产机上那份也是 safetensors）。
`03_weights_v11.sh` 会在下载后自动转换一次：

```bash
/usr/local/miniconda3/envs/tbdub/bin/python - <<'PY'
import torch
from safetensors.torch import save_file
sd = torch.load("hubert-large-ll60k/pytorch_model.bin", map_location="cpu", weights_only=True)
save_file({k: v.clone().contiguous() for k, v in sd.items()}, "hubert-large-ll60k/model.safetensors")
PY
```

（另一条路是把 torch 升到 ≥ 2.6，但那样 mmcv 2.2.0 的 cu121/torch2.4 轮子又会对不上，不建议。）

### 3.8 权重完整性校验（**必做**，别只看文件大小）

这次部署踩到的最深的坑：**hf-mirror 背后的 CDN 有时会忽略 `Range` 头，直接返回整段数据**。
配合"续传 + 拼接"的分片下载器，结果是拼出来的文件**字节数完全正确、内容却是错的** ——
只看 `ls -l` 绝对查不出来。

所以：**下完必须核对 sha256**。官方摘要（来自 HF v1.0 tag 的 LFS oid 与 V1.1 `config.json`）：

| 文件 | 字节数 | sha256 |
|---|---|---|
| `v1.1/tbdub_teacher.safetensors` | 12,591,523,040 | `e73483dfde3b960d3d22f85e356c378067ccd3cff81f3b668d92167b8de605d0` |
| `v1.1/tbdub_student.safetensors` | 12,591,523,048 | `4e86900f9744b3650bfe58451ae89b3afba758233a3edf5947516268c1ea11e6` |
| `v1.0/tbdub_base.safetensors` | 12,585,231,496 | `78d0e9062fcae5f274b4f7a799283c6bed741d77c313f54093dcd90bd0ea2a78` |
| `v1.0/tbdub_finetune.safetensors` | 10,279,804,520 | `65731a07b7f0daa9191c657df747b6aa4002ff03f586803df163508af39fdf60` |
| `v1.0/tbdub_student.safetensors` | 25,181,688,152 | `27d6933f0e3651e3d2485cd274a7736c0eb1d406de15855f7347e0e5e8d120ae` |

```bash
cd /AI/TBDub-master/checkpoints
sha256sum v1.1/*.safetensors v1.0/*.safetensors
```

`deploy/lib_shard.sh` 里的 `shard_retry_verified <url> <out> <size> <sha256> [conns]` 已经把
"下完校验、不一致就删掉整份重下"做成自动循环，`03_weights_v11.sh` / `04_weights_v10.sh` 用的都是它。

> 备选：官方 CLI `hf download` / `modelscope download` 自己会处理分片和校验，但在本机实测
> **modelscope CLI 只有 614KB/s**（11.7GB 要 5 小时以上），而 8 线程分片能到 15–37MB/s，
> 所以这里仍然用自研分片下载器 + 强制校验。不管用哪种，最后都跑一遍上面的 `sha256sum`。

**单源坏件怎么办：** 实测 ModelScope 上的 `tbdub_teacher.safetensors` **反复取回都校验不过**
（字节数对、sha256 不符；同一源同一方法的 `tbdub_student` 却一次就过），说明是该文件在那个
源上的取回有问题，不是方法问题。这种"单源坏件"直接换源即可，`deploy/03b_teacher_fallback.sh`
就是干这个的：先 hf-mirror、再 huggingface.co，每一步都强制校验 sha256。

**镜像始终不老实就用 aria2c：** 如果某个文件换源后还是对不上哈希，说明镜像会**间歇性地忽略
`Range`、直接吐整段数据**。这时用 `deploy/03c_fetch_aria2.sh`：

```bash
bash /AI/TBDub-master/deploy/03c_fetch_aria2.sh teacher              # v1.1 teacher
bash /AI/TBDub-master/deploy/03c_fetch_aria2.sh v10student           # v1.0 student
CONNS=16 bash /AI/TBDub-master/deploy/03c_fetch_aria2.sh v10student  # 并发可调
```

aria2c 会逐请求校验 Content-Range，并用 `--checksum=sha-256=...` 在下载完成后校验整文件，
**坏数据会直接报错而不是静默通过**。实测：在分片下载器上反复校验失败的 teacher，用 aria2c 一次成功。
（aria2c 需先 `apt-get install -y aria2`；本机已装。）

---

## 4. 配置 `config.yaml`

`05_finalize.sh` 会从 `config.example.yaml` 复制一份并改两个关键项。至少要确认：

| 项 | 值 | 说明 |
|---|---|---|
| `admin_key` | `King` | 控制台登录密钥（现网约定） |
| `python_bin` | `/usr/local/miniconda3/envs/tbdub/bin/python` | **必须写绝对路径**，Worker 不经过交互 shell |
| `host` / `port` | `0.0.0.0` / `8811` | 站点根路径，无前缀 |
| `device` | `cuda:0` | 4090 |
| `mock_synthesis` | `false` | **有显卡必须 false**；无卡联调才 true |
| `default_steps` | `50` | 50=高质量(Student,2步) / 80=超高质量(Teacher,30步) |
| `max_video_duration_seconds` | `300` | 形象视频上限 5 分钟 |
| `max_audio_duration_seconds` | `180` | 驱动音频上限 3 分钟 |

其他默认即可（`task_retain_days: 15`、`work_clip_seconds: 12`、`upload_expire_hours: 24` …）。

---

## 5. 启动、日志、自检

```bash
cd /AI/TBDub-master
bash start.sh      # 后台起接口 + Worker（关终端也继续跑）
bash logs.sh       # 实时日志；logs.sh api / logs.sh worker 分开看
bash stop.sh       # 关接口和 Worker，不杀正在跑的 inference.py

curl -s http://127.0.0.1:8811/api/system/ready     # data.ready 应为 true，checks.ffmpeg/gpu/model 全 true
```

控制台：`http://<服务器IP>:8811/`，密钥 `King`。云平台安全组要放行 **8811**。

服务架构（改代码时的红线，官方文档第 7 节）：接口和 Worker 必须是两个进程；
`inference.py` 占 GPU，重启接口/Worker **都不能杀它**；查进程只用 `server/procs.sh` 的
`api_pids` / `py_module_pids`，**禁止 `pgrep -f` / `pkill python`**。

> **调试注意：** Worker 会主动清理"孤儿"推理进程 —— 它会扫描 `inference.py` 并把不在任务库里的
> 一起 `SIGTERM` 掉（日志形如 `killing orphan inference pid=7007`）。所以**手工跑 `inference.py`
> 必须先把服务 `bash stop.sh` 停掉**，否则调试进程会在几十秒内被杀、日志还可能是空的（输出被缓冲）。

---

## 6. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `pip` / `curl` / `apt` 卡住不动，几十分钟无进度 | IPv6 优先导致连接挂死 → 检查 `/etc/gai.conf` 是否含 `precedence ::ffff:0:0/96 100`；`getent ahosts pypi.tuna.tsinghua.edu.cn` 应返回 IPv4 |
| 国内镜像只有几百 KB/s | 走了集群代理 → 把域名加进 `no_proxy`；apt 对 `mirrors.aliyun.com` 设 `DIRECT` |
| `No matching distribution found for torch==2.4.1` | `mirrors.aliyun.com/pytorch-wheels` 只到 2.1.x → 改用 pip 默认索引（清华镜像里的 PyPI torch 就是 cu121 构建） |
| mmcv 源码编译报 CUDA 版本不匹配 | 本机 nvcc 13.2 vs torch cu121 → 别编译，用 mmcv 2.2.0 的 cu121/torch2.4 轮子（§3.2） |
| `undefined symbol: _ZN2at4_ops10zeros_like...` | mmcv 2.1.0 的 torch2.1 轮子与 torch 2.4 ABI 不匹配 → 跑 `deploy/fix_mmcv_mmdet.sh` |
| `AssertionError: MMCV==2.2.0 is used but incompatible` | mmdet 的版本上限写死 `<2.2.0` → 同一脚本会放宽为 `2.3.0` 并升级到 mmdet 3.3.0 |
| pip 反复去编译 mmdet 的依赖 | 安装顺序错了 → 先装完 requirements，最后跑 mmcv 修复 |
| pip 报 `Failed to build 'chumpy' ... No module named 'pip'` | mmpose 依赖 chumpy，其 setup.py 直接 `import pip` → `pip install --no-build-isolation chumpy` |
| `ValueError: ... require users to upgrade torch to at least v2.6` | hubert 目录里只有 `.bin`，transformers 5.x 拒绝加载 → 转成 `model.safetensors`（§3.7） |
| 手工跑的 `inference.py` 几十秒后被杀、日志空白 | 服务在跑，Worker 把它当孤儿推理清理了 → 先 `bash stop.sh`（§5） |
| 下载大文件时拼出来的文件比预期大 | CDN 忽略 Range 返回了整段 → 用 `deploy/lib_shard.sh` 的分片下载（逐片校验 + 截断） |
| **文件大小正确但加载出垃圾/报错** | CDN 忽略 Range 导致内容错位 → **对 sha256**（§3.8），不一致就用 `shard_retry_verified` 重下 |
| 某个文件反复重下都对不上哈希，同目录别的文件却正常 | 单源坏件（实测 ModelScope 的 `tbdub_teacher`）→ 换源，用 `deploy/03b_teacher_fallback.sh` |
| `ready.checks.model=false` | `checkpoints/` 缺三个 safetensors 或 `hubert-large-ll60k/`；或硬链接是断链，`ls -l` 检查 |
| 就绪但一合成即失败 | T5 / VAE / `umt5-xxl` 是断链或没拷；看 `data/tasks/<id>/task.log` |
| `import cv2` 报 libGL | 缺 `libgl1` |
| 自己的 `pkill`/脚本把当前 shell 一起杀了 | `pkill -f` 会匹配到执行它的那条命令行（含脚本路径）→ 用 `pkill -f "路径关键字"` 时写成 `03_[m]odels.sh` 这类正则，或先 `pgrep` 再按 PID kill |

---

## 7. 本机实测记录

| 项 | 结果 |
|---|---|
| 机器 | `cpod-1wewa83bx7vs`，RTX 4090 48GB，14 vCPU / 64GB，Ubuntu 22.04.5，内核 6.12.101-compshare |
| conda 环境 | `/usr/local/miniconda3/envs/tbdub`，Python 3.10；torch 2.4.1+cu121（`cuda.is_available()=True`） |
| DWPose 栈 | mmcv 2.2.0 / mmdet 3.3.0 / mmpose 1.3.2，`mmcv.ops`、`mmdet.apis`、`mmpose.apis` 均可导入 |
| 权重源速度 | ModelScope 13–37MB/s；hf-mirror 8 线程约 3.4MB/s；huggingface.co 直连 0.32MB/s |
| 出口带宽 | 容器整体约 17MB/s（多个下载同时跑会互相分摊） |
| 共享模型盘 | `/model`（UPFS 50T，已用 12T），含 `Wan-AI/Wan2.2-TI2V-5B`、`Wan-AI/Wan2.1-T2V-1.3B` |
| 服务自检 | `GET /api/system/ready` → `ready=true`，`checks.ffmpeg/gpu/model` 全 true，`worker_alive=true` |
| 端到端合成 | 8 秒 1080×1620 真人素材 + 8 秒音频，Student 档（2 步）出片成功：1080×1620 / 25fps / 8.0s 带音轨，显存峰值 **30.9GB**，含模型加载+预处理约 2 分钟 |
| 权重映射验证 | `worker.py` 的 `[tbdub_base, tbdub_student]`（V1.1 硬链接）加载通过，日志 `[TBDub] mode=student, source_frames=200, clips=3, steps=2, sigma_shift=1.0, motion_from_latents=True` |
| 权重哈希校验 | `v1.1/tbdub_student`、`v1.0/tbdub_base`、`v1.0/tbdub_finetune` 与官方 sha256 一致；`v1.1/tbdub_teacher` 首次下载大小正确但哈希不符，已用带校验的下载器重下 |
| 最终状态 | `v1.1/tbdub_teacher` + `v1.1/tbdub_student` 均通过官方 sha256；`tbdub_base/finetune/student` 三个硬链接指向已校验的 V1.1 文件，无断链；`/api/system/ready` → `ready=true` |
| V1.0 可选集 | `v1.0/tbdub_base`、`v1.0/tbdub_finetune` 已校验通过；`v1.0/tbdub_student`(25.18GB) 仍在后台下载（hf-mirror 夜间限速到约 1.7MB/s）。用 `bash deploy/03c_fetch_aria2.sh v10student` 可随时续传，下完会自带 sha256 校验。V1.0 只影响"用 V1.0 文件跑高质量档"，V1.1 不受影响 |
