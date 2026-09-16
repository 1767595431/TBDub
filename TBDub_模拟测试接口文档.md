# TBDub 模拟测试接口（无 GPU）

给**没显卡、没模型权重**时的前端联调用。正式对接仍以 [`TBDub_API接口文档.md`](TBDub_API接口文档.md) 为准，本文不重复罗列路径和字段。

模拟环境与正式环境：

- **路径、方法、请求参数、响应信封、作品状态机、进度字段完全相同**
- **唯一差别**：合成任务不跑 `inference.py`，Worker 按五阶段推进度，再用 ffmpeg 把形象预览视频循环到驱动音频时长并叠声音

不要把本文写进正式对接范围。有显卡后关掉模拟，前端不用改调用代码。

---

## 1. 一键启动（Windows）

仓库根目录双击：

```text
启动测试接口.bat
```

脚本会设置 `TBDUB_MOCK_SYNTHESIS=1`（强制走模拟合成），前台启动 `python -m server`（接口会自己拉起 Worker），监听 **8811**。

关掉这个黑窗口即停止接口和 Worker，不需要另开停止脚本。

本机需要：

| 项 | 说明 |
|---|---|
| Python | 能执行 `python`，并已 `pip install -r requirements-server.txt` |
| ffmpeg / ffprobe | 在 `PATH` 里（形象转码和假成片都要用） |
| **不需要** | NVIDIA 驱动、CUDA、PyTorch GPU、`checkpoints/` 权重 |

没有 `config.yaml` 时脚本会从 `config.example.yaml` 复制一份。控制台密钥默认是配置里的 `admin_key`（没有配置文件时代码默认 `King`）。

连通性：

```bat
curl -s http://127.0.0.1:8811/api/system/ready
```

`data.ready` 应为 `true`。模拟模式下即使没有 GPU / 权重，`checks.gpu` 和 `checks.model` 也会是 `true`，避免前端卡在「服务未就绪」。字段集合与正式接口相同，**不会多出 `mock_synthesis` 之类的对接字段**。

前端 Base URL：

```text
http://127.0.0.1:8811
```

跨域已允许。个人媒体仍须请求头 `X-User-Id`，与正式版相同。

---

## 2. 对接怎么用

请完整遵循 [`TBDub_API接口文档.md`](TBDub_API接口文档.md) 的流程：

1. `GET /api/system/ready`
2. 上传形象 → 等到 `bake_status=ready`（转码是真 ffmpeg，不是假的）
3. 上传音频 → `POST /api/tasks/create`
4. 轮询 `GET /api/tasks/{task_id}`，看 `status` / `progress` / `progress_message` / `remaining_seconds`
5. `status=done` 后用 `result_path` 预览、用 `/download` 下原片

取消、重试、排队、`X-User-Id`、15 天过期，行为与正式版一致。

---

## 3. 合成时到底假在哪

| 步骤 | 模拟环境 | 正式环境 |
|---|---|---|
| 形象分片上传 / 转码 | 真 ffmpeg：封面 + 静音预览 | 相同 |
| 音频分片 / 创建作品 / 排队 | 相同 | 相同 |
| 合成 `status=run` 进度 | 按正式文档五阶段推进度（约 24 秒，超高质量约 36 秒） | 真实 GPU，数分钟到数十分钟 |
| 成片画面 | 循环形象**静音预览**（没有预览才用上传原片） | 模型口型同步 |
| 成片声音 | 用户提交的驱动音频 | 相同（叠到成片上） |
| 成片时长 | 与驱动音频对齐 | 与驱动音频对齐 |
| 口型 | **没有** | 有 |

假成片仍走正式文件布局：`/api/tasks/{id}/preview`、`/api/tasks/{id}/download`。前端可以播、可以下，只是画面不会对嘴型。

同一形象第一次模拟会写一份假预处理缓存，第二次进度文案会变成「复用形象预处理」，方便联调两种文案。

---

## 4. 有显卡之后

1. 关掉 `启动测试接口.bat` 那个窗口，不要再用它起服务
2. `config.yaml` 里 `mock_synthesis: false`，且不要设置环境变量 `TBDUB_MOCK_SYNTHESIS`
3. 按 [`部署安装文档.md`](部署安装文档.md) 装驱动、PyTorch、权重
4. `bash start.sh` 起正式服务

前端继续只用 [`TBDub_API接口文档.md`](TBDub_API接口文档.md)，不必为模拟环境单独写一套调用。
