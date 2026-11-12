"""桌面业务流程：把平台能力（`_linux`）与本机产品 API 组装成功能。

按下 → 绑定焦点 + 推流识别会话 → 松开 finish → POST /api/v1/utterances；
jobs claim → 按 job 焦点注入 → ack；重复键 → /api/v1/utterances/resend；
截图 → 浮层页面（页面自己调 /api/v1/ocr-translate）。平台状态经 /api/v1/events/platform 进入产品事件流。
本包不导入 GTK，测试用假平台对象驱动。
"""
