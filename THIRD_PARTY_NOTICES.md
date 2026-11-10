# 第三方许可说明

文献通互助（WenXianTong，内部代号 KYTDL）自身代码以 **GNU AGPL-3.0** 发布（见 LICENSE）。
选择 AGPL 是因为核心依赖 PyMuPDF 采用 AGPL-3.0（或 Artifex 商业许可），这样整个项目在分发时与其完全兼容。

本项目不包含、不分发任何第三方文献 PDF；`data/` 已从版本库排除，测试用 PDF 为程序生成的虚构内容。

运行时依赖（通过 pip 安装，许可来自各包元数据）：

| 依赖 | 许可 |
|---|---|
| PyMuPDF | AGPL-3.0 或 Artifex 商业许可 |
| playwright | Apache-2.0 |
| greenlet（playwright 依赖） | MIT 与 PSF-2.0 |
| pyee（playwright 依赖） | MIT |
| typing_extensions | PSF-2.0 |
| certifi | MPL-2.0 |
| olefile | BSD |

浏览器不随项目分发：默认使用本机已安装的 Google Chrome 或 Microsoft Edge；两者都没有时，可由 Playwright 官方流程下载 Chromium，适用其供应方条款。

第三方服务（OpenAlex、Crossref、Europe PMC、NCBI/PMC、Semantic Scholar、Unpaywall、arXiv、bioRxiv/medRxiv、CORE、科研通）各有其使用条款，
与本项目无隶属关系；所获取开放获取文献的再利用以各文献自身许可（如 CC BY）为准。
