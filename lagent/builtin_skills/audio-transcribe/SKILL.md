---
name: audio-transcribe
description: "音频/语音文件转写与整理：利用本地摄取/转写管线还原口语文本、清理口癖、切分议题段落并提取关键摘要。"
description_en: "Transcribe or process local audio/speech files cleanly into punctuated text, speaker turns, and structured takeaways."
version: "1.0.0"
pack: media
default_enabled: false
tags: ["audio", "transcription", "speech", "whisper"]
when_to_use: "用户上传 `.mp3`/`.wav`/`.m4a`/`.ogg` 等音频或提供杂乱的语音转写原稿时使用。"
triggers:
  keywords: ["音频转写", "语音转文字", "整理录音", "转写稿", "audio transcribe", "transcribe audio", "voice memo"]
  globs: ["*.mp3", "*.wav", "*.m4a", "*.ogg", "*.flac"]
  mimes: ["audio/*"]
requires:
  tools: ["read_file", "run_command"]
  capabilities: ["audio"]
  binaries: []
  python: []
risk: read
profiles:
  compact: "SKILL.compact.md"
license: "MIT"
---

# 音频转写与口语稿精校

1. **检查可用离线转写结果**：首先查看消息中媒体摄取（`ingest`）提供的转写片段与音频时长；若系统内未安装离线 Whisper/语音模型或转写不可用，明确告知当前缺本地 ASR 引擎及可安装的本地工具方案，绝不编造听写内容。
2. **分段与口癖清洗**：
   - 保留原意前提下删除“嗯/那个/就是说”等无意义口癖并恢复标点断句；
   - 按话题转换切分自然段，结合上下文尝试标出说话人角色（无法区分时标 `发言人 A/B`，不可猜姓名）；
   - 对同音词/专有名词疑点加 `[待核对]`。
3. **双层交付**：先给出核心摘要与关键时间节点/行动项，再附精校后的完整整理文稿（长文稿保存到 `output/`）。
