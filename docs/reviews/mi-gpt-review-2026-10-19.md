# mi-gpt 对本项目的参考评估

日期：2026-10-19（Asia/Shanghai）。这是只读评估和后续建议，未授权实施下列功能。

## 检查范围与版本

- 上游：[idootop/mi-gpt](https://github.com/idootop/mi-gpt)，main检查时指向 `1aea52ef27e79ca705f597559f73b8ef6b7287b1`，提交时间2026-04-04。
- 仓库2026-04-04归档；README明确已停止维护。老文档的模型名、免费额度、机型“完美运行”等不能当成2026年的厂商保证。
- 读取README、工作原理、FAQ、配置、兼容表、TTS、路线图、LICENSE，以及speaker/ai/base/stream、消息游标、长短期记忆和OpenAI调用代码。
- 对照本地 `platform_app/runtime.py`、`model_services.py`、`mi_conversation.py`、`mi_tts.py`、`memory.py`，没有安装或运行上游代码，没有对真实音箱/模型做探测。

## 结论

最有价值的参考是音箱侧消息与播放的边界处理。本项目已有多用户安全、设备归属、用量、主备模型、并发和有界排队，应保留这些产品决策，不因为上游功能多就更换整个架构。

## P0：建议先完善

### 1. 停止播报、新问题与旧回答的关系

**上游证据**：

- [speaker.ts](https://github.com/idootop/mi-gpt/blob/1aea52ef27e79ca705f597559f73b8ef6b7287b1/src/services/speaker/speaker.ts#L181)：追踪当前问题，判断有无新消息。
- [ai.ts](https://github.com/idootop/mi-gpt/blob/1aea52ef27e79ca705f597559f73b8ef6b7287b1/src/services/speaker/ai.ts#L264)：新问题出现时停止旧流程后续响应。
- [base.ts](https://github.com/idootop/mi-gpt/blob/1aea52ef27e79ca705f597559f73b8ef6b7287b1/src/services/speaker/base.ts#L338)：播放中检测新消息或音乐，终止后续响应；stream.ts可取消待播片段。

**我们的现状**：轮询与应答已分离，但同设备应答仍FIFO；最多暂存3条。`MiSpeaker.tts()`分段等待后继续发下一段，没有会话代次或“已被用户打断”的检查。用户重新唤醒使音箱暂时停下后，平台可能继续提交旧回答下一段。

**建议**：先定义清楚停止/更正/新独立问题的优先级，不要默认任何新消息都取消旧回答。显式停止应取消剩余片段及不再需要的待答任务；每段下发前校验会话版本。已发出的模型调用可能收费，取消不承诺退费。普通暂停仍按既定规则允许当前回答完成，不与用户主动停止混同。

**限制**：云端轮询不能保证每个“暂停”“闭嘴”等原生控制命令都出现在记录中，不能承诺毫秒级或所有机型可用的语音打断。

### 2. 小爱回答类型与动作识别

**上游证据**：[speaker.ts](https://github.com/idootop/mi-gpt/blob/1aea52ef27e79ca705f597559f73b8ef6b7287b1/src/services/speaker/speaker.ts#L284) 同时处理TTS/LLM文本，并避免把TTS+Audio这类音乐结果当成普通问答。

**我们的现状**：至少云端 `_cloud_recent_asks()` 只拼接 `answers[].tts.text`；缺少LLM文本兼容及明确的非问答动作分类。LLM类型记录可能变成空原生回答，从而错过失败接管。现有排除词规则并不能代替对消息结构的判断。

**建议**：统一输出问题ID、时间、回答文本、回答类型、动作类型与是否适合接管。保留现有requestId去重、同时间戳多问题处理和60秒过期规则，不照搬只按timestamp区分消息的做法。

### 3. 机型能力细分与播报完成判断

**上游证据**：[兼容表](https://github.com/idootop/mi-gpt/blob/main/docs/compatibility.md)、[FAQ](https://github.com/idootop/mi-gpt/blob/main/docs/faq.md)、[base.ts](https://github.com/idootop/mi-gpt/blob/1aea52ef27e79ca705f597559f73b8ef6b7287b1/src/services/speaker/base.ts#L338)。不同机型有不同TTS/唤醒/播放属性，有些状态不可靠。其文档把L05B Play、LX04列为不推荐连续对话的正常运行档。

**我们的现状**：已有硬件动作映射、兼容等级和原生AI禁用清单；播报主要依赖片段长度/语速+1秒估时，不能确认真正播完。

**建议**：把能力拆成“能朗读、能可靠查播放状态、能暂停、能再次唤醒、可实验连续对话”等，并记录固件、验证日期与来源。支持且实测可靠时用状态反馈；否则继续使用现有保守估时，不能盲信idle导致截断长回复。未来诊断页解释支持哪些能力、哪些只是指令提交成功。

**必须保留**：上游“技术上可调用”不等于本平台“允许接管”。OH2/OH2P/L17A/X10A等本平台自带AI禁用策略不因上游兼容表而撤销。新增前台只读能力信息也不应开放普通用户修改底层动作编号。

## P1：明确收益后再做

### 4. 首句更快、流式分句与可感知的处理状态

**上游证据**：[stream.ts](https://github.com/idootop/mi-gpt/blob/1aea52ef27e79ca705f597559f73b8ef6b7287b1/src/services/speaker/stream.ts) 积累文本并按标点/长度提交片段，首段有短暂缓冲；[配置示例](https://github.com/idootop/mi-gpt/blob/main/.migpt.example.js)有思考中、完成、错误提示。

**我们的现状**：旧CLI有stream入口，但网页平台明确使用完整模型回复后再分段播放，主备切换也基于尚未开始播出的整次调用。此行为有利于避免串句。

**建议**：先测“收到问题→判定接管→排队→模型首字→首次播报”的分段耗时，再决定流式收益。支持按句提交时要保留待播队列、取消机制和完整用量；一旦已经播出主模型部分回答，不能静默切备用继续拼接另一份回答。无需把README的“秒回”当成延迟保证。

### 5. 对话输入预算与可选摘要记忆

**上游证据**：[MemoryManager](https://github.com/idootop/mi-gpt/blob/1aea52ef27e79ca705f597559f73b8ef6b7287b1/src/services/bot/memory/index.ts)按阈值合成短期、长期摘要；[short-term.ts](https://github.com/idootop/mi-gpt/blob/main/src/services/bot/memory/short-term.ts)和[long-term.ts](https://github.com/idootop/mi-gpt/blob/main/src/services/bot/memory/long-term.ts)会额外调用模型。相关记忆检索/embedding在所读版本中仍有TODO，不是成熟RAG实现。

**我们的现状**：内存TTL+轮数，1024限制的是输出，不限制输入上下文总量；没有自动摘要和持久长期记忆。用户已决定前台不可编辑记忆规则和系统提示词。

**建议**：先补输入上下文token预算和闲置资源清理，再讨论摘要。若未来启用长期记忆，须用户可见/可删、加密、按租户隔离、随来源删除，并单独记账摘要消耗。不要默认把“删除聊天历史”后留下的用户画像无限保留。

### 6. 对话列表增量拉取与运行诊断

**上游证据**：[speaker.ts](https://github.com/idootop/mi-gpt/blob/1aea52ef27e79ca705f597559f73b8ef6b7287b1/src/services/speaker/speaker.ts#L189)先取最近2条，再按时间游标有限翻页追赶消息；[FAQ](https://github.com/idootop/mi-gpt/blob/main/docs/faq.md)记录共享设备不可见、异地登录、部分原生控制不进历史等问题。

**我们的现状**：每次读取最多10条，重启不重放，旧问题60秒过期；已有绑定检查，但后台主要监控模型队列，没有完整的轮询滞后/丢失/首句延迟指标。

**建议**：区分空历史、网络失败、需要重新绑定、共享设备不可见、没有观察到该指令等情况。若后续发现批次截断，考虑限页增量拉取；仍限制新鲜度和最大追赶量，不为完整性把几分钟前的问题重新念出来。无需照搬上游人工导出token文件流程作为多用户产品默认入口。

## P2：可选扩展，不建议当前直接开启

- **连续对话**：上游明确列为实验功能，依赖播放状态/反复唤醒/静音手段；我们的Play与触屏不应直接承诺。失败接管模式也需要清楚的退出和原生功能让路规则。
- **第三方音色**：[TTS文档](https://github.com/idootop/mi-gpt/blob/main/docs/tts.md)可参考接口分层，但音箱必须能访问音频地址，不是localhost；其示例把文字放在URL查询参数中。多用户平台如扩展，应避免日志/缓存泄漏，使用短时音频引用、鉴权、删除策略和独立TTS费用统计。
- **角色/自定义提示词**：与用户已确认的固定音箱规则冲突，不因上游支持就开放。
- **智能家居Agent、事件推送、RAG**：README/路线图或代码TODO不是已完成承诺；不能据此安排自动控制家电或宣称轮询瓶颈已经解决。

## 对1000用户的判断边界

[路线图](https://github.com/idootop/mi-gpt/blob/main/docs/roadmap.md)将事件通知替代轮询列为改进方向，并非现成事件方案。所读主项目偏单实例/设备配置，README链接的多账号GUI是另一个项目；不能把功能成熟度当成千租户压测证据。

我们的邮箱验证、设备唯一归属、加密、普通用户/管理员隔离、主备审计与全局队列仍需保留。后续容量工作应以轮询间隔分布、上游错误、连接数、队列、数据库耗时和内存回收为依据，再考虑设备分片和多实例的独占调度。不要直接复制其轮询频率，也不要假定增加Key就增加账号并发额度。

## 维护与授权注意事项

仓库已归档。其[LICENSE](https://github.com/idootop/mi-gpt/blob/main/LICENSE)为MIT，而[README免责声明](https://github.com/idootop/mi-gpt#%EF%B8%8F-%E5%85%8D%E8%B4%A3%E5%A3%B0%E6%98%8E)另写了不用于商业活动。这里仅记录文本差异，不作法律结论。若未来复制代码用于收费服务，应先核实适用授权并保留必要版权说明；借鉴结构和重新实现不等于自动解决授权问题。

## 建议下一轮讨论顺序

1. 定义停止播报/更正/新问题的处理优先级，补消息类型解析。
2. 做机型能力与运行诊断，验证Play和触屏的实际能力。
3. 基于延迟和token数据决定是否增加流式首句与输入预算。
4. 连续对话、长期记忆和第三方音色作为单独功能评估，不默认开启。

以上均为建议；本轮没有改动业务代码、运行配置或生产服务器。
