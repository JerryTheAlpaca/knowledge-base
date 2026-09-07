"""M4 来源适配器（docs/02 §5.4、§17；docs/04）。

- subtitles.py：字幕格式解析与规范化（B 站 JSON / SRT / VTT → 统一 segments）。
- bilibili.py：B 站字幕提取器，全部网络访问经 security.safe_fetch。

适配器内部封装的站点接口不是本项目的稳定 API；读取失败时如实降级，
不伪造 cid、不把分享文字或标题当作视频全文（docs/04 §4、§5）。
"""
