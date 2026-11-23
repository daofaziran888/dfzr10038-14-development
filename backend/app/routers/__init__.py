"""业务路由汇总：专用血缘批次路由在前，通用资源路由按模块 key 分派在后。"""
from __future__ import annotations

from app.routers import lineage, resource

# lineage 的 /api/lineage/* 是固定前缀，必须先于 resource 的 /api/{module} 注册
ROUTERS = [lineage, resource]
