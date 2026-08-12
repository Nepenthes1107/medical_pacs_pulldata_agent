# Medical PACS PullData Agent

这是一个单机可运行的 PACS 数据补拉与链路诊断 MVP。项目保留 PACS C-FIND/C-MOVE、RabbitMQ 异步任务、storescp 接收、本地 DICOM 扫描、MySQL 状态机和规则 Agent 诊断能力，不依赖旧 uAF 平台组件。

## 链路说明

```text
Orthanc / Mock PACS
  -> C-FIND 查询 Study / Series
  -> FastAPI 创建 DownloadTask
  -> RabbitMQ download_tasks
  -> Downloader Worker 执行 C-MOVE
  -> storescp 接收 DICOM
  -> Checker 扫描 data/fileserver，裁定完整性（唯一裁判）
  -> MySQL 更新任务状态
  -> 仅失败时发 repull_events -> Agent 重新诊断并提补拉方案（须人工审批）
```

组件职责边界：

| 组件 | 职责 | 不做什么 |
| --- | --- | --- |
| Downloader Worker | 执行 C-MOVE；失败时发 `failure_stage=move` 失败事件 | 不判定完整性 |
| Checker | **完整性唯一裁判**：`local >= expected` → success；`expected` 缺失 → unverified；超时仍不完整 → fail。success/unverified 同事务直接结束关联 `AgentRun`，不发消息；仅超时不完整发 `failure_stage=integrity` | 不做根因诊断 |
| Agent Worker | 对**已确认失败**的任务做根因诊断、产出补拉方案 | 不重复证明完整性、不自动执行补拉（必经人工审批） |

## Windows + Ubuntu 虚拟机开发说明

本项目推荐在 Ubuntu 虚拟机内运行 Docker Compose。Windows 只作为宿主机和 VS Code 客户端使用。

请将项目放在 Ubuntu 文件系统中，例如：

```bash
mkdir -p ~/projects
cd ~/projects
git clone <repo> medical-pacs-pulldata-agent
cd medical-pacs-pulldata-agent
```

Windows 上的 DICOM 数据需要复制到 Ubuntu 项目目录下：

```text
medical-pacs-pulldata-agent/data/dicom_samples/
```

可用 VS Code Remote SSH 上传，也可以在 Windows PowerShell 中执行：

```powershell
scp -r "D:\UII_PACS_Agent_Data\1.20251203000331" <ubuntu_user>@<ubuntu_ip>:/home/<ubuntu_user>/projects/medical-pacs-pulldata-agent/data/dicom_samples/
```

## 测试数据集

原始 DICOM 数据位于 `data/dicom_samples/1.20251203000331/`，共 30 个 series、4459 个 `.dcm` 实例（6.0 GB）。日常开发测试使用按字母序的**前 10 个 series**，约 2100 个实例，已足够覆盖多 series 导入、C-FIND、C-MOVE 补拉、Checker 扫描、Agent 诊断等核心链路。

使用 `--series-limit 10` 导入的 series（字母序前 10）：

```text
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000007   (  34 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000070   (  34 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000133   ( 116 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000360   (  62 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000484   (  34 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000547   (  34 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000610   (  29 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000663   (  29 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000716   ( 962 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300002644   ( 802 实例)
```

> 注：每个 series 目录下还可能包含 `.mhd`（MetaImage 头）、`.vol`（体数据）、`.png`（预览图）、`series.mark` 等辅助文件，导入脚本只读取 `.dcm` 文件。

测试 StudyInstanceUID：**`1.20251203000331`**

## 本地启动

先启动基础服务：

```bash
docker compose up -d mysql redis rabbitmq orthanc
```

再启动业务服务：

```bash
docker compose up --build -d pull-data-api pull-data-storescp pull-data-worker pull-data-checker agent-api
```

健康检查：

```bash
curl http://localhost:8000/health
curl http://localhost:8001/health
```

## 导入测试 DICOM 到 Orthanc

导入前 10 个测试 series（约 2100 个实例）：

```bash
python3 scripts/import_dicom_to_orthanc.py \
  --input-dir data/dicom_samples/1.20251203000331 \
  --orthanc-url http://localhost:8042 \
  --series-limit 10
```

导入后验证：

```bash
curl http://localhost:8042/studies
# 预期返回 1 个 Study、10 个 Series、~2100 个 Instances
```

如需导入全部 series（4459 个实例，约 6 GB），去掉 `--series-limit` 参数即可。

Orthanc 默认 HTTP 端口是 `8042`，DICOM 端口是 `4242`，AE Title 是 `ORTHANC`。

## PACS 测试

C-ECHO：

```bash
curl -X POST http://localhost:8000/pacs/orthanc-local/echo
```

C-FIND Study：

```bash
curl "http://localhost:8000/pacs/orthanc-local/studies?study_instance_uid=1.20251203000331"
```

C-FIND Series：

```bash
curl http://localhost:8000/pacs/orthanc-local/studies/1.20251203000331/series
```

## 创建补拉任务

Study 级补拉默认会先 C-FIND Orthanc，并将真实 Study/Series 元数据写入 MySQL：

```bash
curl -X POST http://localhost:8000/tasks/pull \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "orthanc-local",
    "level": "study_level",
    "study_instance_uid": "1.20251203000331",
    "priority": 5
  }'
```

Series 级补拉：

```bash
curl -X POST http://localhost:8000/tasks/pull \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "orthanc-local",
    "level": "series_level",
    "study_instance_uid": "1.20251203000331",
    "series_instance_uid": "12.2.1107...00716_001"
  }'
```

如确实需要手动创建任务，可传 `"skip_pacs_find": true`，同时建议显式传入 `expected_image_number`。

查询任务：

```bash
curl http://localhost:8000/tasks/{task_id}
```

重试任务：

```bash
curl -X POST http://localhost:8000/tasks/{task_id}/retry -H "Content-Type: application/json" -d '{"force": false}'
```

取消任务：

```bash
curl -X POST http://localhost:8000/tasks/{task_id}/cancel
```

扫描本地文件：

```bash
curl http://localhost:8000/studies/1.20251203000331/local-scan
```

## 端到端验收

```bash
# 1. 启动基础服务
docker compose up -d mysql redis rabbitmq orthanc

# 2. 导入前 10 个测试 series 到 Orthanc
python3 scripts/import_dicom_to_orthanc.py \
  --input-dir data/dicom_samples/1.20251203000331 \
  --orthanc-url http://localhost:8042 \
  --series-limit 10

# 3. 查看 Orthanc 是否有 Study (预期 10 Series, ~2100 Instances)
curl http://localhost:8042/studies

# 4. 启动业务服务
docker compose up --build -d pull-data-api pull-data-storescp pull-data-worker pull-data-checker agent-api

# 5. 测试 C-ECHO
curl -X POST http://localhost:8000/pacs/orthanc-local/echo

# 6. 查询 Study
curl "http://localhost:8000/pacs/orthanc-local/studies"

# 7. 创建补拉任务
curl -X POST http://localhost:8000/tasks/pull \
  -H "Content-Type: application/json" \
  -d '{"source_id":"orthanc-local","level":"study_level","study_instance_uid":"<StudyInstanceUID>"}'

# 8. 查看接收文件数
find data/fileserver -type f | wc -l

# 9. 查看 MySQL 任务状态
docker exec -it pulldata-mysql mysql -u pulldata -ppulldata pulldata -e "
SELECT task_id, study_instance_uid, status, expected_image_number, last_error
FROM download_task
ORDER BY id DESC
LIMIT 5;
"

# 10. 查看 storescp 接收记录
docker exec -it pulldata-mysql mysql -u pulldata -ppulldata pulldata -e "
SELECT image_name, study_instance_uid, series_instance_uid, file_path, received_at
FROM storescp_image
ORDER BY id DESC
LIMIT 10;
"
```

## Agent 诊断

按任务诊断：

```bash
curl -X POST http://localhost:8001/agent/diagnose \
  -H "Content-Type: application/json" \
  -d '{"task_id": "替换为任务 UUID", "source_id": "orthanc-local"}'
```

按 Study 诊断：

```bash
curl -X POST http://localhost:8001/agent/diagnose \
  -H "Content-Type: application/json" \
  -d '{"study_instance_uid": "1.20251203000331", "source_id": "orthanc-local"}'
```

Agent 重试：

```bash
curl -X POST http://localhost:8001/agent/retry \
  -H "Content-Type: application/json" \
  -d '{"task_id": "替换为任务 UUID"}'
```

## 常见失败场景

| 场景 | 诊断方向 |
| --- | --- |
| PACS 不可达 | 网络、端口、AE Title、Orthanc 服务状态 |
| Study 查不到 | StudyInstanceUID 或 StudyDate 条件错误，上游未入库 |
| 任务停留 in_queue | RabbitMQ 队列堆积或 pull-data-worker 未运行 |
| downloaded 后本地文件为 0 | storescp 未收到文件，Move Destination AE 或 11112 端口配置错误 |
| 文件数量不完整 | C-MOVE 部分失败、PACS 数据不完整或 storescp 保存失败 |
| expected count 缺失 | checker 只能标记 unverified，不会把任务误判为 success |

## 数据库

数据库初始化脚本位于 `config/schema.sql`。Compose 会在 MySQL 首次启动时自动执行该脚本，创建 `study`、`series`、`download_task`、`local_dicom_file`、`archive`、`storescp_image` 等表。
