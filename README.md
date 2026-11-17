# cc-manifest-lab

CC_Codex 二期（26.11）**数据工程**产品线：**湖区落地清单台**（Litestar + Preact CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**湖区落地批次 → 对象指纹登记 → 清单闭合 → 可见性对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（落地清单 / 对象登记闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、Litestar、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，侧栏），靛青竖纹 + 亮青强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 湖区落地批次列表（收集中 / 已闭合）
2. 向批次登记对象键与内容指纹
3. 闭合清单（对象数固化，拒绝空批）
4. 可见性对账报告（闭合批 vs 开放批 vs 孤儿对象）

## 批量写入（26.11 增量）

- **提交边界只启用一条：整批单事务。** 一次请求里任一键撞库，整笔回滚，
  前面已写入的键不保留；不接受逐键独立事务，也不混用。
- **核齐**：可选 `expected_count` 声明批次对象数，与实交枚数不符直接拒绝；
  事务内再核对“声明数 / 本次实写键数 / 批次对象总数”三者一致，偏了就回滚。
- **并发**：同批写入走进程内串行闸 + `BEGIN IMMEDIATE`，两侧灌数员同批抢写
  不会双写互盖；拿不到闸的一方收到 409 与一条失败结算。
- **回看**：每次批量请求（成功或失败）落一行 `bulk_runs`，含成功键、失败键、
  回滚标记与成败条数；页面「批量写入」页签、批次详情、失败卡片三处可读，
  本地库可直接查表，清洗任务可 `GET /api/bulk-runs` 回看。

接口：`POST /api/batches/{id}/objects/bulk`、`GET /api/bulk-runs`、
`GET /api/bulk-runs/{run_id}`。

预留切口（未做，供后续功能题）：清单换版生效窗、闭合作废回开放、跨区依赖冻结、清单回灌等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-manifest-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6900/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6900**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
