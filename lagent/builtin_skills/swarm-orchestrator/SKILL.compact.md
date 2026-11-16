# swarm-orchestrator (Compact)

1. 仅对真正可并行的多模块或“写+审”分离任务开启 Swarm。
2. `swarm_create` 明确 `depends_on`、各写节点互不重叠的文件归属与收尾 `verifier` 节点。
3. `swarm_run` 后监控状态，综合真实产出汇报。
