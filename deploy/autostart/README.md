# 开机自启

```bash
bash deploy/autostart/install_autostart.sh              # 默认：只有检测到 systemd 才装
bash deploy/autostart/install_autostart.sh --youyun     # 优云智慧算平台专用（supervisord）
bash deploy/autostart/install_autostart.sh --cron       # crontab @reboot（需 cron 守护在跑）
bash deploy/autostart/install_autostart.sh --uninstall  # 取消
```

## ⚠️ 先判断"这台机器适不适用"，不要盲目装

| 机器 | 机制 | 通用性 |
|---|---|---|
| 装了 systemd 的虚拟机 / 物理机 | systemd | **通用做法**，默认就会用这个 |
| **优云智慧算平台**的容器 Pod（镜像自带 supervisord） | supervisord | **仅该平台适用**，别的服务器不适用 |
| 确认本机 cron 守护在跑的机器 | crontab `@reboot` | 通用，但依赖 cron 守护 |

**默认行为**：脚本只检测 systemd。
有 systemd → 直接装 systemd 单元；
**没有 systemd → 脚本什么都不改**，只打印提示，要求先确认"这台机器是不是优云智慧算平台、
该镜像是不是用 supervisord 管服务"，确认后才用 `--youyun` 显式安装。不适用就别做自启
（手工 `bash start.sh` / `stop.sh` 一样能用）。

> 原因：supervisord 这条路是**优云智慧算平台容器镜像特有的**（它的 entrypoint 会起 supervisord，
> `[include] /etc/supervisor/conf.d/*.conf`）。换到别的服务器上既没有 supervisord、也可能有 systemd，
> 套过去只会白折腾甚至把启动流程搞乱。

## ⚠️ 装了自启之后，启停方式会变（这是守护进程的正常表现）

| 你想做的事 | 用什么命令 |
|---|---|
| 看状态 | `supervisorctl status tbdub-api tbdub-worker` |
| 只重启接口（不动 Worker / 推理） | `supervisorctl restart tbdub-api` |
| 只重启 Worker（不杀正在跑的 inference.py） | `supervisorctl restart tbdub-worker` |
| **真正停掉服务** | `supervisorctl stop tbdub-api tbdub-worker` |
| 恢复 | `supervisorctl start tbdub-api tbdub-worker` |

**`bash stop.sh` 不再能"停住"服务**：它会把进程停掉，但 supervisord 发现进程没了会立刻拉起来。
这是"开机自启/崩溃自动拉起"的必然代价，不是 bug。要真停就用上面的 `supervisorctl stop`。
（没装自启时，`bash start.sh / stop.sh / logs.sh` 仍然是原样的用法。）

日志位置没变，仍然写 `data/api.log` 与 `data/worker.log`，`bash logs.sh` 照常可用；也可以 `supervisorctl tail -f tbdub-api`。

## 三种方式的共同约定

1. **只托管"接口 + 拉起 Worker"**：入口都是 `start_server.sh`（前台 `python -m server`，无 `--reload`），
   它内部先确保 Worker 在，再 exec 接口。
2. **停/重启不杀正在跑的合成**：supervisord 用 `stopasgroup=false` / `killasgroup=false`，
   systemd 用 `KillMode=process`。这样重启接口不会毁掉一条跑了半小时的 `inference.py`。
3. **日志位置不变**：仍然写 `data/api.log` 与 `data/worker.log`，`bash logs.sh` 照常用。
4. 装了自启之后，**不要再手工 `bash start.sh`**（会提示已在运行；真要用手工方式，先 `supervisorctl stop`）。
5. supervisord 里 Worker 与接口是**两个 program**，各自独立重启：接口崩了不会带走 Worker，
   Worker 崩了会自动拉起（这正是本仓库"接口与 Worker 分进程 + 重启接口不杀推理"的落地方式）。

## 常用命令

```bash
# supervisord 环境
supervisorctl status tbdub-api tbdub-worker
supervisorctl restart tbdub-api        # 只重启接口，不碰 Worker / inference.py
supervisorctl stop tbdub-api           # 只停接口

# systemd 环境
systemctl status tbdub
systemctl restart tbdub                # 同样不会杀 inference.py

# 无论哪种，服务自身的启停/日志命令仍然可用
bash stop.sh && bash start.sh          # 只在没装自启时用
bash logs.sh
```

## 自启动验证

装完不必真重启机器，用"杀掉进程看是否自动拉起"来验证：

```bash
supervisorctl status tbdub-api         # 记下 pid
kill <api_pid>                          # 模拟接口崩溃
sleep 20; supervisorctl status tbdub-api   # 应重新 RUNNING
curl -s http://127.0.0.1:8811/api/system/ready
```

还可以用 `supervisorctl reload` 把 supervisord 整个重启一次 —— 相当于"开机瞬间"的完整演练，
所有 autostart 的程序（含本服务）都应当自己起来。
