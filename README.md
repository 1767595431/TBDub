# TBDub 数字人配音服务

淘宝天猫开源 [TBDub](https://github.com/TaoLiveAIGC/TBDub) 的口型同步配音，本仓库在官方推理之上加了：

- 网页控制台（密钥门禁）
- 形象上传 / 转码 / 作品排队
- 给业务前端对接的 HTTP 接口

官方单次推理说明见 [`README copy.md`](README%20copy.md)。换机安装、权重、进程红线见 [`部署安装文档.md`](部署安装文档.md)。业务前端对接见 [`TBDub_API接口文档.md`](TBDub_API接口文档.md)。

**不要改官方合成引擎**（`inference.py`、`preprocessing.py`、`video_utils.py`、`diffsynth/`、`dwpose_tools/`）。服务层只做裁工作视频、排队、进度、过期清理。

---

## 现网

| 项 | 值 |
|---|---|
| 控制台 / API | http://10.60.19.103:8811/ |
| 端口 | **8811**（HTTP，站点根路径，无额外前缀） |
| 密钥 | `config.yaml` 的 `admin_key` |
| GPU | 同时只跑一条合成 |

```bash
curl -s http://127.0.0.1:8811/api/system/ready
```

`data.ready == true` 且 `checks.ffmpeg / gpu / model` 均为 true 即可提交任务。

---

## 进程

```text
浏览器 / 业务前端
    ↓  :8811
python -m server          # FastAPI + 静态控制台。不要 --reload
    ↓
python -m server.worker   # 转码形象、取队列、拉起 inference.py
    ↓
inference.py              # 占 GPU。重启接口或 Worker 都不要杀它
```

| 脚本 | 作用 |
|---|---|
| `bash start.sh` | **后台启动**接口 + Worker，关终端也继续跑 |
| `bash stop.sh` | **关闭**接口 + Worker，不杀正在跑的 `inference.py` |
| `bash logs.sh` | **实时日志**（接口 + Worker）。`bash logs.sh api` 只看接口 |
| `bash start_server.sh` | 前台启动（占住当前终端） |
| `bash restart_api.sh` | 只重启接口（前台） |
| `bash restart_worker.sh` | 只重启 Worker，不杀正在跑的推理 |

```bash
cd /root/AI/TBDub
bash start.sh
bash logs.sh
bash stop.sh
```

---

## 产品约定

| `steps` | 页面 | 引擎 | 实际步数 |
|---|---|---|---|
| 50（默认） | 高质量 | Student | 2 |
| 80 | 超高质量 | Teacher | 30 |
| 30 | 标准（接口仍接受） | Student | 2 |

- 形象视频：≤ 5 分钟、≤ 2GB
- 驱动音频：≤ 3 分钟、≤ 300MB
- 作品：完成 / 失败 / 取消后保留 **15 天**，到期整条删除；排队和合成中不删
- 长形象只切约 12 秒给官方，官方再正放倒放对齐音频

---

## 目录

```text
admin/                 本仓库控制台静态页
server/                FastAPI + Worker（本仓库加的，改行为只动这里）
diffsynth/             官方推理库（只读）
dwpose_tools/          官方人脸检测（只读；.pth 权重不进 git）
checkpoints/           模型权重（不进 git，约 60GB）
data/                  运行时库、上传、成片（不进 git）
inference.py           官方入口，由 Worker 调用
config.yaml            本机配置（不进 git，含密钥）
config.example.yaml    配置模板
```

从 `config.example.yaml` 复制为 `config.yaml`，至少改 `admin_key` 和 `python_bin`（写成 conda 环境的绝对路径）。

---

## 安装

完整步骤、权重清单、软链陷阱、换机搬迁见 [`部署安装文档.md`](部署安装文档.md)。摘要：

1. Ubuntu + NVIDIA GPU + 驱动，`ffmpeg` 在 PATH，Python **3.10**
2. conda 环境 `tbdub`，先装 GPU 版 PyTorch（现网 `2.4.1+cu121`），再 `requirements.txt` + `requirements-server.txt`，再 `mim install "mmcv==2.1.0"`
3. 放入 `checkpoints/` 与 DWPose 两个 `.pth`（本仓库 git 不含权重）
4. `cp config.example.yaml config.yaml` 后启动

本仓库目前 **没有 git remote**。`config.yaml`、`data/`、权重不要提交。

---

## 文档

| 文件 | 给谁 |
|---|---|
| `README.md` | 本仓库入口（你正在看的这份） |
| `部署安装文档.md` | 换机 / 重装 / 后续 AI |
| `TBDub_API接口文档.md` | 业务前端对接（不含管理后台） |
| `README copy.md` | 官方 TBDub 推理 README |
| `config.example.yaml` | 配置模板 |

官方论文与权重：[arXiv](https://arxiv.org/abs/2609.06144) · [项目页](https://taoliveaigc.github.io/TBDub/) · [Hugging Face](https://huggingface.co/TaoLiveAIGC/TBDub)

官方代码与模型权重为 [Apache License 2.0](LICENSE)。第三方依赖与权重仍受各自许可证约束。
