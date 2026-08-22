# Apple `.115` 恢复基线

本分支保存用户确认的 `.115` 阶段源码，不包含后续的 Apple 截图条件调整。

## 原始产物

- 应用：`dist-official-apple-115/福建移动铺货报价助手.app`
- 主可执行文件 SHA-256：`ff1c607b684ae12738423d386784508e3531461725b105c5ef03ef30f1967f21`
- PyInstaller PYZ SHA-256：`ca653f0df8cf7e694d845c2e60e1639b9198579a4da94d92b4d541ff725ebec1`
- Apple 适配器源码 SHA-256：`1eef1072d3ffc9ac8338819d32e1e66213b55ce9ab6d1129d7589926b8ec6eb8`
- Apple 适配器语义字节码摘要：`2b6cfbad6d40fbdd8b4599ad15fac2aa5521a9e30868728b7b6866a88f3510a5`

## 恢复校验

已将以下运行关键模块与原始 `.115` 安装包中的字节码进行语义对比，结果一致：

- `quote_app.app`
- `quote_app.excel.quote_writer`
- `quote_app.services.web_pipeline`
- `quote_app.sites.official`
- `quote_app.sites.official_brands.apple`

后续修改必须从本基线标签新建分支，不得覆盖本标签或原始 `.115` 安装包。
