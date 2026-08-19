# Mobile Power OAI Platform

5G 手机功耗实验平台，由 PC 端 FastAPI/React 控制平台与 Android 采集 Agent 组成，对接 OAI gNB 控制与 PHY 快照接口。

## 目录

- `experiment_platform/`：PC 端后端、Web 前端与平台测试。
- `android/`：手机端实验任务、负载和遥测采集 Agent。
- `scripts/`：OAI 信道快照服务和路由配置脚本。
- `deploy.ps1`：构建并部署平台与 Android App。

## 启动 PC 平台

```powershell
python -m pip install fastapi "uvicorn[standard]" pydantic httpx pandas pyarrow pytest
cd experiment_platform/web
npm install
npm run build
cd ../..
python -m experiment_platform.backend.server
```

默认访问地址：`http://127.0.0.1:8900`。本地 OAI 地址和 token 写入 `experiment_platform/config.local.json`，该文件不入库。

## 验证

```powershell
python -m pytest experiment_platform/backend/tests -q
cd experiment_platform/web
npm run build
cd ../..
```

更详细的平台说明见 `experiment_platform/README.md`，手机端说明见 `android/README.md`。
