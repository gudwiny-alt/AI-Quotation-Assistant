# 华为官网逐站冻结测试包 `.143`

## 恢复起点

- 源码起点：`.141` 对应提交 `de55b85dca0f07f7492b238b0258666192944dc4`。
- 失败的 `.142` 改动已完整保存于 Git stash `failed-live-shape-142-preserved-2026-08-28`，未覆盖、未删除。
- 京东稳定基线：提交 `f9d543ad9528797301ab7c2f9b83d4bb5992f0ee`，标签 `jd-stable-2026-08-28`。

## 本站唯一生产逻辑变更

1. 华为官网搜索结果：当当前 VMALL React 页面将跳转事件绑定在整张 `*-searchProduct` 商品卡上时，点击整卡，并有界等待同页或单一新页进入数字商品详情路由。
2. 华为官网价格稳定：React 重绘造成价格节点短暂消失时，保留上一次已验证报价的稳定计数；只有实际价格或配置变化才重置。
3. `src/quote_app/sites/jd.py` 保持零差异；小米、OPPO、vivo、荣耀、苹果适配逻辑未改动。

## 验证

- 华为官网契约：82 项通过。
- 全部品牌官网＋京东适配器：586 项通过。
- 界面与版本标识：55 项通过。
- 完整自动测试：2,875 项在沙箱内通过；唯一需要本机回环端口的持久化测试单独复跑通过，合计 2,876 项。
- Ruff：通过。
- Mypy（华为适配器）：通过。
- PyInstaller：构建通过。
- macOS 深层临时签名与严格校验：通过。
- 打包归档已确认包含 `quote_app.app`、`quote_app.sites.jd` 和 `quote_app.sites.official_brands.huawei`。

## 测试包

- 路径：`dist-official-huawei-freeze-143/福建移动铺货报价助手.app`
- 主程序 SHA-256：`0f4bde6598f67e9285aecffabba1cd884464197f4b342d186b9e53d0b62b91a7`
