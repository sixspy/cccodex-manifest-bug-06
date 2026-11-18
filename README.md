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

## 争用下的唯一正式闭合

两名值班同一秒点闭合时，后端一次裁决，正式闭合凭证全仓同批只留一条：

- **先手**：在 `BEGIN IMMEDIATE` 写锁内写唯一闭合凭证（`close_vouchers.batch_id` UNIQUE），
  固化数、编排闭合记录与凭证同一次事务落库；队列行转 `已裁决` 并带凭证号。
- **后到**：整笔回滚，不写固化数、不新增编排闭合记录；队列行转 `已回滚`，
  死信队列（`close_dead_letters`）留回滚因。两种提交命运由表 CHECK 与唯一索引硬拦，不会同挂。
- **同判定**：凭证固化数 = 编排记录数 = 批次固化数 = 实到已闭合对象数，闭合时校验，
  不一致即整笔回滚；可见性对账把缺凭证 / 双写 / 错固化数都记为错位。

四处同查（同一批次、同一条正式闭合）：

| 位置 | 接口 | 看什么 |
|------|------|--------|
| 占用卡片 | `GET /api/batches/{id}/close-spot` | 占用芯片 + 凭证号或死信因 |
| 闭合队列 | `GET /api/close-queue` | 每次点闭合一条：已裁决带凭证号 / 已回滚带回滚因 |
| 批次附页 | 批次详情页底部 | 该批的闭合请求与死信落点 |
| 编排回看台 | `GET /api/close-board` | 每批只呈现一条正式闭合，后到计为回滚 |

争用复现单：`tests/test_close_contention.py`（真实线程抢同一 SQLite 文件）；
接口层同查：`tests/test_close_api.py`。

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
